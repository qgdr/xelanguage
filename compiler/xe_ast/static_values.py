"""静态标量求值，共用于检查器与 C 初始化器。

这里只计算已有的字面量运算，不运行用户函数、读取可变存储或解释任意
Xe 代码。整数除法与余数遵守 Xe 的向零截断，不能直接使用 Python //。
位运算使用声明的整数位宽，所以 bitnot 0 在 u8 上是 255 而不是 -1。
"""
import math
import struct

from .typesys import NUMERIC


class StaticValueError(ValueError):
    """有静态形状，但其计算本身无效，例如循环引用或除零。"""


def integer_limits(type_):
    if type_ is None or type_.name not in NUMERIC - {"f32", "f64"}:
        return None
    width = struct.calcsize("P") * 8 if type_.name in {"isize", "usize"} else int(type_.name[1:])
    signed = type_.name.startswith("i")
    return width, -(1 << (width - 1)) if signed else 0, (1 << (width - signed)) - 1


def float_value(value, type_):
    """f32 每个运算步骤都舍入，不能先用 f64 算到底再缩窄一次。"""
    if not isinstance(value, float):
        return value
    if type_ is not None and type_.name == "f32":
        try:
            value = struct.unpack("f", struct.pack("f", value))[0]
        except OverflowError as error:
            raise StaticValueError("模块初值的浮点运算超出 f32 有限范围") from error
    if not math.isfinite(value):
        raise StaticValueError("模块初值的浮点运算结果不是有限值")
    return value


def scalar_static_value(node, constants, type_=None, seen=frozenset()):
    kind = node["kind"]
    if kind == "Literal":
        value = node["value"]
        return ord(value) if node["literal_kind"] in {"CHAR", "BYTE"} else float_value(value, type_)
    if kind == "Group":
        return scalar_static_value(node["expression"], constants, type_, seen)
    if kind == "Name":
        name = "::".join(node["path"]["parts"])
        constant = constants.get(name)
        if constant is None:
            return None
        if name in seen:
            raise StaticValueError(f"模块只读初值循环引用 {name}")
        return scalar_static_value(constant.node, constants, constant.type, seen | {name})
    if kind not in {"Unary", "Binary"}:
        return None
    operator = node["operator"]
    if kind == "Unary":
        # -128 的正数部分不必先成为 i8；合法性检查针对完整的一元表达式。
        value = scalar_static_value(node["operand"], constants, type_, seen)
        if value is None:
            return None
        if operator == "not":
            return not value
        if operator == "bitnot":
            limits = integer_limits(type_)
            width = limits[0] if limits else 32
            result = (~value) & ((1 << width) - 1)
            if limits is None or limits[1] < 0:
                if result >= 1 << (width - 1):
                    result -= 1 << width
        else:
            result = -value if operator == "-" else value
    else:
        left = scalar_static_value(node["left"], constants, type_, seen)
        if left is None:
            return None
        # 保持逻辑运算的短路，未执行的右侧不会产生除零等运行错误。
        if operator == "and" and not left:
            return False
        if operator == "or" and left:
            return True
        right = scalar_static_value(node["right"], constants, type_, seen)
        if right is None:
            return None
        if operator in {"/", "%"} and right == 0:
            raise StaticValueError("模块初值除以零")
        if operator == "+":
            result = left + right
        elif operator == "-":
            result = left - right
        elif operator == "*":
            result = left * right
        elif operator in {"/", "%"} and type(left) is int and type(right) is int:
            quotient = abs(left) // abs(right)
            if (left < 0) != (right < 0):
                quotient = -quotient
            # MIN / -1 溢出；MIN % -1 为 0，与 runtime 的特殊分支一致。
            limits = integer_limits(type_)
            if operator == "/" and limits and not limits[1] <= quotient <= limits[2]:
                raise StaticValueError("模块初值的整数除法溢出")
            result = quotient if operator == "/" else left - quotient * right
        elif operator == "/":
            result = left / right
        elif operator == "%":
            raise StaticValueError("浮点数不能使用 %")
        elif operator == "bitand":
            result = left & right
        elif operator == "bitor":
            result = left | right
        elif operator == "bitxor":
            result = left ^ right
        elif operator in {"bitshl", "bitshr"}:
            limits = integer_limits(type_)
            width = limits[0] if limits else 32
            if not 0 <= right < width:
                raise StaticValueError(f"移位次数必须在 0..{width} 内")
            result = left << right if operator == "bitshl" else left >> right
        elif operator == "and":
            return bool(left and right)
        elif operator == "or":
            return bool(left or right)
        else:
            return None
    limits = integer_limits(type_)
    if limits and type(result) is int and not limits[1] <= result <= limits[2]:
        raise StaticValueError(f"模块初值的运算结果超出 {type_} 范围")
    return float_value(result, type_)
