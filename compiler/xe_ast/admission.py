"""Fail-closed admission for a deliberately small, reviewed source subset.

This is not a second type checker.  It refuses unreviewed syntax and situations
that would ask the historical checker to invent an unspecified default.  The
ordinary checker still owns type equality, permissions, initialization and
integer bounds.  No AST field is rewritten or annotated here.

The small contextual-type descriptors below only answer whether an integer has
an explicit machine-type source.  They do not implement subtyping, conversions,
Copy/Drop, function-value inference, general generics or a new type algebra.
"""
from dataclasses import dataclass
from typing import Any, NoReturn

from .core_branches import CORE_BRANCH_FIELDS, plan_core_branch
from .declaration_domains import resolve_declaration_domain
from .language_policy import current_profile
from .lexer import KEYWORDS
from .source import Diagnostic, Source
from .type_domains import Domain
from .typesys import PRIMITIVES, STANDARD, Type

Node = dict[str, Any]
INTEGERS = frozenset({f"{prefix}{bits}" for prefix in ("i", "u") for bits in (8, 16, 32, 64)} |
                     {"isize", "usize"})
SCALARS = INTEGERS | {"bool", "str", "Unit"}
BUILTINS = frozenset({"println", "panic"})
# Core-only source names. Keep this separate from typesys.STANDARD so the
# legacy profile may still declare an unrelated user type named View.
CORE_TYPE_NAMES = SCALARS | {"Never", "String", "Array", "View"}

# Every source field must appear here.  Parser extensions therefore do not
# silently become part of the core merely by reusing an existing node kind.
FIELDS = {
    "Module": {"items", "comments", "_sources", "_display_names"},
    "Comment": {"raw", "documentation"},
    "Function": {"name", "public", "generics", "parameters", "result", "constraints", "body"},
    "GenericParameter": {"name", "category"},
    "Parameter": {"name", "type", "mutable"},
    "TypeAlias": {"public", "name", "type"},
    "Enum": {"public", "name", "generics", "variants"},
    "Struct": {"public", "name", "generics", "fields"},
    "FieldDeclaration": {"public", "name", "type"},
    "StructLiteral": {"constructor", "fields"},
    "FieldInitialization": {"name", "operator", "value"},
    "VariantDeclaration": {"name", "payload"},
    "Impl": {"generics", "trait", "target", "constraints", "methods"},
    "NamedType": {"path", "arguments"},
    "SetType": {"members"},
    "SetMember": {"value", "type"},
    "UnionType": {"operator", "left", "right"},
    "PointerType": {"target", "modifiers"},
    "ViewTypeModifier": {"target", "modifiers"},
    "Modifier": {"name"},
    "Path": {"parts"},
    "Block": {"statements", "tail"},
    "Binding": {"name", "mutable", "type", "operator", "value"},
    "Assignment": {"operator", "left", "right"},
    "ExpressionStatement": {"expression"},
    "Return": {"value"},
    "Break": {"value"},
    "Continue": {"value"},
    "Literal": {"literal_kind", "raw", "value"},
    "Name": {"path"},
    "Group": {"expression"},
    "Borrow": {"operand", "modifiers"},
    "Dereference": {"operand"},
    "Call": {"callee", "arguments"},
    "FieldAccess": {"object", "field"},
    "BracketApply": {"object", "arguments"},
    "Array": {"elements"},
    "Index": {"target", "index"},
    "Restriction": {"target", "range"},
    "ViewConstruct": {"target", "modifiers"},
    "Range": {"operator", "lower", "upper"},
    "Pipeline": {"input", "handler"},
    "FunctionTarget": {"target"},
    "HandlerBinding": {"parameters", "body"},
    **CORE_BRANCH_FIELDS,
    "Binary": {"operator", "left", "right"},
    "Unary": {"operator", "operand"},
    "ComparisonChain": {"operands", "operators"},
    "If": {"condition", "then", "otherwise"},
    "While": {"condition", "body"},
}
UNIMPLEMENTED = frozenset({"Struct", "Impl", "StructLiteral", "FieldAccess", "AssociatedAccess",
                           "SetType", "UnionType", "Newtype", "NewType"})


@dataclass(frozen=True)
class _Type:
    name: str
    target: "_Type | None" = None
    mutable: bool = False
    length: int | None = None

    @property
    def integer(self) -> bool:
        return self.target is None and self.name in INTEGERS


UNIT = _Type("Unit")
BOOL = _Type("bool")
STR = _Type("str")
STRING = _Type("String")
NEVER = _Type("Never")
# A branch may obtain its literal carrier from a later, already typed arm.
# This descriptor is only passed to result positions. Checker.core_branch must
# resolve it or reject; it is never a default runtime integer type.
BRANCH_LITERAL = _Type("branch-literal-context")


