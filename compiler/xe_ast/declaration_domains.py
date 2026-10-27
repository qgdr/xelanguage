"""Resolve the reviewed declaration-only set/union slice to scalar domains.

This adapter is deliberately narrower than the parser and the internal domain
algebra.  It selects no integer default, conversion, runtime layout or Copy/Drop
rule.  A resulting full domain (or Never) does not erase the fact that its source
used declaration-only syntax; the caller must retain that provenance separately.

All accepted AST fields are checked here as well as at source admission.  Direct
AST clients therefore cannot approve an unknown feature by attaching a flag or
by supplying a forged literal's decoded value.
"""
from collections.abc import Callable
from typing import Any, NoReturn

from .lexer import Lexer
from .source import Diagnostic, Source
from .type_domains import (
    FIXED_INTEGERS,
    UNIT_VALUE,
    Constant,
    Domain,
    DomainError,
    DuplicateMemberError,
    finite,
    scalar_domain,
    union,
)
from .typesys import Type

Node = dict[str, Any]
Lookup = Callable[[str], Type | Domain | None]
_CARRIERS = FIXED_INTEGERS | {"bool", "Unit"}
_FIELDS = {
    "SetType": {"members"},
    "SetMember": {"value", "type"},
    "UnionType": {"operator", "left", "right"},
    "NamedType": {"path", "arguments"},
    "Path": {"parts"},
    "Literal": {"literal_kind", "raw", "value"},
    "Unary": {"operator", "operand"},
}


