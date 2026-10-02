"""The accepted extern syntax, with an explicit and deliberately small C ABI.

Xe Unit results are bridged to C void by the backend. Other values must already
have the same representation in Xe and C; aggregates, resources and nullable
Maybe pointers are never silently reinterpreted as C values.
"""
import re
from typing import TYPE_CHECKING

from .typesys import NUMERIC, UNIT, Type

if TYPE_CHECKING:
    from .semantic import Checker, Signature


C_KEYWORDS = set("""auto break case char const continue default do double else enum
extern float for goto if inline int long register restrict return short signed
sizeof static struct switch typedef union unsigned void volatile while _Alignas
_Alignof _Atomic _Bool _Complex _Generic _Imaginary _Noreturn _Static_assert
_Thread_local""".split())
C_SCALARS = NUMERIC | {"bool", "char"}


def external_symbol(checker: "Checker", signature: "Signature") -> str:
    name = signature.node["name"]
    return checker.tree.get("_display_names", {}).get(name, name).split("::")[-1]


def ffi_type_supported(type_: Type, *, result=False) -> bool:
    if type_ == UNIT:
        return result
    if type_.name in C_SCALARS:
        return True
    if type_.name == "ptr":
        # A pointer does not grant an ABI to its Xe-specific pointee layout.
        return ffi_type_supported(type_.args[0])
    if type_.name == "fn":
        # Internal fn values return XeUnit rather than C void. Until an explicit
        # callback bridge exists, only signatures with an identical ABI pass.
        return (type_.args[-1] != UNIT and
                all(ffi_type_supported(t) for t in type_.args[:-1]) and
                ffi_type_supported(type_.args[-1]))
    return False


def validate_externals(checker: "Checker") -> None:
    symbols = {}
    for block in checker.tree["items"]:
        if block["kind"] != "Extern":
            continue
        if block["abi"]["value"] != "C":
            checker.fail(block["abi"], '当前外部接口只支持 extern "C"', "XE-FFI-0001")
        for node in block["functions"]:
            signature = checker.functions[node["name"]]
            symbol = external_symbol(checker, signature)
            if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol) or
                    symbol in C_KEYWORDS or symbol == "main" or
                    symbol.startswith(("_", "xe_"))):
                checker.fail(node, f"{symbol} 不能用作当前 C 外部函数符号", "XE-FFI-0001",
                             "使用非保留的 ASCII C 函数名；可在 C 包装函数中调用原接口")
            if signature.generics or node.get("constraints"):
                checker.fail(node, "C 外部函数不能声明泛型或 Trait 约束", "XE-FFI-0001",
                             "在 Xe 普通函数中封装具有具体 C 签名的外部函数")
            for parameter, type_ in zip(node["parameters"], signature.parameters):
                if not ffi_type_supported(type_):
                    checker.fail(parameter["type"], f"{type_} 没有受支持的 C 参数 ABI", "XE-FFI-0001",
                                 "传递数值、bool、char、这些类型的指针，或同 ABI 的无捕获函数指针")
            if not ffi_type_supported(signature.result, result=True):
                checker.fail(node["result"], f"{signature.result} 没有受支持的 C 返回 ABI", "XE-FFI-0001",
                             "C void 对应 Xe Unit；资源、聚合和 T? 需通过 C/Xe 包装函数转换")
            shape = (tuple(signature.parameters), signature.result)
            if symbol in symbols and symbols[symbol] != shape:
                checker.fail(node, f"C 外部符号 {symbol} 在不同模块的声明签名不一致", "XE-FFI-0002")
            symbols[symbol] = shape
