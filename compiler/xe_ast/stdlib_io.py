"""stage0 标准 IO 的公开签名：语义检查和 C 降低共用这一入口。

操作系统读写暂时由 stdlib/io/xe_io.h 实现；不要因此给 IO 增加新的
语法或不同的所有权规则。print 系列的异构格式化参数目前由编译器检查，
还不是已经实现了可变参数泛型/Trait 的普通 Xe 函数。
"""
from .stdlib import StandardFunction
from .typesys import BOOL, IO_ERROR, STR, STRING, UNIT, Type, maybe


# 一次读取有两层可能性：操作是否成功，以及成功时是否还有一行。
# None 只表示 EOF。空行是 Yes[String("")]，不能与 EOF 混淆。
READLINE_RESULT = maybe(maybe(STRING), IO_ERROR)

# 短路径是既有的 io::Error；全限定路径是同一类型，不生成另一套 ABI。
IO_TYPE_ALIASES = {"std::io::Error": IO_ERROR}


IO_FUNCTIONS = {
    "print": StandardFunction((STR,), UNIT, formatted=True),
    "println": StandardFunction((STR,), UNIT, formatted=True),
    "eprintln": StandardFunction((STR,), UNIT, formatted=True),
    "readline": StandardFunction((), READLINE_RESULT),
}

# 这些普通函数的 C ABI 与 Xe 相同，不需要 readline 的 Maybe 布局桥接。
# 用一份映射共同驱动签名和后端，新增检测函数时不必修改多份名称列表。
IO_NATIVE_FUNCTIONS = {
    name: "xe_io_" + name for name in (
        "stdin_is_terminal", "stdout_is_terminal", "stderr_is_terminal",
        "stdout_supports_color", "stderr_supports_color",
    )
}
IO_FUNCTIONS.update({name: StandardFunction((), BOOL) for name in IO_NATIVE_FUNCTIONS})


def normalize_io_name(name: str) -> str:
    """prelude 名称与显式 std::io 路径指向相同接口，不模拟模块加载。

    调用方必须先检查用户函数和局部函数值，才尝试标准库入口。
    不删除任意前缀：拼错的 std::io::函数仍应报告未知名称。
    """
    prefix = "std::io::"
    if name.startswith(prefix) and name[len(prefix):] in IO_FUNCTIONS:
        return name[len(prefix):]
    return name


def io_function(name: str) -> StandardFunction | None:
    return IO_FUNCTIONS.get(normalize_io_name(name))
