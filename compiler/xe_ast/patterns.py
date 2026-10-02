"""一层模式的覆盖检查，不递归解构用户的数据。

模式先被语义检查器展开成简单行：一个变体标签和若干常量/_。
这里仅比较这些行，不负责取出载荷，也不推导新的绑定类型。
bool 和 Unit 的取值有限；其他类型使用“出现过的常量 + 其他值”
检查兜底，因而不需要枚举所有整数、字符串或资源值。
"""
from dataclasses import dataclass
from struct import calcsize, pack, unpack

from .typesys import BOOL, NUMERIC, UNIT, Type


@dataclass(frozen=True)
class SelectorCase:
    tag: str
    filters: tuple[str | None, ...]
    # types 与 filters 按筛选位置对应，不等于处理器参数。
    types: tuple[Type, ...]
    payload_types: tuple[Type, ...] | None = None

    @property
    def handler_types(self) -> tuple[Type, ...]:
        return self.types if self.payload_types is None else self.payload_types


def literal_key(value: object, type_: Type, category: str) -> str:
    """常量按实际比较的数值归一化，如 u8 的 b'a' 和 97 是同一个模式。"""
    if category in {"CHAR", "BYTE"}:
        value = ord(str(value))
    if type_.name in {"f32", "f64"}:
        number = float(str(value))
        if type_.name == "f32":
            number = unpack("f", pack("f", number))[0]
        value = 0.0 if number == 0.0 else number
    return repr(value)


def covered(case: SelectorCase, previous: list[SelectorCase]) -> bool:
    """Is every value selected by this case already selected by previous rows?"""
    if any(row.tag == "*" for row in previous):
        return True
    if case.tag == "*":
        return False
    rows = [row.filters for row in previous if row.tag == case.tag]
    if not rows:
        return False
    cache: dict[tuple[int, tuple[int, ...]], bool] = {}
    other = object()  # 不能与任何源码常量相等的“其他取值”。

    def visit(index: int, candidates: tuple[int, ...]) -> bool:
        if not candidates:
            return False
        if index == len(case.filters):
            return True
        key = (index, candidates)
        if key in cache:
            return cache[key]
        selected = case.filters[index]
        if selected is not None:
            domain: list[object] = [selected]
        elif case.types[index] == BOOL:
            domain = ["False", "True"]
        elif case.types[index] == UNIT:
            domain = ["None"]
        else:
            domain = list({rows[row][index] for row in candidates if rows[row][index] is not None})
            type_ = case.types[index]
            finite_integer = type_.name in NUMERIC - {"f32", "f64"}
            width = (calcsize("P") * 8 if type_.name in {"isize", "usize"}
                     else int(type_.name[1:])) if finite_integer else 0
            # 所有常量已由前端验证范围。用数量判断是否枚举完整整数
            # 域，不生成一个庞大的列表；u8 的 256 个值可自然穷尽。
            if not finite_integer or len(domain) != 2 ** width:
                domain.append(other)
        result = all(visit(index + 1, tuple(row for row in candidates
                     if rows[row][index] is None or rows[row][index] == value)) for value in domain)
        cache[key] = result
        return result

    return visit(0, tuple(range(len(rows))))