class _Admission:
    def __init__(self, source: Source, tree: Node):
        self.source, self.tree = source, tree
        # Domain is compile-time-only even if its normal form is a full machine
        # carrier or Never. Keep that provenance instead of laundering it into
        # a runtime _Type and implicitly approving value/Copy/layout rules.
        self.aliases: dict[str, _Type | Domain] = {}
        # Collect names before resolving transparent RHS dependencies. Values
        # still go to separate runtime/domain entries, never an untyped cache.
        self.alias_declarations: dict[str, Node] = {}
        self.alias_stack: list[str] = []
        self.functions: dict[str, tuple[tuple[_Type, ...], _Type]] = {}
        # Explicit function parameters are opaque types, never UNKNOWN and
        # never an implicit Copy assumption. The ordinary checker separately
        # checks every template, including declarations with no call sites.
        self.function_generics: dict[str, tuple[str, ...]] = {}
        self.type_parameters: frozenset[str] = frozenset()
        self.declared_names: frozenset[str] = frozenset()
        # This table admits declaration shapes, not a second runtime type
        # system. The checker owns identity, move state and actual Copy/Drop.
        self.enums: dict[str, dict[str, tuple[_Type, ...]]] = {}
        self.branch_handler_depth = 0
        self.scopes: list[dict[str, _Type]] = []
        self.result = UNIT
        self.loop_depth = 0
        self.condition_depth = 0
        from .core_nominals import NominalSurface
        self.nominals = NominalSurface(self)

    def fail(self, node: Any, message: str, *, implemented=False) -> NoReturn:
        location = node if isinstance(node, dict) else {}
        start, end = self.offset(location), self.offset(location, end=True)
        status = "已确认方向，但本核心子集暂未实现" if implemented else "未确认的语法或行为"
        raise Diagnostic(self.source, start, max(start, end), f"{status}：{message}",
                         "XE-CORE-0002" if implemented else "XE-CORE-0001",
                         "当前默认只接受已审核且已接入的核心；旧基线需显式选择 legacy，仅供归档回归。")

    def shape(self, node: Any, expected: str | None = None) -> str:
        if not isinstance(node, dict) or not isinstance(node.get("kind"), str):
            self.fail(node, "需要可审核的源码 AST 节点，不接受内部类型或占位对象")
        kind = node["kind"]
        if expected is not None and kind != expected:
            self.fail(node, f"该位置需要 {expected}，不能默认采用 {kind}")
        if kind not in FIELDS:
            self.fail(node, f"{kind} 不属于当前核心", implemented=kind in UNIMPLEMENTED)
        extra = set(node) - FIELDS[kind] - {"kind", "span", "_file", "_module"}
        if extra:
            self.fail(node, f"{kind} 的附件/字段 {', '.join(sorted(extra))} 未审核")
        # Loader-owned metadata is permitted, never used as an exemption.
        required = FIELDS[kind] - {"_sources", "_display_names"}
        if required - set(node):
            self.fail(node, f"{kind} 缺少必要字段，不能补造默认值")
        return kind

    def name(self, node: Node, field="name") -> str:
        value = node.get(field)
        if not isinstance(value, str) or not value.isascii() or not value.isidentifier() or value == "_":
            self.fail(node, "该位置只接受普通具名标识符")
        return value

    def path(self, node: Node) -> str:
        self.shape(node, "Path")
        parts = node["parts"]
        if not isinstance(parts, list) or len(parts) != 1:
            self.fail(node, "限定路径/模块解析尚未审核")
        if (not isinstance(parts[0], str) or not parts[0].isascii()
                or not parts[0].isidentifier() or parts[0] == "_"):
            self.fail(node, "该路径没有可审核的普通名称")
        return parts[0]

    def modifiers(self, nodes: Any) -> bool:
        if not isinstance(nodes, list):
            self.fail(nodes, "修饰附件必须是明确列表")
        names = []
        for node in nodes:
            self.shape(node, "Modifier")
            name = self.name(node)
            if name not in {"mut", "unsafe"} or name in names:
                self.fail(node, "只接入不重复的 mut / unsafe 指针附件")
            names.append(name)
        return "mut" in names

    def type(self, node: Node, *, result=False, local_view=False) -> _Type:
        kind = self.shape(node)
        if kind in {"SetType", "UnionType"}:
            self.fail(node, "集合/并集目前只接入 type 声明右侧；运行时值、参数、返回与指针尚未接入",
                      implemented=True)
        if kind == "ViewTypeModifier":
            self.fail(node, "旧 View[T][mut] 已取消；权限写在完整定位结果 P 中，例如 View[i32@[mut]]")
        if kind == "PointerType":
            return _Type("ptr", self.type(node["target"]), self.modifiers(node["modifiers"]))
        if kind != "NamedType":
            self.fail(node, f"{kind} 的类型规则未接入当前核心")
        name = self.path(node["path"])
        if not isinstance(node["arguments"], list):
            self.fail(node, "类型附件必须是显式列表")
        if name == "Self" and self.nominals.current_type is not None:
            if node["arguments"]:
                self.fail(node, "Self已经表示完整类型，不接额外附件")
            return self.nominals.current_type
        if name in self.type_parameters:
            if node["arguments"]:
                self.fail(node, "类型参数已经表示完整类型，不能再次附加类型实参")
            return _Type("$" + name)
        if name == "View":
            # Only the root annotation of an ordinary local binding is wired
            # in this increment. Recursive calls deliberately lose this permit:
            # a View parameter/return/alias or pointer-to-descriptor would need
            # its own reviewed admission and lifetime/context contract.
            if not local_view:
                self.fail(node, "View[P] 本轮仅接入局部绑定的直接类型标注；此处组合尚未接入",
                          implemented=True)
            if len(node["arguments"]) != 1:
                self.fail(node, "View 必须有一个完整定位结果指针附件，写作 View[P]")
            pointer = self.type(node["arguments"][0])
            if pointer.name != "ptr" or pointer.target is None:
                self.fail(node, "View[P] 的 P 必须是完整指针类型，例如 i32@；不再填写元素 i32")
            if self.owned_type(pointer.target):
                self.fail(node, "View[P] 本轮只接已有 Copy 元素；资源元素视图尚未接入",
                          implemented=True)
            return _Type("view", pointer)
        if name == "Array":
            arguments = node["arguments"]
            if len(arguments) != 2:
                self.fail(node, "Array 只接入 Array[T, N]，N 为非负整数字面量")
            element = self.type(arguments[0])
            if element.name.startswith("$"):
                self.fail(node, "抽象元素的Array布局/能力尚未接入；具体资源数组只做整体构造与传递", implemented=True)
            length = arguments[1]
            self.shape(length, "Literal")
            if (length["literal_kind"] != "INTEGER" or type(length["value"]) is not int
                    or length["value"] < 0 or not isinstance(length["raw"], str)
                    or not length["raw"].isascii() or not length["raw"].isdecimal()):
                self.fail(length, "Array 的长度只接入十进制非负整数字面量")
            return _Type("Array", element, length=length["value"])
        if node["arguments"]:
            self.fail(node, "泛型与类型应用尚未审核")
        if name in self.alias_declarations or name in self.aliases:
            resolved = self.resolve_alias(name)
            if isinstance(resolved, Domain):
                self.fail(node, f"{name} 是本轮仅限编译期的类型声明；归一化不授予运行时使用许可",
                          implemented=True)
            if resolved == NEVER and not result:
                self.fail(node, "Never 在参数、局部存储和指针目标中的规则尚未确认；别名不能绕过位置检查")
            return resolved
        if name in SCALARS or name in self.enums or name in self.nominals.structs or name == "Never" and result:
            return _Type(name)
        if name == "String":
            return STRING
        self.fail(node, f"类型 {name} 的规则或此处用法未确认；不补默认类型或转换")

    def resolve_alias(self, name: str) -> _Type | Domain:
        if name in self.aliases:
            return self.aliases[name]
        declaration = self.alias_declarations[name]
        # A forward dependency may be reached before the ordinary module walk
        # visits this declaration. Validate its full source shape here too;
        # a missing RHS in a hand-provided AST must be a diagnostic, not KeyError.
        self.shape(declaration, "TypeAlias")
        if name in self.alias_stack:
            cycle = " -> ".join(self.alias_stack[self.alias_stack.index(name):] + [name])
            self.fail(declaration, f"循环透明别名的规则尚未确定；不能默认展开：{cycle}")
        if len(self.alias_stack) >= 128:
            self.fail(declaration, "类型别名依赖超过当前128层展开上限", implemented=True)
        self.alias_stack.append(name)
        saved = self.type_parameters, self.nominals.current_type
        # A module alias cannot capture the generic parameter of the function
        # which happens to trigger its lazy resolution.
        self.type_parameters = frozenset()
        self.nominals.current_type = None
        try:
            resolved = self.declaration_type(declaration["type"])
            self.aliases[name] = resolved
            return resolved
        finally:
            self.type_parameters, self.nominals.current_type = saved
            self.alias_stack.pop()

    def declaration_type(self, node: Node) -> _Type | Domain:
        """Use the shared scalar resolver, never a parallel domain algorithm.

        Ordinary aliases still use the existing runtime type admission. Only a
        set/union root or a direct alias of an admitted Domain enters this new
        path. Forward aliases are resolved before this classification: an
        unresolved Domain must never be mistaken for a runtime Type.
        """
        kind = self.shape(node)
        if kind == "NamedType":
            name = self.path(node["path"])
            target = self.resolve_alias(name) if name in self.alias_declarations else self.aliases.get(name)
            if not isinstance(target, Domain):
                return self.type(node, result=True)
        elif kind not in {"SetType", "UnionType"}:
            return self.type(node, result=True)

        def runtime_type(value: _Type) -> Type:
            if value.name == "Array":
                assert value.target is not None
                return Type("Array", (runtime_type(value.target), Type(str(value.length))))
            if value.name == "ptr":
                assert value.target is not None
                return Type("ptr", (runtime_type(value.target),), mutable=value.mutable)
            return Type(value.name)

        def lookup(name: str) -> Type | Domain | None:
            if name in SCALARS | {"Never"}:
                return Type(name)
            value = self.resolve_alias(name) if name in self.alias_declarations else self.aliases.get(name)
            if isinstance(value, Domain):
                return value
            if value is not None:
                return runtime_type(value)
            return None

        return resolve_declaration_domain(self.source, node, lookup)

    def owned_type(self, type_: _Type) -> bool:
        """All newly admitted enums remain non-Copy; no implicit derivation.

        A pointer *to* a resource is still Copy and does not own its target.
        Even a presently empty variant does not alter the enum's type ability.
        """
        return (type_.name.startswith("$") or type_.name == "String" or type_.name in self.enums
                or type_.name in self.nominals.structs and type_.name not in self.nominals.copy_names
                or type_.name == "Array" and type_.target is not None and self.owned_type(type_.target))

    @staticmethod
    def substitute_type(type_: _Type, substitutions: dict[str, _Type]) -> _Type:
        """Substitute a reviewed signature, without inferring an argument."""
        if type_.name in substitutions:
            return substitutions[type_.name]
        return _Type(type_.name,
                     _Admission.substitute_type(type_.target, substitutions) if type_.target else None,
                     type_.mutable, type_.length)

    def explicit_type_argument(self, node: Node) -> _Type:
        """Read bracket-expression syntax strictly as a type argument.

        The parser intentionally preserves expressions in [...]. Convert only
        the reviewed type-shaped forms back to a temporary type AST, then use
        the very same runtime-slot gate as ordinary parameters. In particular,
        aliases of declaration-only set domains cannot be laundered via T.
        """
        kind = self.shape(node)
        if kind == "Group":
            return self.explicit_type_argument(node["expression"])
        if kind == "Name":
            return self.type({"kind": "NamedType", "path": node["path"],
                              "arguments": [], "span": node.get("span", {})})
        if kind == "Borrow":
            target = self.explicit_type_argument(node["operand"])
            return _Type("ptr", target, self.modifiers(node["modifiers"]))
        if kind == "BracketApply":
            self.shape(node["object"], "Name")
            name = self.path(node["object"]["path"])
            if name != "Array":
                # View/function/nominal applications keep their ordinary
                # unconnected boundaries; an explicit function argument does
                # not open an otherwise forbidden runtime type position.
                self.fail(node, "类型实参目前只接已有运行类型；此类型应用组合尚未接入", implemented=True)
            arguments = node["arguments"]
            if not isinstance(arguments, list) or len(arguments) != 2:
                self.fail(node, "Array 类型实参需要元素类型和明确长度")
            element = self.explicit_type_argument(arguments[0])
            if element.name.startswith("$"):
                self.fail(node, "抽象元素Array尚未接入；具体完整Array可作为一个类型实参", implemented=True)
            length = arguments[1]
            self.shape(length, "Literal")
            if (length["literal_kind"] != "INTEGER" or type(length["value"]) is not int
                    or length["value"] < 0 or not isinstance(length["raw"], str)
                    or not length["raw"].isascii() or not length["raw"].isdecimal()):
                self.fail(length, "Array 的长度只接入十进制非负整数字面量")
            return _Type("Array", element, length=length["value"])
        self.fail(node, "这里需要已审核的类型实参，不接值、Never存储、函数或未确认类型")

    def generic_call_signature(self, callee: Node) -> tuple[tuple[_Type, ...], _Type]:
        self.shape(callee, "BracketApply")
        target = callee["object"]
        self.shape(target, "Name")
        name = self.path(target["path"])
        if self.lookup(name) is not None or name not in self.function_generics:
            self.fail(callee, "显式函数类型实参仅用于已声明的具名泛型函数")
        arguments = callee["arguments"]
        if not isinstance(arguments, list):
            self.fail(callee, "函数类型实参必须是明确列表")
        parameters = self.function_generics[name]
        if len(arguments) != len(parameters):
            self.contract(callee, f"需要 {len(parameters)} 个显式类型实参，实际为 {len(arguments)} 个",
                          "XE-GENERIC-0001")
        substitutions = {"$" + parameter: self.explicit_type_argument(argument)
                         for parameter, argument in zip(parameters, arguments)}
        values, result = self.functions[name]
        return (tuple(self.substitute_type(type_, substitutions) for type_ in values),
                self.substitute_type(result, substitutions))

    def payload_type(self, node: Node) -> _Type:
        type_ = self.type(node)
        if type_.name not in SCALARS | {"String"} and type_.name not in self.enums:
            self.fail(node, "枚举负载先接已有标量、String和此前声明的枚举；其余复合/指针负载尚未接入",
                      implemented=True)
        return type_

    def variant(self, node: Node, path: Node) -> tuple[_Type, tuple[_Type, ...]]:
        """Resolve a qualified variant without opening arbitrary namespaces."""
        self.shape(path, "Path")
        parts = path["parts"]
        if (not isinstance(parts, list) or len(parts) != 2
                or any(not isinstance(p, str) or not p.isascii() or not p.isidentifier()
                       or p == "_" for p in parts)):
            self.fail(node, "枚举构造/选择器需要 Enum::Variant 的明确路径")
        owner, name = parts
        type_ = self.aliases.get(owner, _Type(owner))
        if not isinstance(type_, _Type) or type_.name not in self.enums:
            self.fail(node, "限定路径尚未接入模块或其他关联接口；这里需要已声明的枚举变体")
        variants = self.enums[type_.name]
        if name not in variants:
            raise Diagnostic(self.source, self.offset(node), self.offset(node, end=True),
                             f"{owner} 没有变体 {name}", "XE-MATCH-0001")
        return type_, variants[name]

    def offset(self, node: Node, *, end=False) -> int:
        span = node.get("span", {})
        position = span.get("end" if end else "start", {}) if isinstance(span, dict) else {}
        value = position.get("offset", 0) if isinstance(position, dict) else 0
        return max(0, min(value, len(self.source.text))) if type(value) is int else 0

    def contract(self, node: Node, message: str, code: str) -> NoReturn:
        raise Diagnostic(self.source, self.offset(node), self.offset(node, end=True), message, code)

    def reject_enum_drop(self, item: Node) -> None:
        """An explicit contract error, not a warning or an inherited impl."""
        trait, target = item["trait"], item["target"]
        if (isinstance(trait, dict) and trait.get("kind") == "NamedType"
                and isinstance(trait.get("path"), dict) and trait["path"].get("parts") == ["Drop"]
                and isinstance(target, dict) and target.get("kind") == "NamedType"):
            type_ = self.type(target)
            if type_.name in self.enums:
                raise Diagnostic(self.source, self.offset(item), self.offset(item, end=True),
                                 "当前阶段禁止为 enum 自定义 Drop；活动负载由编译器自动清理",
                                 "XE-OWN-0002", "整值转交或交给负载自身的析构逻辑；枚举自定义析构以后单独设计。")
        self.fail(item, "其他 impl / Copy / Drop 接口仍未接入本核心", implemented=True)

    def lookup(self, name: str) -> _Type | None:
        return next((scope[name] for scope in reversed(self.scopes) if name in scope), None)

    def hint(self, node: Any) -> _Type | None:
        """Only inspect type sources that do not require evaluating or guessing."""
        if not isinstance(node, dict):
            return None
        kind = node.get("kind")
        if kind == "Name":
            parts = node.get("path", {}).get("parts", [])
            return self.lookup(parts[0]) if len(parts) == 1 else None
        if kind == "Group":
            return self.hint(node.get("expression"))
        if kind == "Dereference":
            pointer = self.hint(node.get("operand"))
            return pointer.target if pointer and pointer.name == "ptr" else None
        if kind == "FieldAccess":
            source = self.hint(node.get("object"))
            return self.nominals.field(node, source) if source else None
        if kind == "StructLiteral":
            constructor = node.get("constructor", {})
            if constructor.get("kind") == "Name":
                return self.explicit_type_argument(constructor)
        if kind == "Unary":
            return BOOL if node.get("operator") == "not" else self.hint(node.get("operand"))
        if kind == "ComparisonChain":
            return BOOL
        if kind == "Binary" and node.get("operator") in {"and", "or"}:
            return BOOL
        if kind == "Binary" and node.get("operator") in {"+", "-", "*", "/", "%"}:
            return self.hint(node.get("left")) or self.hint(node.get("right"))
        if kind in {"Restriction", "ViewConstruct"}:
            if kind == "ViewConstruct":
                restriction = node.get("target", {})
                source = self.hint(restriction.get("target"))
                if source is not None and source.name == "Array":
                    return _Type("view", _Type("ptr", source.target,
                        any(m.get("name") == "mut" for m in node.get("modifiers", []))))
            source = self.hint(node.get("target"))
            if source is not None and source.name == "view":
                return source
        if kind == "Index":
            source = self.hint(node.get("target"))
            if source is not None and source.name in {"Array", "view"}:
                return source.target
        if kind == "Call":
            callee = node.get("callee", {})
            if callee.get("kind") == "FieldAccess":
                source = self.hint(callee.get("object"))
                if source:
                    base = source.target if source.name == "ptr" else source
                    if base == STRING and callee.get("field") == "clone":
                        return STRING
                    signature = self.nominals.call(node, source)
                    return signature[1]
            if callee.get("kind") == "Name":
                signature = self.nominals.qualified(callee)
                if signature:
                    return signature[1]
            if callee.get("kind") == "BracketApply":
                return self.generic_call_signature(callee)[1]
            parts = callee.get("path", {}).get("parts", []) if callee.get("kind") == "Name" else []
            if len(parts) == 1 and parts[0] in self.functions:
                return self.functions[parts[0]][1]
        if kind == "Pipeline":
            handler = node.get("handler", {})
            target = handler.get("target", {}) if handler.get("kind") == "FunctionTarget" else {}
            parts = target.get("path", {}).get("parts", []) if target.get("kind") == "Name" else []
            if len(parts) == 1 and parts[0] in self.functions:
                return self.functions[parts[0]][1]
        return None

    def pipeline(self, node: Node, expected: _Type | None) -> _Type:
        """Admit only ordinary calls/one local binding, not callable values.

        A handler parameter can provide an integer's context just as a normal
        function parameter does. The *result* context cannot choose an unrelated
        input type: allowing that used to lose literal range evidence in the
        historical checker. Consumption, permissions and type equality remain
        the checker's responsibility; this gate does not simulate a move.
        """
        handler = node["handler"]
        kind = self.shape(handler)
        if kind == "FunctionTarget":
            target = handler["target"]
            self.shape(target, "Name")
            name = self.path(target["path"])
            if self.lookup(name) is not None or name not in self.functions:
                self.fail(target, "管道先接入普通具名函数；函数值、内建输出目标和其他可调用对象未接入")
            if name in self.function_generics:
                self.fail(target, "泛型管道目标尚未接入；本轮只接显式类型实参的普通调用", implemented=True)
            parameters, result = self.functions[name]
            value = self.expression(node["input"], parameters[0] if parameters else None)
            if value.name == "view":
                self.fail(node, "View[T] 通过管道传入的递归上下文尚未接入", implemented=True)
            return self.call_result(node, result, expected)
        if kind != "HandlerBinding":
            self.fail(handler, "管道右侧只接函数目标或单输入绑定")
        if isinstance(handler["body"], dict) and handler["body"].get("kind") == "Pipeline":
            self.fail(handler, "参数绑定后无括号接续管道的边界尚未确认；请用块或分组明确处理体")
        parameters = handler["parameters"]
        if not isinstance(parameters, list) or len(parameters) != 1:
            self.fail(handler, "管道绑定必须只有一个参数，不接列表或隐式展开")
        parameter = parameters[0]
        self.shape(parameter, "Parameter")
        if parameter["mutable"] is not False:
            self.fail(parameter, "管道参数绑定不接可写附件；可在处理体内声明可写局部")
        name = "_" if parameter["name"] == "_" else self.name(parameter)
        if name != "_" and (self.lookup(name) is not None or name in self.nominals.structs or name in self.functions or name in BUILTINS
                            or name in self.aliases or name in self.enums
                            or name in CORE_TYPE_NAMES):
            self.fail(parameter, "管道参数的遮蔽/重名边界尚未审核，不默认覆盖已有名字")
        context = self.type(parameter["type"]) if parameter["type"] is not None else None
        value = self.expression(node["input"], context)
        if value.name == "view":
            self.fail(node, "View[T] 通过管道传入的递归上下文尚未接入", implemented=True)
        # A known input supplies an inferred parameter, not a default integer
        # policy. `_` deliberately never appears in the source name environment.
        self.scopes.append({} if name == "_" else {name: context or value})
        try:
            result = self.expression(handler["body"], expected)
            if result.name == "view":
                self.fail(node, "View[T] 作为管道结果的递归上下文尚未接入", implemented=True)
            return result
        finally:
            self.scopes.pop()

    def branch_handler(self, handler: Node, payloads: tuple[_Type, ...],
                       expected: _Type | None) -> _Type:
        """Inspect syntax/context only; the checker owns transfers and joins.

        Inputs come from the selected declaration, never from the handler's
        output context. An immediate body remains inside the enclosing fn.
        """
        kind = self.shape(handler)
        if kind == "FunctionTarget":
            target = handler["target"]
            self.shape(target, "Name")
            name = self.path(target["path"])
            if self.lookup(name) is not None or name not in self.functions:
                self.fail(target, "分支只接入普通具名函数目标；函数值和闭包尚未接入")
            if name in self.function_generics:
                self.fail(target, "泛型分支目标尚未接入；请使用明确的普通泛型调用", implemented=True)
            parameters, result = self.functions[name]
            if len(parameters) != len(payloads):
                self.contract(handler, "分支函数参数数量与负载不一致", "XE-CALL-0001")
            return result
        self.shape(handler, "HandlerBinding")
        parameters = handler["parameters"]
        if not isinstance(parameters, list):
            self.fail(handler, "分支参数必须是显式列表")
        if len(parameters) != len(payloads):
            self.contract(handler, "零负载使用 () ->；一个负载使用具名参数或 _", "XE-CALL-0001")
        scope: dict[str, _Type] = {}
        for parameter, payload in zip(parameters, payloads):
            self.shape(parameter, "Parameter")
            if parameter["mutable"] is not False:
                self.fail(parameter, "分支参数绑定可变附件尚未接入")
            name = "_" if parameter["name"] == "_" else self.name(parameter)
            context = self.type(parameter["type"]) if parameter["type"] is not None else payload
            if name != "_":
                if (self.lookup(name) is not None or name in self.nominals.structs or name in self.functions or name in BUILTINS
                        or name in self.aliases or name in self.enums
                        or name in CORE_TYPE_NAMES):
                    self.fail(parameter, "分支参数遮蔽已有名称的规则尚未审核")
                scope[name] = context
        body = handler["body"]
        if isinstance(body, dict) and body.get("kind") == "Pipeline":
            self.fail(body, "参数绑定后的裸管道接续尚未确认；请用块或分组明确处理体")
        self.scopes.append(scope)
        self.branch_handler_depth += 1
        try:
            return self.expression(body, expected)
        finally:
            self.branch_handler_depth -= 1
            self.scopes.pop()

    def branch(self, node: Node, expected: _Type | None) -> _Type:
        if node["style"] != "named" or node["modifiers"] != []:
            self.fail(node, "本轮仅接入按值枚举逐变体分支；编号分流和指针匹配尚未接入", implemented=True)
        source = self.expression(node["input"])
        if source.name not in self.enums:
            self.fail(node, "本轮分支输入必须是已声明的枚举整值；不解引用指针", implemented=True)
        arms = node["arms"]
        if not isinstance(arms, list):
            self.fail(node, "分支臂必须是显式列表")
        def resolve_variant(selector):
            owner, _ = self.variant(selector, selector["path"])
            if owner != source:
                self.contract(selector, "变体不属于被匹配的枚举类型", "XE-MATCH-0001")
            return selector["path"]["parts"][1]

        plans = plan_core_branch(node, source, self.enums[source.name],
                                 resolve_variant=resolve_variant, reject=self.contract,
                                 describe_payload=lambda type_: type_.name)
        # This descriptor is only a source of literal context. Exact normal
        # result equality and nonlocal return completion are checked separately
        # by Checker.core_branch, not reconstructed by this admission walk.
        context = expected
        for arm, plan in zip(arms, plans):
            result = self.branch_handler(arm["handler"], plan.payload_types, expected or BRANCH_LITERAL)
            if result.name in {"Array", "view"}:
                self.fail(arm, "数组/视图作为分支结果的递归上下文尚未接入", implemented=True)
            if context is None and result not in {NEVER, BRANCH_LITERAL}:
                context = result
        return context or BRANCH_LITERAL

    def condition(self, node: Node) -> None:
        # A condition is outside the admitted loop-body jump context. A nested
        # loop may create its own body target, but cannot inherit an outer one.
        saved_depth = self.loop_depth
        self.loop_depth = 0
        self.condition_depth += 1
        try:
            self.expression(node, BOOL)
        finally:
            self.loop_depth = saved_depth
            self.condition_depth -= 1

    def call_result(self, node: Node, result: _Type, expected: _Type | None) -> _Type:
        if result == NEVER and expected not in {None, NEVER}:
            self.fail(node, "Never 在普通值上下文中的消解尚未接入；不默认进行适配")
        return result

    def place(self, node: Node) -> _Type:
        kind = self.shape(node)
        if kind == "Group":
            return self.place(node["expression"])
        if kind == "Name":
            name = self.path(node["path"])
            type_ = self.lookup(name)
            if type_ is None:
                self.fail(node, f"位置 {name} 没有已确认的局部类型")
            return type_
        if kind == "Dereference":
            pointer = self.expression(node["operand"])
            if pointer.name != "ptr" or pointer.target is None:
                self.fail(node, "解引用操作数必须是已知的普通指针")
            return pointer.target
        if kind == "FieldAccess":
            return self.nominals.field(node, self.expression(node["object"]))
        if kind == "Index":
            source = self.expression(node["target"])
            if source.name != "Array":
                self.fail(node, "View 索引产生 P 指针量，不是保存 P 的盒子；需先 # 或显式 let 保存")
            return self.index(node)
        self.fail(node, "本核心只接入具名局部位置、分组和显式解引用", implemented=True)

    def expression(self, node: Node, expected: _Type | None = None) -> _Type:
        kind = self.shape(node)
        if kind == "Literal":
            literal = node["literal_kind"]
            if not isinstance(node["raw"], str):
                self.fail(node, "字面量缺少源码拼写")
            if literal == "INTEGER" and type(node["value"]) is int:
                if not node["raw"].isascii() or not node["raw"].isdecimal():
                    self.fail(node, "本核心仅接入十进制整数字面量，其他拼写仍待审核")
                if expected == BRANCH_LITERAL:
                    return BRANCH_LITERAL
                if expected is None or not expected.integer:
                    self.fail(node, "整数字面量没有显式机器整数上下文；默认整数规则未确认")
                return expected
            if literal == "STRING" and isinstance(node["value"], str):
                if not node["raw"].startswith('"') or not node["raw"].endswith('"') or "\\" in node["raw"]:
                    self.fail(node, "字符串转义的完整规则尚未审核；本核心先接入无转义字面量")
                return STR
            if literal in {"true", "false"} and type(node["value"]) is bool:
                return BOOL
            if literal == "unit" and node["value"] is None:
                return UNIT
            self.fail(node, "该种字面量或其值形状未审核")
        if kind == "Name":
            self.shape(node["path"], "Path")
            parts = node["path"]["parts"]
            if not isinstance(parts, list):
                self.fail(node, "名字路径必须是明确名称列表")
            if len(parts) > 1:
                type_, payloads = self.variant(node, node["path"])
                if payloads:
                    self.contract(node, "该变体需要一个显式负载附件 [value]", "XE-CALL-0001")
                return type_
            return self.place(node)
        if kind == "BracketApply":
            target = node["object"]
            self.shape(target, "Name")
            type_, payloads = self.variant(node, target["path"])
            arguments = node["arguments"]
            if not isinstance(arguments, list):
                self.fail(node, "枚举负载必须是显式列表")
            if len(payloads) != len(arguments) or not payloads:
                self.contract(node, "枚举构造负载数量与声明不一致；零负载直接写变体路径", "XE-CALL-0001")
            for argument, payload in zip(arguments, payloads):
                self.expression(argument, payload)
            return type_
        if kind == "Group":
            return self.expression(node["expression"], expected)
        if kind == "StructLiteral":
            return self.nominals.constructor(node)
        if kind == "FieldAccess":
            return self.nominals.field(node, self.expression(node["object"]))
        if kind == "Borrow":
            target = self.place(node["operand"])
            if target.name == "view":
                self.fail(node, "视图描述符取址与 View[T]@ 的组合尚未接入；元素地址使用 view(i)",
                          implemented=True)
            return _Type("ptr", target, self.modifiers(node["modifiers"]))
        if kind == "Dereference":
            return self.place(node)
        if kind == "Block":
            return self.block(node, expected)
        if kind == "Array":
            if expected is None or expected.name != "Array" or expected.target is None:
                self.fail(node, "数组字面量需要明确的 Array[T, N] 上下文，不猜默认元素类型")
            if not isinstance(node["elements"], list):
                self.fail(node, "数组元素必须是显式列表")
            for element in node["elements"]:
                self.expression(element, expected.target)
            return expected
        if kind == "Index":
            return self.index(node)
        if kind == "Restriction":
            return self.restriction(node)
        if kind == "ViewConstruct":
            self.shape(node["target"], "Restriction")
            source = self.expression(node["target"]["target"])
            if source.name != "Array":
                self.fail(node, "[@] / [@[mut]] 只为 Array 范围构造 View；已有 View 仅保留 P 限制范围")
            if source.target is not None and self.owned_type(source.target):
                self.fail(node, "资源Array本轮仅整体构造/转交；资源元素定位与视图尚未接入", implemented=True)
            self.range_argument(node["target"]["range"])
            self.modifiers(node["modifiers"])
            names = [m["name"] for m in node["modifiers"]]
            if names not in ([], ["mut"]):
                self.fail(node, "范围地址构造只接受 [@] 或 [@[mut]]")
            return _Type("view", _Type("ptr", source.target, names == ["mut"]))
        if kind == "Unary":
            if node["operator"] == "not":
                self.expression(node["operand"], BOOL)
                return BOOL
            if node["operator"] == "-":
                from .core_numeric import integer_operation
                return integer_operation(self, node, expected)
            self.fail(node, "本次仅确认整数一元 - 与布尔 not；其他一元运算未接入")
        if kind == "Binary":
            if node["operator"] in {"+", "-", "*", "/", "%"}:
                from .core_numeric import integer_operation
                return integer_operation(self, node, expected)
            if node["operator"] in {"and", "or"}:
                self.expression(node["left"], BOOL)
                self.expression(node["right"], BOOL)
                return BOOL
            self.fail(node, f"二元运算 {node['operator']} 的完整合同尚未接入；不默认选择溢出行为",
                      implemented=node["operator"] in {"+", "<<", ">>"})
        if kind == "ComparisonChain":
            from .core_numeric import COMPARISONS
            if (len(node["operators"]) != 1 or node["operators"][0] not in COMPARISONS
                    or len(node["operands"]) != 2):
                self.fail(node, "本次仅确认两个同型整数的六种比较；不开放比较链")
            left, right = node["operands"]
            context = self.hint(left) or self.hint(right)
            first = self.expression(left, context)
            second = self.expression(right, first if first.integer else context)
            if not first.integer or not second.integer:
                self.fail(node, "比较仅接入已有明确类型的机器整数")
            return BOOL
        if kind == "Call":
            callee = node["callee"]
            self.shape(callee)
            if callee["kind"] == "FieldAccess":
                # One already specified library contract, not general method
                # lookup, a Clone trait, or arbitrary FieldAccess admission.
                receiver = self.expression(callee["object"])
                base = receiver.target if receiver.name == "ptr" else receiver
                if base == STRING and callee["field"] == "clone":
                    if not isinstance(node["arguments"], list) or node["arguments"]:
                        self.contract(node, "String.clone() 不接受额外实参", "XE-CALL-0001")
                    return STRING
                parameters, result = self.nominals.call(node, receiver)
                if not isinstance(node["arguments"], list):
                    self.fail(node, "方法实参必须为明确列表")
                for index, argument in enumerate(node["arguments"]):
                    self.expression(argument, parameters[index] if index < len(parameters) else None)
                return self.call_result(node, result, expected)
            if callee["kind"] == "BracketApply":
                parameters, result = self.generic_call_signature(callee)
                if not isinstance(node["arguments"], list):
                    self.fail(node, "调用实参必须为显式列表")
                for index, argument in enumerate(node["arguments"]):
                    self.expression(argument, parameters[index] if index < len(parameters) else None)
                return self.call_result(node, result, expected)
            if callee["kind"] == "Name":
                self.shape(callee["path"], "Path")
                signature = self.nominals.qualified(callee)
                if signature is not None:
                    parameters, result = signature
                    if not isinstance(node["arguments"], list):
                        self.fail(node, "关联函数实参必须为明确列表")
                    for index, argument in enumerate(node["arguments"]):
                        self.expression(argument, parameters[index] if index < len(parameters) else None)
                    return self.call_result(node, result, expected)
            if (callee["kind"] == "Name"
                    and callee["path"].get("parts") == ["String", "from"]):
                self.shape(callee["path"], "Path")
                if not isinstance(node["arguments"], list) or len(node["arguments"]) != 1:
                    self.contract(node, "String::from 恰好接收一个 str 参数", "XE-CALL-0001")
                self.expression(node["arguments"][0], STR)
                return STRING
            if callee["kind"] != "Name" or self.lookup(self.path(callee["path"])) is not None:
                source = self.expression(callee)
                if source.name in {"Array", "view"}:
                    self.fail(node, "旧 Call 定位已取消；Array 用 a⟦i⟧，View 用 w⟦i⟧")
                self.fail(node, "一般可调用对象尚未接入当前核心")
            self.shape(node["callee"], "Name")
            name = self.path(node["callee"]["path"])
            if self.lookup(name) is not None:
                self.fail(node, "函数值/可调用对象尚未审核")
            if not isinstance(node["arguments"], list):
                self.fail(node, "调用实参必须为显式列表")
            if name in self.functions:
                if name in self.function_generics:
                    self.contract(node, "泛型调用必须显式给全类型实参；本核心不推断类型参数",
                                  "XE-GENERIC-0001")
                parameters, result = self.functions[name]
                for index, argument in enumerate(node["arguments"]):
                    self.expression(argument, parameters[index] if index < len(parameters) else None)
                return self.call_result(node, result, expected)
            if name in BUILTINS:
                if name == "println":
                    if not node["arguments"]:
                        self.fail(node, "println 的零实参形式尚未审核；当前要求文本模板")
                    template = node["arguments"][0]
                    while isinstance(template, dict) and template.get("kind") == "Group":
                        self.shape(template, "Group")
                        template = template["expression"]
                    if (not isinstance(template, dict) or template.get("kind") != "Literal"
                            or template.get("literal_kind") != "STRING"):
                        self.fail(node, "动态输出模板的合同尚未接入；当前只接受字符串字面量", implemented=True)
                    text = template.get("value")
                    if not isinstance(text, str) or any(c in text.replace("{}", "") for c in "{}"):
                        self.fail(template, "当前仅接入 {} 占位符；其他格式规则尚未审核")
                for index, argument in enumerate(node["arguments"]):
                    type_ = self.expression(argument, STR if index == 0 else None)
                    if type_.name.startswith("$"):
                        self.fail(argument, "无约束类型参数没有输出/格式化能力；不从某个具体实例猜测", implemented=True)
                    if type_ == NEVER:
                        self.fail(argument, "不能把 Never 默认适配为输出或 panic 的输入")
                    string_pointer = type_.name == "ptr" and type_.target == STRING
                    if index and (type_.name in {"ptr", "Array", "view"} and not string_pointer
                                  or type_.name in self.enums):
                        self.fail(argument, "指针格式化的访问规则尚未接入本窄子集", implemented=True)
                return self.call_result(node, NEVER if name == "panic" else UNIT, expected)
            self.fail(node, f"调用 {name} 没有已接入的具名函数签名；不默认提供库接口、转换或构造")
        if kind == "Pipeline":
            return self.pipeline(node, expected)
        if kind == "Branch":
            return self.branch(node, expected)
        if kind == "If":
            self.condition(node["condition"])
            then = self.block(node["then"], expected)
            if node["otherwise"] is None:
                return UNIT
            otherwise = self.expression(node["otherwise"], expected or then)
            return otherwise if then == NEVER else then
        if kind == "While":
            # The target exists only in the body: a condition block cannot
            # quietly inherit the current while as an unspecified jump target.
            self.condition(node["condition"])
            self.loop_depth += 1
            try:
                self.block(node["body"], UNIT)
            finally:
                self.loop_depth -= 1
            return UNIT
        self.fail(node, f"{kind} 在表达式位置尚未接入")

    def index(self, node: Node) -> _Type:
        self.shape(node, "Index")
        source = self.expression(node["target"])
        if source.name not in {"Array", "view"}:
            self.fail(node, "⟦i⟧ 只为 Array 或 View 定义索引")
        self.expression(node["index"], _Type("usize"))
        assert source.target is not None
        if source.name == "Array" and self.owned_type(source.target):
            self.fail(node, "资源Array本轮仅整体构造/转交；单元素定位与移出尚未接入", implemented=True)
        return source.target

    def restriction(self, node: Node) -> _Type:
        source = self.expression(node["target"])
        if source.name == "Array":
            self.fail(node, "Array 裸范围不是独立量；构造地址映射须写 a⟨范围⟩[@] 或 [@[mut]]")
        if source.name != "view":
            self.fail(node, "⟨范围⟩限制只用于已有 View")
        self.range_argument(node["range"])
        return source

    def range_argument(self, range_node: Node) -> None:
        while self.shape(range_node) == "Group":
            range_node = range_node["expression"]
        self.shape(range_node, "Range")
        if range_node["operator"] not in {"..", "..="}:
            self.fail(range_node, "只接入半开或闭区间切片")
        lower, upper = range_node["lower"], range_node["upper"]
        if (lower is None) != (upper is None) or lower is None and range_node["operator"] != "..":
            self.fail(range_node, "范围需有两个端点，或用 .. 表示当前完整范围")
        if lower is not None:
            self.expression(lower, _Type("usize"))
            self.expression(upper, _Type("usize"))

    def statement(self, node: Node) -> None:
        kind = self.shape(node)
        if kind == "Binding":
            name = self.name(node)
            if (self.lookup(name) is not None or name in self.nominals.structs or name in self.functions or name in BUILTINS
                    or name in self.aliases or name in self.enums
                    or name in CORE_TYPE_NAMES or name in self.type_parameters):
                self.fail(node, "遮蔽/重名规则尚未审核，不能默认覆盖已有名字")
            if type(node["mutable"]) is not bool:
                self.fail(node, "绑定写权限必须由 let / let[mut] 明确给出")
            if node["type"] is None:
                self.contract(node, "每个局部 let / let[mut] 都必须显式标注 : 类型；视图也不例外",
                              "XE-CORE-0001")
            type_ = self.type(node["type"], local_view=True)
            if node["value"] is None:
                if node["operator"] is not None:
                    self.fail(node, "未初始化声明不能附带缺失右侧的赋值符号")
            else:
                if node["operator"] not in {"=", "<<"}:
                    self.fail(node, "声明初始化只接受 = 或 <<；反向转移用于已有目标的赋值语句")
                self.expression(node["value"], type_)
            self.scopes[-1][name] = type_
            return
        if kind == "Assignment":
            if node["operator"] not in {"=", "<<", ">>"}:
                self.fail(node, "该赋值运算未审核")
            target_node = node["right"] if node["operator"] == ">>" else node["left"]
            value_node = node["left"] if node["operator"] == ">>" else node["right"]
            target = self.place(target_node)
            plain_target = target_node
            while plain_target["kind"] == "Group":
                plain_target = plain_target["expression"]
            if node["operator"] in {"<<", ">>"} and plain_target["kind"] != "Name":
                self.fail(node, "显式转移当前只接直接本地目标；复杂目标定位顺序和重叠状态尚未接入", implemented=True)
            if self.owned_type(target):
                if plain_target["kind"] != "Name":
                    self.fail(node, "本轮资源替换只接本地具名盒子；经指针/元素写入资源的状态与旧值清理仍未接通", implemented=True)
            self.expression(value_node, target)
            return
        if kind == "ExpressionStatement":
            self.expression(node["expression"])
            return
        if kind == "Return":
            if self.condition_depth:
                self.fail(node, "条件块提前返回的组合规则尚未接入")
            if node["value"] is not None:
                self.expression(node["value"], self.result)
            return
        if kind in {"Break", "Continue"}:
            if not self.loop_depth or node["value"] is not None:
                self.fail(node, "只接入循环体内无标签、无值的 break / continue")
            return
        self.fail(node, f"{kind} 在语句位置尚未接入")

    def block(self, node: Node, expected: _Type | None = None) -> _Type:
        self.shape(node, "Block")
        if not isinstance(node["statements"], list):
            self.fail(node, "块语句必须为显式列表")
        self.scopes.append({})
        try:
            for statement in node["statements"]:
                self.statement(statement)
            return UNIT if node["tail"] is None else self.expression(node["tail"], expected)
        finally:
            self.scopes.pop()

    def validate(self) -> None:
        self.shape(self.tree, "Module")
        if not isinstance(self.tree["items"], list) or not isinstance(self.tree["comments"], list):
            self.fail(self.tree, "模块内容与注释必须是明确列表")
        # Generic-name shadowing is a syntax boundary, not an accident of the
        # declaration-registration order. Name dependencies of ordinary named
        # functions do not require users to sort source declarations manually.
        self.declared_names = frozenset(
            item["name"] for item in self.tree["items"]
            if isinstance(item, dict) and item.get("kind") in {"Function", "TypeAlias", "Enum", "Struct"}
            and isinstance(item.get("name"), str))
        self.alias_declarations = {
            item["name"]: item for item in self.tree["items"]
            if isinstance(item, dict) and item.get("kind") == "TypeAlias"
            and isinstance(item.get("name"), str)}
        self.nominals.prepare(self.tree["items"])
        registered_names = set()
        functions = []
        for comment in self.tree["comments"]:
            self.shape(comment, "Comment")
            if (not isinstance(comment["raw"], str) or not comment["raw"].startswith("//")
                    or type(comment["documentation"]) is not bool):
                self.fail(comment, "当前只接入 // 行注释，其他词法形式仍待审核")
        for item in self.tree["items"]:
            kind = self.shape(item)
            if kind == "Impl":
                continue  # Fully checked by the nominal surface collector.
            if kind not in {"Function", "TypeAlias", "Enum", "Struct"}:
                self.fail(item, f"模块声明 {kind} 尚未接入")
            name = self.name(item)
            if item["public"] is not False:
                self.fail(item, "公开/私有与 pub 的规则尚未审核")
            if name in registered_names or name in CORE_TYPE_NAMES | BUILTINS:
                self.fail(item, "声明重名或重定义内建名字的规则尚未审核")
            registered_names.add(name)
            if kind == "TypeAlias":
                self.resolve_alias(name)
                continue
            if kind == "Struct":
                continue
            if kind == "Enum":
                if name in PRIMITIVES | STANDARD | {"Self", "None", "Maybe", "ptr", "maybe", "tuple", "closure"}:
                    # The historical runtime also dispatches on these names.
                    # A source enum must not accidentally acquire a builtin
                    # layout or destructor. This namespace/identity collision
                    # is not resolved by merely registering another spelling.
                    self.fail(item, "与既有内建/内部类型同名的名义身份隔离尚未接入；不默认采用其布局或析构")
                # Resolve payloads before registration: recursive and forward
                # ownership/layout are deliberately not implicitly approved.
                continue
            if not isinstance(item["generics"], list) or not isinstance(item["constraints"], list):
                self.fail(item, "函数类型参数及约束必须为明确列表")
            if item["constraints"]:
                self.fail(item, "Trait/Copy 约束尚未接入本次最小显式泛型", implemented=True)
            generic_names = []
            for generic in item["generics"]:
                self.shape(generic, "GenericParameter")
                generic_name = self.name(generic)
                if (generic["category"] != "type" or generic_name in generic_names or generic_name in KEYWORDS
                        or generic_name in CORE_TYPE_NAMES | BUILTINS | set(self.aliases)
                        or generic_name in PRIMITIVES | STANDARD | {"Self", "None", "ptr", "maybe", "tuple", "closure"}
                        or generic_name in self.declared_names):
                    self.fail(generic, "类型参数须为互异且不遮蔽声明/内建类型的普通类型名字")
                generic_names.append(generic_name)
            self.type_parameters = frozenset(generic_names)
            if item["result"] is None:
                self.contract(item, "具名函数必须显式写 -> Type，不能省略返回类型", "XE-CORE-0001")
            if not isinstance(item["parameters"], list):
                self.fail(item, "函数参数必须为显式列表")
            parameters = []
            names = set()
            for parameter in item["parameters"]:
                self.shape(parameter, "Parameter")
                parameter_name = self.name(parameter)
                if parameter_name in names or parameter["mutable"] is not False:
                    self.fail(parameter, "参数重名或绑定可变附件未审核")
                names.add(parameter_name)
                if parameter["type"] is None:
                    self.contract(parameter, "具名函数的每个参数必须显式标注 : 类型", "XE-CORE-0001")
                parameters.append(self.type(parameter["type"]))
            self.functions[name] = (tuple(parameters), self.type(item["result"], result=True))
            if generic_names:
                self.function_generics[name] = tuple(generic_names)
            functions.append(item)
            self.type_parameters = frozenset()
        for function in functions:
            parameters, self.result = self.functions[function["name"]]
            self.type_parameters = frozenset(self.function_generics.get(function["name"], ()))
            self.scopes = [{parameter["name"]: type_ for parameter, type_ in zip(function["parameters"], parameters)}]
            if set(self.scopes[0]) & (set(self.functions) | set(BUILTINS) | set(self.aliases)
                                      | set(self.enums) | set(self.nominals.structs) | set(CORE_TYPE_NAMES) | set(self.type_parameters)):
                self.fail(function, "参数遮蔽声明名字的规则尚未审核")
            if function["name"] == "main" and (parameters or self.result != UNIT or self.type_parameters):
                self.fail(function, "当前核心入口只接入无参数且显式返回 Unit 的 main；其他入口 ABI 未审核")
            self.block(function["body"], self.result)
        for method, owner in self.nominals.method_nodes:
            self.nominals.current_type = _Type(owner)
            parameters, self.result = self.nominals.methods[owner, method["name"]]
            self.type_parameters = frozenset()
            self.scopes = [{parameter["name"]: type_ for parameter, type_ in zip(method["parameters"], parameters)}]
            self.block(method["body"], self.result)
        self.nominals.current_type = None


