"""检查器与 C 后端共用的既有输出格式能力，不暗中增加 Display/Debug。

只有 {} 的一层指针自动读取规则和 {:p} 的地址输出已经实现。用户类型
不能仅凭 Copy 或一个同名普通方法自动加入格式化协议；未来的公开协议
需要另外审核。字符串构造 format(...) 也不等同于 print/println。
"""
import string

from .typesys import NEVER, NUMERIC, Type

DISPLAY_NAMES = NUMERIC | {
    "$integer", "str", "String", "bool", "char", "ConversionError", "AllocError",
    "Expired", "io::Error", "SyncError", "ThreadError",
}


def parse_format_template(template: str):
    """保留文字和字段顺序，包括 {{/}}；花括号错误仍使用 ValueError。"""
    return [(text, field, spec or "", conversion)
            for text, field, spec, conversion in string.Formatter().parse(template)]


def format_field_issue(field: str, spec: str, conversion: str | None) -> str | None:
    if field != "" or conversion or spec not in {"", "p"}:
        return "当前版本格式化只支持匿名 {} 与指针 {:p}；命名、序号、转换和其他格式附件尚未实现"
    return None


def format_argument_issue(type_: Type, spec: str) -> str | None:
    # Never 在参数求值时终止执行，后端不会尝试格式化一个不存在的值。
    if type_ == NEVER:
        return None
    if spec == "p":
        return None if type_.name == "ptr" else "{:p} 需要指针；使用 value@ 显式取得地址"
    value_type = type_.args[0] if type_.name == "ptr" else type_
    if value_type.name not in DISPLAY_NAMES:
        return f"当前版本尚未实现 {type_} 的 {{}} 格式化；请显式输出已支持的字段或转成 str/String"
    return None