class _DeclarationResolver:
    def __init__(self, source: Source, lookup: Lookup):
        self.source, self.lookup = source, lookup
        self.active: set[int] = set()

    def fail(self, node: Any, message: str, code="XE-CORE-0001") -> NoReturn:
        # A malformed externally supplied span must not obscure the actual
        # rejection with a Python exception in error reporting.
        span = node.get("span") if isinstance(node, dict) else None
        begin = span.get("start") if isinstance(span, dict) else None
        finish = span.get("end") if isinstance(span, dict) else None
        start = begin.get("offset", 0) if isinstance(begin, dict) else 0
        end = finish.get("offset", start) if isinstance(finish, dict) else start
        if type(start) is not int or type(end) is not int:
            start = end = 0
        start = max(0, min(start, len(self.source.text)))
        end = max(start, min(end, len(self.source.text)))
        hint = None
        if code == "XE-CORE-0001":
            hint = "该形式或行为尚未确认；本次只实现明确标量、互异成员的类型声明，不猜测默认规则。"
        elif code == "XE-CORE-0002":
            hint = "本次仅接入同载体标量类型声明；不自动选择异质布局、运行表示或资源能力。"
        raise Diagnostic(self.source, start, end, message, code, hint)

    def shape(self, node: Any, expected: str | None = None) -> str:
        if not isinstance(node, dict) or not isinstance(node.get("kind"), str):
            self.fail(node, "类型声明需要明确的源码 AST 节点")
        kind = node["kind"]
        if expected is not None and kind != expected:
            self.fail(node, f"此处需要 {expected}，实际为 {kind}")
        if kind not in _FIELDS:
            self.fail(node, f"类型声明中的 {kind} 尚未接入本次标量子集")
        fields = set(node)
        extra = fields - _FIELDS[kind] - {"kind", "span", "_file", "_module"}
        if extra:
            self.fail(node, f"{kind} 包含未确认字段：{', '.join(sorted(map(str, extra)))}")
        if _FIELDS[kind] - fields:
            self.fail(node, f"{kind} 缺少必要字段，不补造默认值")
        return kind

    def named(self, node: Node) -> Domain:
        self.shape(node, "NamedType")
        path = node["path"]
        self.shape(path, "Path")
        parts = path["parts"]
        if (not isinstance(parts, list) or len(parts) != 1
                or not isinstance(parts[0], str) or not parts[0].isascii()
                or not parts[0].isidentifier() or parts[0] == "_"):
            self.fail(path, "本次类型声明仅接受单段普通类型名，不推定模块或忽略项语义")
        arguments = node["arguments"]
        if not isinstance(arguments, list) or arguments:
            self.fail(node, "泛型与类型附件尚未确认，标量声明不能附加类型实参")
        name = parts[0]
        if name in _CARRIERS | {"Never"}:
            resolved: Type | Domain | None = Type(name)
        else:
            resolved = self.lookup(name)
        if resolved is None:
            self.fail(node, f"未知类型 {name}", "XE-NAME-0001")
        if isinstance(resolved, Domain):
            if resolved.carrier is None or resolved.carrier.name in _CARRIERS:
                return resolved
            self.fail(node, f"类型 {name} 的载体尚未接入声明域", "XE-CORE-0002")
        if (not isinstance(resolved, Type) or resolved.args or resolved.mutable
                or resolved.identity is not None or resolved.unsafe):
            self.fail(node, f"类型 {name} 不是已接入的无附件标量声明", "XE-CORE-0002")
        if resolved.name not in _CARRIERS | {"Never"}:
            self.fail(node, f"类型 {name} 的载体尚未接入声明域", "XE-CORE-0002")
        return scalar_domain(resolved)

    def literal(self, node: Node) -> tuple[str, int | bool | None]:
        """Decode only approved token forms and verify an external AST's value."""
        self.shape(node, "Literal")
        kind, raw, value = node["literal_kind"], node["raw"], node["value"]
        if not isinstance(kind, str) or kind not in {"INTEGER", "true", "false", "unit"}:
            self.fail(node, "该常量载体尚未确认为本次 set 成员；不执行任意常量表达式")
        if not isinstance(raw, str):
            self.fail(node, "字面量必须保留可核对的原始拼写")
        if kind == "INTEGER" and (not raw.isascii() or not raw.isdecimal()):
            self.fail(node, "set 整数成员当前只接入十进制数字拼写，其他进制与分隔符规则尚未确认")
        try:
            tokens = Lexer(Source(raw, self.source.filename)).scan()
        except Diagnostic:
            self.fail(node, "字面量原文不是对应的完整词法单元")
        if (len(tokens) != 2 or tokens[0].kind != kind or tokens[0].text != raw
                or tokens[0].start != 0 or tokens[0].end != len(raw)):
            self.fail(node, "字面量原文与 AST 类别不一致")
        if kind == "INTEGER":
            cleaned = raw.replace("_", "")
            try:
                decoded: int | bool | None = int(
                    cleaned, 0 if cleaned.lower().startswith(("0x", "0o", "0b")) else 10)
            except ValueError:
                self.fail(node, "整数字面量无法解码")
        elif kind in {"true", "false"}:
            decoded = kind == "true"
        else:
            decoded = None
        # Python equates True with 1: explicit type equality is required here.
        if type(value) is not type(decoded) or value != decoded:
            self.fail(node, "字面量解码值与原文不一致，不能用 AST 数据改变常量身份")
        return kind, decoded

    def member(self, node: Node) -> Constant:
        self.shape(node, "SetMember")
        value_node, annotation = node["value"], node["type"]
        kind = self.shape(value_node)
        negative = kind == "Unary"
        if negative:
            if value_node["operator"] != "-":
                self.fail(value_node, "set 当前只接入负整数字面量，不求值其他一元运算")
            literal_node = value_node["operand"]
        else:
            literal_node = value_node
        literal_kind, value = self.literal(literal_node)
        if negative:
            if literal_kind != "INTEGER" or type(value) is not int:
                self.fail(value_node, "set 当前只接入负整数字面量")
            value = -value

        if annotation is None:
            if literal_kind == "INTEGER":
                self.fail(value_node, "set 整数成员必须显式标注机器整数类型；默认整数类型未确定")
            carrier = Type("bool" if literal_kind in {"true", "false"} else "Unit")
        else:
            domain = self.resolve(annotation)
            if domain.carrier is None or domain.members is not None:
                self.fail(annotation, "set 成员标注为有限类型或空域时的规则尚未确定")
            carrier = domain.carrier

        native = "integer" if literal_kind == "INTEGER" else (
            "bool" if literal_kind in {"true", "false"} else "Unit")
        if (native == "integer" and carrier.name not in FIXED_INTEGERS
                or native != "integer" and carrier.name != native):
            self.fail(node, f"{native} 字面量不能定型为 {carrier.name}；成员标注不是转换",
                      "XE-TYPE-0001")
        constant_value = UNIT_VALUE if native == "Unit" else value
        assert constant_value is not None  # Validated INTEGER / bool literal above.
        try:
            return Constant(carrier, constant_value)
        except DomainError as error:
            self.fail(node, str(error), "XE-TYPE-0001")

    def resolve(self, node: Node) -> Domain:
        kind = self.shape(node)
        if id(node) in self.active or len(self.active) >= 128:
            self.fail(node, "类型表达式循环或嵌套过深，不能默认展开")
        self.active.add(id(node))
        try:
            if kind == "NamedType":
                return self.named(node)
            if kind == "SetType":
                members = node["members"]
                if not isinstance(members, list):
                    self.fail(node, "set 成员必须是明确列表")
                constants = [self.member(member) for member in members]
                if len({member.carrier for member in constants}) > 1:
                    self.fail(node, "混合载体 set 成员的规则尚未确认，不隐式扩大或合并身份")
                try:
                    return finite(constants)
                except DuplicateMemberError as error:
                    self.fail(members[error.duplicate_index],
                              f"重复 set 成员的处理尚未确认：{error}")
            elif kind == "UnionType":
                if not isinstance(node["operator"], str) or node["operator"] not in {"|", "union"}:
                    self.fail(node, "未确认的类型并集运算符")
                left, right = self.resolve(node["left"]), self.resolve(node["right"])
                try:
                    return union(left, right)
                except DomainError:
                    self.fail(node, "跨载体并集的编译期声明归一化尚未接入本子集；声明本身不分配运行存储",
                              "XE-CORE-0002")
            self.fail(node, f"{kind} 不是当前已接入的类型表达式")
        finally:
            self.active.remove(id(node))


def resolve_declaration_domain(source: Source, node: Node, lookup: Lookup) -> Domain:
    """Resolve one declaration RHS; ``lookup`` supplies resolved prior aliases.

    This function reports source diagnostics but never mutates the source AST,
    declaration table, historical ``Type`` objects or domain instances.
    """
    return _DeclarationResolver(source, lookup).resolve(node)