def validate_core_source(source: Source, tree: Node) -> None:
    """Reject unspecified source behavior; legacy is an explicit caller choice."""
    if current_profile() == "legacy":
        # AST consumers must not bypass the same edition boundary enforced by
        # the parser. Only new core declaration nodes are gated here; do not
        # reinterpret or migrate historical AST fields during old regressions.
        pending: list[Any] = [tree]
        seen: set[int] = set()
        while pending:
            value = pending.pop()
            if not isinstance(value, (dict, list)) or id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, list):
                pending.extend(value)
                continue
            if value.get("kind") in {"SetType", "SetMember", "UnionType", "Access", "AccessModifier",
                                    "Index", "Restriction", "ViewConstruct",
                                    "ViewTypeModifier",
                                    "WholeBranchArm", "ElseSelector"}:
                span = value.get("span", {})
                start = span.get("start", {}).get("offset", 0) if isinstance(span, dict) else 0
                end = span.get("end", {}).get("offset", start) if isinstance(span, dict) else start
                start = max(0, min(start, len(source.text))) if type(start) is int else 0
                end = max(start, min(end, len(source.text))) if type(end) is int else start
                raise Diagnostic(source, start, end, "新的类型声明或访问 AST 仅在 core 中接入，不能以 legacy AST 绕过边界",
                                 "XE-CORE-0002", "legacy 只保留旧玩具语言；新类型声明请使用默认 core。")
            pending.extend(value.values())
        return
    _Admission(source, tree).validate()
