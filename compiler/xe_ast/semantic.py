"""单文件语义检查：名称、类型、初始化、所有权和普通指针风险提示。

这是 AST 上的参考检查器，不生成代码。每个函数至多报告一个主错误，继续检查其他函数。
地址来源分析只产生 warning，不把普通指针当作独占借用，也不证明内存安全。
类型、写权限和资源所有权错误仍然阻止编译。标准库接口有明确白名单。
"""
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from itertools import product
from typing import Any, NoReturn

from .formatting import format_argument_issue, format_field_issue, parse_format_template
from .parser import parse_source
from .patterns import SelectorCase, covered, literal_key
from .semantic_globals import GlobalChecker
from .semantic_sync import SYNC_MARKERS, SYNC_TYPES, SyncChecker
from .semantic_traits import TraitChecker, TraitImplementation
from .source import Diagnostic, Source
from .static_values import StaticValueError, integer_limits, scalar_static_value
from .stdlib_env import env_function
from .stdlib_io import IO_TYPE_ALIASES, io_function, normalize_io_name
from .stdlib_iter import FROM_FN, callback_type, next_result
from .typesys import (
    BOOL,
    CONVERSION_ERROR,
    FILE,
    I32,
    INT_LITERAL,
    IO_ERROR,
    NEVER,
    NONE,
    NUMERIC,
    PRIMITIVES,
    STANDARD,
    STR,
    STRING,
    UNIT,
    UNKNOWN,
    USIZE,
    Type,
    callable_type,
    has_unsafe,
    mark_unsafe,
    maybe,
    merge_unsafe,
    ptr,
    substitute,
)

Node = dict[str, Any]

# 预置枚举沿用用户枚举的检查、匹配和布局规则，不进入用户源码 AST。
# 从合法 Xe 声明生成节点，避免手写另一套与解析器漂移的 AST 形状。
STEP_DECLARATION = parse_source("enum[T] Step { Item[T], Stop, }", "<builtin Step>")["items"][0]


@dataclass
class Value:
    type: Type
    node: Node
    place: tuple[int, tuple[str, ...]] | None = None
    # (拥有者 uid, 是否可写)。str/切片/闭包也可以携带这些来源。
    origins: tuple[tuple[int, bool], ...] = ()
    # 历史内部名字：表示经下标/指针/地址捕获访问原有存储，不提供资源
    # 移出权限。它不是用户可见的“借用类型”，也不把 T 偷换成 T@。
    borrowed: bool = False
    access_uid: int | None = None
    literal: Any = None
    # 对被指向的存储位置的写权限，与指针变量自身是否可重绑无关。
    writable: bool | None = None
    # 待定整数表达式中的原始字面量，待上下文确定数值类型后逐个检查。
    literal_sources: tuple[tuple[Node, int], ...] = ()


@dataclass
class Binding:
    uid: int
    name: str
    type: Type
    node: Node
    mutable: bool = False
    initialized: bool = True
    moved: bool = False
    moved_fields: set[tuple[str, ...]] = dataclass_field(default_factory=set)
    origins: tuple[tuple[int, bool], ...] = ()
    last_use: int = -1
    parameter: bool = False
    # 借用捕获的环境字段是 T@，正文中的名字却是指向外部 T 的别名。
    capture_borrowed: bool = False
    capture_writable: bool = False


@dataclass
class Signature:
    node: Node
    parameters: list[Type]
    result: Type
    generics: set[str] = dataclass_field(default_factory=set)
    self_type: Type | None = None
    # None 表示尚未分析/外部函数，保守依赖所有借用输入。
    # 本地函数检查后记录返回结果依赖的参数位置，包含错误路径。
    borrow_parameters: frozenset[int] | None = None
    unsafe_result: bool = False
    type_substitutions: dict[str, Type] = dataclass_field(default_factory=dict)
    instance_context: str | None = None


@dataclass
class ClosureInfo:
    """闭包是具体环境加函数；侧表供后端布局、调用和资源清理使用。"""
    node: Node
    captures: tuple[tuple[str, Type], ...]
    parameters: tuple[Type, ...]
    result: Type
    mode: str = "read"
    borrow_parameters: frozenset[int] = frozenset()
    borrow_captures: frozenset[int] = frozenset()
    storage_captures: frozenset[int] = frozenset()
    unsafe_result: bool = False
    # 不是普通写入：只记录替换资源/缓冲区重分配，数值更新不在这里。
    invalidated_captures: frozenset[int] = frozenset()
    invalidated_external_captures: frozenset[int] = frozenset()


def names_used(node: Any) -> dict[str, int]:
    """只统计真正的名字引用与捕获，不把字段名/声明名当成使用。"""
    result = {}
    def visit(value):
        if isinstance(value, dict):
            name = None
            if value.get("kind") == "Name" and len(value["path"]["parts"]) == 1:
                name = value["path"]["parts"][0]
            if value.get("kind") == "Capture":
                name = value["name"]
            if name:
                result[name] = max(result.get(name, -1), value["span"]["start"]["offset"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(node)
    return result


class Checker(TraitChecker, SyncChecker, GlobalChecker):
    def __init__(self, source: Source, tree: Node, check_borrows: bool = True):
        # 旧参数仅为调用兼容保留；写权限和所有权始终检查，不再切换借用模式。
        self.source, self.tree, self.check_borrows = source, tree, True
        self.types: dict[str, Node] = {"Step": STEP_DECLARATION}
        # 透明别名单独保存，不能让后端把它们当成新的数据布局。
        self.aliases: dict[str, Node] = {}
        self.alias_types: dict[str, Type] = {}
        self._alias_stack: list[str] = []
        self.destructure_types: dict[int, Type] = {}
        self.functions: dict[str, Signature] = {}
        self.methods: dict[tuple[str, str], Signature] = {}
        self.constants: dict[str, Value] = {}
        # 静态存储与函数局部存储分开：没有函数退出 Drop，也不随调用重建。
        # 每个全局有稳定 uid，地址来源分析不能把它误当成返回的栈地址。
        self.globals: dict[str, Binding] = {}
        self.static_roots: set[int] = set()
        self.drop_types: set[str] = set()
        self.copy_types: set[str] = set()
        self.trait_implementations: dict[str, list[TraitImplementation]] = {}
        self.trait_assumptions: set[tuple[Type, Type]] = set()
        self.drop_instances: dict[Type, Signature] = {}
        self._drop_type_visits: set[Type] = set()
        self._traits_ready = False
        self.scopes: list[dict[str, Binding]] = []
        self.uid = 0
        self.result = UNIT
        self.last_uses: dict[str, int] = {}
        self.generic_names: set[str] = set()
        self.copy_generics: set[str] = set()
        self.self_type: Type | None = None
        self.loop_depths: list[int] = []
        self.position = 0
        self.diagnostics: list[Diagnostic] = []
        self.warnings: list[Diagnostic] = []
        self.inferred_types: dict[int, dict[str, Any]] = {}
        self._warning_keys: set[tuple] = set()
        self._report_warnings = True
        self.invalid_roots: dict[int, str] = {}
        # 调用参数中的临时借用也须互斥，不能因为没赋给名字就逃过检查。
        self.temporary_loans: list[tuple[int, bool]] = []
        # 输入所携带的外部区域，区别于“参数变量本身”的栈存储。
        self.parameter_roots: set[int] = set()
        self.return_origins: set[tuple[int, bool]] = set()
        self.function_unsafe_return = False
        # 泛型声明是模板，不是可以直接生成机器码的函数。每个具体类型组合
        # 只复制一次 AST；调用侧表让后端找到这份已经重新检查的具体函数。
        self.call_targets: dict[int, str] = {}
        self.generic_instances: dict[tuple, str] = {}
        self.implementation_constraints: dict[int, list[Node]] = {}
        self.instance_limit = 256
        self.type_substitutions: dict[str, Type] = {}
        self.closures: dict[Type, ClosureInfo] = {}
        self.closure_call_modes: dict[int, str] = {}
        self._closure_bindings: dict[int, int] = {}
        self._closure_mode = "read"
        self._closure_invalidated: set[int] = set()
        self._closure_external_invalidated: set[int] = set()
        self._closure_external_roots: dict[int, set[int]] = {}
        # for 的具体 next 方法/适配器由检查器决定，后端不猜测协议。
        self.for_iterators: dict[int, Signature | str | None] = {}

    @staticmethod
    def has_variable(type_):
        return type_.name.startswith("$") or any(Checker.has_variable(t) for t in type_.args)

    def type_node(self, type_, span):
        """把已解析的具体类型放回复制的 AST；原始源码 AST 始终不修改。"""
        if type_.name == "closure":
            # 仅实例化复制的内部 AST 会出现；用户 AST JSON 不添加隐藏类型。
            return {"kind": "InternalResolvedType", "span": span, "resolved_type": type_}
        if type_.name == "ptr":
            modifiers = [{"kind": "Modifier", "span": span, "name": name}
                         for name in (["mut"] if type_.mutable else []) + (["unsafe"] if type_.unsafe else [])]
            return {"kind": "PointerType", "span": span,
                    "target": self.type_node(type_.args[0], span), "modifiers": modifiers}
        if type_.name == "maybe":
            error = None if type_.args[1] == NONE else self.type_node(type_.args[1], span)
            return {"kind": "MaybeType", "span": span,
                    "value": self.type_node(type_.args[0], span), "error": error}
        if type_.name == "fn":
            return {"kind": "FunctionType", "span": span,
                    "parameters": [self.type_node(t, span) for t in type_.args[:-1]],
                    "result": self.type_node(type_.args[-1], span)}
        if type_.name == "tuple":
            return {"kind": "TupleType", "span": span,
                    "elements": [self.type_node(t, span) for t in type_.args]}
        if type_.name.isdigit():
            return {"kind": "Literal", "span": span, "value": int(type_.name), "literal_kind": "integer"}
        name = type_.name[1:] if type_.name.startswith("$") else type_.name
        return {"kind": "NamedType", "span": span,
                "path": {"kind": "Path", "span": span, "parts": name.split("::")},
                "arguments": [self.type_node(t, span) for t in type_.args]}

    def expression_type(self, node):
        """[] 在类型应用位置内使用类型语法，不把 i32@ 当作取变量地址。"""
        kind, span = node["kind"], node["span"]
        if kind in {"NamedType", "PointerType", "MaybeType", "TupleType", "FunctionType", "InternalResolvedType"}:
            return self.type_of(node)
        if kind == "Name":
            if len(node["path"]["parts"]) == 1:
                name = node["path"]["parts"][0]
                if "$" + name in self.type_substitutions:
                    return self.type_substitutions["$" + name]
                if name == "Self" and self.self_type is not None:
                    return self.self_type
            return self.type_of({"kind": "NamedType", "span": span,
                                 "path": node["path"], "arguments": []})
        if kind == "Group":
            return self.expression_type(node["expression"])
        if kind == "MaybeTypeAttachment":
            error = self.type_of(node["error"]) if node.get("error") else NONE
            return maybe(self.expression_type(node["operand"]), error)
        if kind == "Borrow":
            modifiers = {m["name"] for m in node["modifiers"]}
            if modifiers - {"mut", "unsafe"}:
                self.fail(node, "类型参数中的指针附件只支持 mut / unsafe")
            return ptr(self.expression_type(node["operand"]), "mut" in modifiers, "unsafe" in modifiers)
        if kind == "BracketApply" and node["object"]["kind"] == "Name":
            return self.type_of({"kind": "NamedType", "span": span, "path": node["object"]["path"],
                "arguments": [self.type_node(self.expression_type(a), a["span"])
                              if a["kind"] != "Literal" else a for a in node["arguments"]]})
        if kind == "Tuple":
            return Type("tuple", tuple(self.expression_type(n) for n in node["elements"]))
        if kind == "Literal" and isinstance(node["value"], int) and node["value"] >= 0:
            return Type(str(node["value"]))
        self.fail(node, "这里需要具体类型参数（例如 i32、String、i32@ 或 Holder[i32]）",
                  "XE-GENERIC-0001")

    def specialize_node(self, node, substitutions, self_type=None) -> Any:
        """递归替换类型及类型限定路径，不替换同名的普通值变量。"""
        if isinstance(node, list):
            return [self.specialize_node(n, substitutions, self_type) for n in node]
        if not isinstance(node, dict):
            return node
        if node.get("kind") == "NamedType":
            parts = node["path"]["parts"]
            target = substitutions.get("$" + parts[0]) if len(parts) == 1 else None
            if parts == ["Self"]:
                target = self_type
            if target is not None:
                return self.derived_node(self.type_node(target, node["span"]), node)
        result = {key: self.specialize_node(value, substitutions, self_type)
                  for key, value in node.items()}
        # T::try_from 和 Self::new 的 T/Self 是类型限定名，不能留下模板名。
        if node.get("kind") == "Name":
            parts = node["path"]["parts"]
            target = substitutions.get("$" + parts[0])
            if parts[0] == "Self":
                target = self_type
            if target is not None and len(parts) > 1:
                owner = self.type_expression(target, node["span"])
                for member in parts[1:]:
                    owner = {"kind": "AssociatedAccess", "span": node["span"],
                             "object": owner, "member": member}
                return self.derived_node(owner, node)
            # 普通 Name T 可能是同名值变量/数组索引，不能盲替换。具体实例
            # 保存 type_substitutions，仅在 expression_type 的类型上下文读取。
        return result

    def derived_node(self, generated, original):
        """实例化得到的新节点仍属于模板文件，不能丢失可见性和诊断来源。"""
        if isinstance(generated, dict):
            if "kind" in generated:
                for key in ("_file", "_module"):
                    if key in original:
                        generated[key] = original[key]
            for key, child in generated.items():
                if key not in {"span", "_file", "_module"}:
                    self.derived_node(child, original)
        elif isinstance(generated, list):
            for child in generated:
                self.derived_node(child, original)
        return generated

    def type_expression(self, type_, span):
        if type_.name == "ptr":
            return {"kind": "Borrow", "span": span, "operand": self.type_expression(type_.args[0], span),
                    "modifiers": self.type_node(type_, span)["modifiers"]}
        if type_.name == "tuple":
            return {"kind": "Tuple", "span": span,
                    "elements": [self.type_expression(t, span) for t in type_.args]}
        if type_.name in {"maybe", "fn", "closure"}:
            return self.type_node(type_, span)
        result = {"kind": "Name", "span": span,
                  "path": {"kind": "Path", "span": span, "parts": type_.name.split("::")}}
        if type_.args:
            result = {"kind": "BracketApply", "span": span, "object": result,
                      "arguments": [self.type_expression(t, span) for t in type_.args]}
        return result

    def unify_generic(self, pattern, actual, substitutions, node):
        # 不返回的表达式没有可观察值，不能据此把未知 T 定成 Never。
        # 后续实参仍可推导 T；只有这种实参时要求显式填写具体类型。
        if actual == NEVER:
            return
        if pattern.name.startswith("$"):
            actual = self.default(actual)
            previous = substitutions.get(pattern.name)
            if previous is not None and previous != actual:
                self.fail(node, f"泛型 {pattern.name[1:]} 被推导为 {previous} 和 {actual}", "XE-GENERIC-0001")
            substitutions[pattern.name] = actual
        elif pattern.name == actual.name and len(pattern.args) == len(actual.args):
            for p, a in zip(pattern.args, actual.args):
                self.unify_generic(p, a, substitutions, node)

    def explicit_substitutions(self, signature, arguments, node):
        generics = signature.node.get("generics", [])
        if len(arguments) != len(generics):
            self.fail(node, f"需要 {len(generics)} 个函数类型参数，实际为 {len(arguments)} 个", "XE-GENERIC-0001")
        return {"$" + g["name"]: self.expression_type(a) for g, a in zip(generics, arguments)}

    def instantiate(self, signature, substitutions, node):
        if not signature.generics:
            self.check_trait_constraints(signature, substitutions, node)
            return signature
        missing = sorted(g for g in signature.generics if "$" + g not in substitutions)
        if missing:
            self.fail(node, "无法推导泛型 " + ", ".join(missing) + "；请显式填写函数类型参数", "XE-GENERIC-0001")
        self.check_trait_constraints(signature, substitutions, node)
        # unsafe 是调用处的地址风险，不是实例身份。缓存不能把首次调用的
        # 风险留在签名内，污染下一次安全地址的调用；显式模板注记仍保留。
        def canonical(type_):
            return Type(type_.name, tuple(canonical(t) for t in type_.args), type_.mutable, type_.identity)
        substitutions = {name: canonical(type_) for name, type_ in substitutions.items()}
        def depth(type_):
            return 1 + max((depth(t) for t in type_.args), default=0)
        if any(depth(t) > 64 for t in substitutions.values()):
            self.fail(node, "泛型类型嵌套超过 64 层；可能存在不断增加类型层数的递归实例化",
                      "XE-GENERIC-0002")
        key = (id(signature.node), tuple((g, substitutions["$" + g]) for g in sorted(signature.generics)))
        if key in self.generic_instances:
            return self.functions[self.generic_instances[key]]
        if len(self.generic_instances) >= self.instance_limit:
            self.fail(node, f"泛型实例超过 {self.instance_limit} 个；可能存在不断增加类型层数的递归实例化",
                      "XE-GENERIC-0002", "检查递归调用是否将 T 改成 Holder[T] 等越来越大的类型")
        concrete_self = substitute(signature.self_type, substitutions) if signature.self_type else None
        concrete_node = self.specialize_node(signature.node, substitutions, concrete_self)
        name = f"__xe_generic_{len(self.generic_instances) + 1}"
        while name in self.functions:
            name += "_"
        concrete_node["name"], concrete_node["generics"], concrete_node["constraints"] = name, [], []
        concrete = Signature(concrete_node, [substitute(t, substitutions) for t in signature.parameters],
                             substitute(signature.result, substitutions), self_type=concrete_self,
                             borrow_parameters=frozenset() if concrete_node["body"] is not None else None,
                             type_substitutions=substitutions)
        position = node["span"]["start"]
        arguments = ", ".join(f"{g} = {self.display_type(substitutions['$' + g])}"
                              for g in sorted(signature.generics))
        concrete.instance_context = (f"检查 {self.display_type(signature.node['name'])} 的具体实例（{arguments}）；"
            f"首次使用于 {self.source_of(node).filename}:{position['line']}:{position['column']}")
        self.generic_instances[key] = name
        self.functions[name] = concrete
        return concrete

    def fail(self, node: Node, message: str, code="XE-TYPE-0001", hint=None) -> NoReturn:
        span = node["span"]
        for internal, display in sorted(self.tree.get("_display_names", {}).items(), key=lambda item: -len(item[0])):
            message = message.replace(internal, display)
            if hint:
                hint = hint.replace(internal, display)
        raise Diagnostic(self.source_of(node), span["start"]["offset"], span["end"]["offset"],
                         message, code, hint)

    def source_of(self, node):
        filename = node.get("_file") or node.get("path", {}).get("_file")
        return self.tree.get("_sources", {}).get(filename, self.source)

    def member_access(self, member, node):
        """单文件仍沿用原规则；加载的模块检查私有字段/方法。"""
        from .modules import accessible
        if "_module" in member and "_module" in node:
            if not accessible(member["_module"], node["_module"], member.get("public", False)):
                self.fail(node, f"成员 {member['name']} 是私有的", "XE-MODULE-0001",
                          "在定义处添加 pub，或通过公开方法访问")

    def offset(self, node: Node):
        return node["span"]["start"]["offset"]

    def record_type(self, node, type_, name=None):
        """语义侧表单独输出；原始 AST 仍只描述源码，不伪造 unsafe 附件。"""
        self.ensure_drop_type(type_, node)
        entry = {"kind": node["kind"], "span": node["span"],
                 "type": self.display_type(type_), "unsafe": has_unsafe(type_)}
        if node.get("_file"):
            entry["file"] = node["_file"]
        if name is not None:
            entry["name"] = name
        previous = self.inferred_types.get(id(node))
        if previous and previous.get("unsafe") and not entry["unsafe"]:
            return
        self.inferred_types[id(node)] = entry

    def display_type(self, type_):
        rendered = str(type_)
        names = self.tree.get("_display_names", {})
        for internal in sorted(names, key=len, reverse=True):
            rendered = rendered.replace(internal, names[internal])
        return rendered

    def warn_pointer(self, value, message, code="XE-PTR-0001"):
        """风险注记不改变 ABI，不升级为错误，也不延长对象的生存时间。"""
        if self.carries_borrow(value.type):
            value.type = mark_unsafe(value.type)
            self.record_type(value.node, value.type)
            if value.access_uid is not None:
                binding = self.by_uid(value.access_uid)
                if binding and self.carries_borrow(binding.type):
                    self.mark_binding_unsafe(binding)
        span = value.node["span"]
        key = (code, span["start"]["offset"], span["end"]["offset"], message, self.source_of(value.node).filename)
        if self._report_warnings and key not in self._warning_keys:
            self._warning_keys.add(key)
            self.warnings.append(Diagnostic(self.source_of(value.node), key[1], key[2], message, code,
                "已自动标记 unsafe；仍可编译。请确保访问时地址、对象及边界有效。",
                severity="warning", inferred_type=self.display_type(value.type)))
        return value

    def mark_binding_unsafe(self, binding):
        binding.type = mark_unsafe(binding.type)
        self.record_type(binding.node, binding.type, binding.name)
        # C 后端的声明侧表也更新；风险不改变底层类型或布局。
        binding_types: dict[int, Type] | None = getattr(self, "binding_types", None)
        if binding_types is not None:
            binding_types[id(binding.node)] = binding.type

    def invalidate_storage(self, uid, node, reason, exclude_uids=()):
        """移动、替换或作用域结束可能使地址失效；别名本身并不是错误。"""
        if uid in self._closure_bindings:
            self._closure_invalidated.add(self._closure_bindings[uid])
        self._closure_external_invalidated.update(self._closure_external_roots.get(uid, ()))
        self.invalid_roots[uid] = reason
        for binding in self.bindings().values():
            if (binding.uid != uid and binding.uid not in exclude_uids and binding.initialized and not binding.moved
                    and binding.last_use >= self.offset(node)
                    and any(root == uid for root, _ in binding.origins)
                    and self.carries_borrow(binding.type)):
                self.mark_binding_unsafe(binding)
                self.warn_pointer(Value(binding.type, node, origins=binding.origins),
                    f"{binding.name} 指向的存储{reason}，后续访问可能失效", "XE-PTR-0002")

    def generic_set(self, node):
        return {p["name"] for p in node.get("generics", [])}

    def resolve_alias(self, name):
        if name in self.alias_types:
            return self.alias_types[name]
        declaration = self.aliases[name]
        if name in self._alias_stack:
            cycle = " -> ".join(self._alias_stack[self._alias_stack.index(name):] + [name])
            self.fail(declaration, f"类型别名循环引用：{cycle}", "XE-TYPE-0007",
                      "透明别名必须最终指向已定义的具体类型")
        if len(self._alias_stack) >= 128:
            self.fail(declaration, "类型别名引用链过深，请缩短别名链", "XE-TYPE-0007")
        self._alias_stack.append(name)
        # 别名只在模块作用域解析，不能捕获调用位置的 Self 或泛型参数。
        saved = self.generic_names, self.self_type, self.type_substitutions
        self.generic_names, self.self_type, self.type_substitutions = set(), None, {}
        try:
            result = self.type_of(declaration["type"], set())
            self.alias_types[name] = result
            return result
        finally:
            self.generic_names, self.self_type, self.type_substitutions = saved
            self._alias_stack.pop()

    def associated_owner(self, name, node):
        """给后端及关联调用解析源码中的类型限定名称，别名返回具体类型。"""
        return self.type_of({"kind": "NamedType", "span": node["span"],
            "path": {"kind": "Path", "span": node["span"], "parts": name.split("::")},
            "arguments": []})

    def type_of(self, node: Node | None, generics=None, self_type=None) -> Type:
        if node is not None and node.get("kind") == "InternalResolvedType":
            return node["resolved_type"]
        if node is None:
            return UNIT
        generics = self.generic_names if generics is None else generics
        self_type = self.self_type if self_type is None else self_type
        kind = node["kind"]
        if kind == "NamedType":
            name = "::".join(node["path"]["parts"])
            if name == "Self":
                if self_type is None:
                    self.fail(node, "Self 只能用于 impl/Trait 上下文", "XE-NAME-0001")
                return self_type
            if name in generics:
                return Type("$" + name)
            if name in self.aliases:
                if node["arguments"]:
                    self.fail(node, f"类型别名 {name} 不接受类型附件；泛型别名暂不支持", "XE-GENERIC-0001")
                return self.resolve_alias(name)
            if name in IO_TYPE_ALIASES:
                if node["arguments"]:
                    self.fail(node, f"{name} 不接受类型参数", "XE-GENERIC-0001")
                return IO_TYPE_ALIASES[name]
            if name not in PRIMITIVES | STANDARD and name not in self.types:
                self.fail(node, f"未知类型 {name}", "XE-NAME-0001")
            if name in {"Map", "Set", "Iterator", "Formatter"} and name not in self.types:
                self.fail(node, f"当前版本尚未实现内建 {name} 类型的运行布局与接口；此声明仅属于后续目标能力",
                          "XE-SEM-0001", "目前可使用已实现的 Vec/Box/Step 接口，或定义自己的模块类型；不是语言设计上禁止此类类型")
            if name in self.types and self.types[name]["kind"] == "Trait":
                self.fail(node, f"{name} 是 Trait 约束，不是具体值类型；动态 Trait 对象尚未设计",
                          "XE-SEM-0001")
            args = []
            for arg in node["arguments"]:
                if arg["kind"] == "Literal":
                    args.append(Type(str(arg["value"])))
                else:
                    args.append(self.type_of(arg, generics, self_type))
            arities = {"Array": 2, "Vec": 1, "Slice": 1, "SliceMut": 1,
                       "Box": 1, "Map": 2, "Set": 1, "Range": 1, "Iterator": 1, "FromFn": 1, "Step": 1,
                       **{name: 1 for name in SYNC_TYPES}}
            arity = len(self.types[name].get("generics", [])) if name in self.types else arities.get(name, 0)
            if len(args) != arity:
                self.fail(node, f"{name} 需要 {arity} 个类型附件，实际为 {len(args)} 个")
            if name == "Array" and not args[1].name.isdigit():
                self.fail(node, "Array 的第二个附件必须是非负整数长度")
            if name == "FromFn" and not self.has_variable(args[0]):
                self.iterator_callback_result(args[0], node)
            return Type(name, tuple(args))
        if kind == "PointerType":
            base = self.type_of(node["target"], generics, self_type)
            modifiers = [m["name"] for m in node["modifiers"]]
            if len(modifiers) != len(set(modifiers)):
                self.fail(node, "指针修饰符重复")
            if any(m not in {"mut", "unsafe"} | set(generics) for m in modifiers):
                self.fail(node, "未知指针修饰符")
            return ptr(base, "mut" in modifiers, "unsafe" in modifiers)
        if kind == "MaybeType":
            # None 是无负载结果标记，只在问号的错误附件内具有类型语法身份。
            # 它不是普通存储类型，不能通过 fn(x: None) 引入伪造的值类型。
            error_node = node["error"]
            error = (NONE if error_node is None or error_node["kind"] == "NoneTypeMarker"
                     else self.type_of(error_node, generics, self_type))
            return maybe(self.type_of(node["value"], generics, self_type), error)
        if kind == "NoneTypeMarker":
            self.fail(node, "None 不是普通值类型；仅作为 T? / T?[None] 的无负载标记",
                      "XE-RESULT-0001")
        if kind == "FunctionType":
            return callable_type([self.type_of(p, generics, self_type) for p in node["parameters"]],
                                 self.type_of(node["result"], generics, self_type))
        if kind == "TupleType":
            return Type("tuple", tuple(self.type_of(t, generics, self_type) for t in node["elements"]))
        self.fail(node, f"尚未支持的类型结构 {kind}", "XE-SEM-0001")

    def copyable(self, type_: Type, visited=None) -> bool:
        if type_.name in PRIMITIVES | {"$integer", "fn", "None", "ConversionError", "AllocError"} | SYNC_MARKERS:
            return True
        if type_.name in self.copy_generics:
            return True
        if type_.name == "ptr":
            # 指针复制的是地址，不是所指对象；可写指针也不是独占借用。
            # 可写/unsafe 注记只控制访问权限或提示风险，不能改变 Copy。
            return True
        if type_.name in {"tuple", "Array", "maybe", "Step"}:
            return all(self.copyable(arg, visited) for arg in type_.args
                       if not arg.name.isdigit())
        if type_.name in {"Slice", "SliceMut", "Range", "Bytes", "Chars"}:
            return True
        if (type_, Type("Copy")) in self.trait_assumptions:
            return True
        return self.find_trait_implementation(type_, Type("Copy"), visited) is not None

    def carries_borrow(self, type_: Type, visited=None) -> bool:
        if type_.name in {"ptr", "str", "Slice", "SliceMut", "closure", "Bytes", "Chars"}:
            return True
        if type_.name in (visited or set()):
            return False
        if type_.name in {"maybe", "tuple", "Array", "FromFn", "Step", "Vec", "Box"} | SYNC_TYPES:
            return any(self.carries_borrow(t, visited) for t in type_.args)
        if type_.name not in self.types:
            return type_.name.startswith("$") and type_ not in {INT_LITERAL, UNKNOWN}
        visited = (visited or set()) | {type_.name}
        declaration = self.types[type_.name]
        substitutions = {"$" + p["name"]: t for p, t in
                         zip(declaration.get("generics", []), type_.args)}
        fields = ([f["type"] for f in declaration.get("fields", [])] +
                  [t for v in declaration.get("variants", []) for t in v["payload"]])
        # 纯值结构体/枚举不因经过 self 指针而变成借用值。递归检查真实字段；
        # 指针在递归之前返回 True，visited 仅防止非法按值循环导致 Python 递归。
        for field in fields:
            field_type = substitute(self.type_of(field, self.generic_set(declaration)), substitutions)
            if field_type.name not in visited and self.carries_borrow(field_type, visited):
                return True
        return False

    def signature(self, node, self_type=None, extra_generics=None):
        if any(p["category"] == "region" for p in node.get("generics", [])):
            self.fail(node, "命名区域求解尚未实现；目前支持单文件基础借用检查", "XE-SEM-0001")
        generics = self.generic_set(node) | (extra_generics or set())
        signature = Signature(node,
            [self.type_of(p["type"], generics, self_type) for p in node["parameters"]],
            self.type_of(node["result"], generics, self_type), generics, self_type)
        if self_type is not None and node["parameters"] and node["parameters"][0]["name"] == "self":
            receiver = signature.parameters[0]
            target = receiver.args[0] if receiver.name == "ptr" else receiver
            if target != self_type:
                self.fail(node["parameters"][0], f"方法 self 类型必须是所属类型 {self_type} 或其一层指针，实际为 {receiver}",
                          "XE-TYPE-0001", "使用 self: Self、self: Self@ 或 self: Self@[mut]")
        return signature

    def collect(self):
        seen = set()
        for node in self.tree["items"]:
            if node["kind"] in {"Struct", "Enum", "Trait", "Function", "Constant", "TypeAlias", "GlobalBinding"}:
                name = node["name"]
                if name in seen:
                    self.fail(node, f"顶层名称 {name} 重复", "XE-NAME-0002")
                seen.add(name)
                if node["kind"] in {"Struct", "Enum", "Trait"}:
                    if name in {"FromFn", "Step", "Vec", "Box", "AllocError", "Bytes", "Chars"} | SYNC_TYPES | SYNC_MARKERS:
                        self.fail(node, f"{name} 是内建标准类型，不能重新定义", "XE-NAME-0002")
                    self.types[name] = node
                if node["kind"] == "TypeAlias":
                    if name in PRIMITIVES | STANDARD | {"Self", "None", "Maybe"}:
                        self.fail(node, f"类型别名不能重定义内建类型名称 {name}", "XE-NAME-0002")
                    self.aliases[name] = node
        for external in self.tree["items"]:
            if external["kind"] == "Extern":
                for function in external["functions"]:
                    if function["name"] in seen:
                        self.fail(function, f"顶层名称 {function['name']} 重复", "XE-NAME-0002")
                    seen.add(function["name"])
        for name in self.aliases:
            self.resolve_alias(name)
        self.collect_traits()
        for node in self.tree["items"]:
            kind = node["kind"]
            if kind == "Function":
                self.functions[node["name"]] = self.signature(node)
            elif kind == "Extern":
                for function in node["functions"]:
                    if function["name"] in self.functions:
                        self.fail(function, "extern 函数名重复", "XE-NAME-0002")
                    self.functions[function["name"]] = self.signature(function)
            elif kind == "Impl":
                target = self.type_of(node["target"], self.generic_set(node))
                trait = "::".join(node["trait"]["path"]["parts"]) if node["trait"] else None
                if trait == "Drop":
                    if target.name not in self.types or len(node["methods"]) != 1 or node["methods"][0]["name"] != "drop":
                        self.fail(node, "Drop 仅用于用户类型，且必须定义唯一的 drop 方法", "XE-OWN-0002")
                    drop = self.signature(node["methods"][0], target, self.generic_set(node))
                    if len(drop.parameters) != 1 or not self.compatible(drop.parameters[0], ptr(target, self.check_borrows)) or drop.result != UNIT:
                        self.fail(node, "drop 签名必须为 fn drop(self: Self@[mut])", "XE-OWN-0002")
                if trait == "Copy" and node["methods"]:
                    self.fail(node, "Copy 是无方法标记 Trait", "XE-OWN-0001")
                for method in node["methods"]:
                    self.register_impl_method(node, target, method)
            elif kind == "Use":
                self.fail(node, "当前语义阶段只检查单文件，尚未加载 use 依赖", "XE-SEM-0001",
                          "将相关声明放在同一文件，或等待模块加载阶段")
        # 验证用户数据字段，不能把未知字段类型留给函数体来猜。
        for node in self.types.values():
            if node["kind"] == "Struct":
                fields = node["fields"]
                if len({f["name"] for f in fields}) != len(fields):
                    self.fail(node, "结构体字段重复", "XE-NAME-0002")
                for field_node in fields:
                    self.type_of(field_node["type"], self.generic_set(node))
            if node["kind"] == "Enum":
                variants = node["variants"]
                if len({v["name"] for v in variants}) != len(variants):
                    self.fail(node, "枚举变体重复", "XE-NAME-0002")
                for variant in variants:
                    for payload in variant["payload"]:
                        self.type_of(payload, self.generic_set(node))
        for implementations in self.trait_implementations.values():
            for implementation in implementations:
                self.validate_trait_implementation(implementation)
        # 契约自身也必须合法，不能等到函数被调用才发现未知 Trait/类型。
        for signature in list(self.functions.values()) + list(self.methods.values()):
            for constraint in signature.node.get("constraints", []) + self.implementation_constraints.get(id(signature), []):
                self.constraint_types(constraint, signature.generics, signature.self_type, {})
        self.validate_copy_implementations()
        self.validate_layouts()
        self._traits_ready = True
        for signature in list(self.functions.values()) + list(self.methods.values()):
            for type_ in signature.parameters + [signature.result]:
                self.ensure_drop_type(type_, signature.node)

    def validate_layouts(self):
        """按值递归会要求无限存储；在前端给出源码位置，不留给 C 报错。

        @、Box、Vec 等间接容器不展开目标存储。这里检查声明之间的直接
        布局依赖；尚未实现的泛型实例布局仍由泛型阶段负责。
        """
        declarations = {name: node for name, node in self.types.items()
                        if node["kind"] in {"Struct", "Enum"}}

        def dependencies(type_):
            if type_.name == "Step":
                # Step[T] 内联保存 T，必须发现 Node -> Step[Node] 的递归布局。
                for argument in type_.args:
                    yield from dependencies(argument)
            elif type_.name in declarations:
                yield type_.name
            elif type_.name in {"maybe", "tuple", "Array"}:
                for argument in type_.args:
                    yield from dependencies(argument)

        graph = {}
        for name, node in declarations.items():
            fields = ([f["type"] for f in node.get("fields", [])] +
                      [t for v in node.get("variants", []) for t in v["payload"]])
            graph[name] = [(target, field) for field in fields
                           for target in dependencies(self.type_of(field, self.generic_set(node)))]
        done, visiting = set(), []

        def visit(name):
            if name in done:
                return
            visiting.append(name)
            for target, field in graph[name]:
                if target in visiting:
                    cycle = " -> ".join(visiting[visiting.index(target):] + [target])
                    self.fail(field, f"按值递归需要无限存储：{cycle}", "XE-TYPE-0006",
                              "用 T@ 借用指向另一对象，或使用间接拥有容器")
                visit(target)
            visiting.pop()
            done.add(name)

        for name in declarations:
            visit(name)

    def check(self) -> list[Diagnostic]:
        try:
            self.collect()
            from .ffi import validate_externals
            validate_externals(self)
            # 先注册所有全局类型/位置，再检查初值，允许静态地址引用后声明对象。
            for node in self.tree["items"]:
                if node["kind"] not in {"GlobalBinding", "Constant"}:
                    continue
                type_ = self.type_of(node["type"])
                if not self.copyable(type_):
                    if node["kind"] == "Constant":
                        self.fail(node, "当前模块级只读绑定只支持可复制值；资源请在函数内创建", "XE-SEM-0001")
                    self.fail(node, "当前全局变量只支持 Copy 类型；资源的全局初始化与退出清理尚未实现",
                              "XE-GLOBAL-0001", "把 String、Vec 等资源在函数内创建，通过参数传递")
                self.uid += 1
                # 模块 let 和 let[mut] 都拥有静态存储，唯一差别是写权限。
                # Constant 保留为兼容已有 AST 的节点名，不再表示无地址的值。
                self.globals[node["name"]] = Binding(
                    self.uid, node["name"], type_, node, mutable=node["kind"] == "GlobalBinding")
                self.static_roots.add(self.uid)
                self.record_type(node, type_, node["name"])
            for node in self.tree["items"]:
                if node["kind"] == "Constant":
                    value = self.infer(node["value"], self.type_of(node["type"]))
                    self.constants[node["name"]] = self.convert(value, self.type_of(node["type"]))
                    if not self.copyable(value.type):
                        self.fail(node, "当前模块级只读绑定只支持可复制值；资源请在函数内创建", "XE-SEM-0001")
            for binding in self.globals.values():
                node = binding.node
                self.static_initializer(node["value"])
                value = self.convert(self.infer(node["value"], binding.type), binding.type)
                self.check_static_numbers(node["value"], binding.type)
                binding.origins = value.origins
                binding.type = value.type
                self.record_type(node, binding.type, binding.name)
        except Diagnostic as error:
            return [error]
        def concrete_signatures():
            return [s for s in list(self.functions.values()) + list(self.methods.values()) if not s.generics]
        signatures = concrete_signatures()
        # 本地函数的输入依赖从空集合开始，只增不减，迭代至稳定。这样互递归
        # 的纯静态错误消息不会被错误归因到 self。外部函数无函数体，始终保守。
        # 这里只计算有限参数依赖，不推导生命周期；最终一遍执行所有逃逸诊断。
        for signature in signatures:
            if signature.node["body"] is not None:
                signature.borrow_parameters = frozenset()
        # 函数数量不能界定传播轮数：单个递归函数可以轮换许多参数，
        # 每一轮才发现下一项来源。摘要只增加参数索引，而参数总数有限，
        # 因此以真正稳定为终止条件既会终止，也不会遗漏晚发现的借用。
        self._report_warnings = False
        while True:
            previous = [(signature.borrow_parameters, signature.unsafe_result) for signature in signatures]
            for signature in signatures:
                if signature.node["body"] is not None:
                    try:
                        self.check_function(signature)
                    except Diagnostic:
                        # 不把探测阶段的错误当作成功；最终遍仍会报告它。
                        pass
            signatures = concrete_signatures()
            if previous == [(signature.borrow_parameters, signature.unsafe_result) for signature in signatures]:
                break
        self._report_warnings = True
        self.inferred_types.clear()
        for signature in signatures:
            if signature.node["body"] is None:
                continue
            try:
                self.check_function(signature)
            except Diagnostic as error:
                if signature.instance_context:
                    error.hint = signature.instance_context + ("。" + error.hint if error.hint else "")
                self.diagnostics.append(error)
        return self.diagnostics

    def check_function(self, signature):
        self.scopes = [{}]
        self.generic_names, self.self_type = signature.generics, signature.self_type
        self.type_substitutions = signature.type_substitutions
        self.copy_generics = set()
        for constraint in signature.node.get("constraints", []):
            if constraint["trait"]["kind"] == "NamedType" and constraint["trait"]["path"]["parts"] == ["Copy"]:
                self.copy_generics.add(self.type_of(constraint["target"]).name)
        self.result = signature.result
        self.last_uses = names_used(signature.node["body"])
        self.loop_depths, self.temporary_loans = [], []
        self.parameter_roots = set(self.static_roots)
        self.return_origins = set()
        self.invalid_roots = {}
        self.function_unsafe_return = False
        parameter_sources = {}
        for index, (parameter, type_) in enumerate(zip(signature.node["parameters"], signature.parameters)):
            binding = self.declare(parameter["name"], type_, parameter, parameter["mutable"], parameter=True)
            if self.carries_borrow(type_):
                self.uid += 1
                self.parameter_roots.add(self.uid)
                parameter_sources[self.uid] = index
                binding.origins = ((self.uid, type_.mutable),)
        result = self.block(signature.node["body"], signature.result, lift=True)
        self.convert(result, signature.result, lift=True)
        self.escape(result, function_exit=True)
        signature.unsafe_result = signature.unsafe_result or has_unsafe(result.type) or self.function_unsafe_return
        self.return_origins.update(result.origins)
        signature.borrow_parameters = (signature.borrow_parameters or frozenset()) | frozenset(
            parameter_sources[uid] for uid, _ in self.return_origins if uid in parameter_sources)

    def call_origins(self, signature, values):
        """只传播实际返回依赖；没有摘要时保守取全部输入来源。"""
        if not self.carries_borrow(signature.result):
            return ()
        return tuple(origin for index, value in enumerate(values)
                     if signature.borrow_parameters is None or index in signature.borrow_parameters
                     for origin in value.origins)

    def call_result_type(self, signature, values, result=None):
        result = result or signature.result
        # 函数值没有捕获地址，也可能携带“调用会返回风险指针”的签名信息。
        if (self.carries_borrow(result) or result.name == "fn") and (signature.unsafe_result or any(
                has_unsafe(value.type) for index, value in enumerate(values)
                if signature.borrow_parameters is None or index in signature.borrow_parameters)):
            return mark_unsafe(result)
        return result

    def declare(self, name, type_, node, mutable=False, initialized=True, origins=(), parameter=False):
        if name in self.scopes[-1]:
            self.fail(node, f"当前作用域中的 {name} 重复", "XE-NAME-0002")
        self.uid += 1
        binding = Binding(self.uid, name, type_, node, mutable, initialized,
                          origins=origins, last_use=self.last_uses.get(name, -1), parameter=parameter)
        self.scopes[-1][name] = binding
        self.record_type(node, type_, name)
        return binding

    def lookup(self, name, node, read=True, partial=False):
        binding = next((scope[name] for scope in reversed(self.scopes) if name in scope), self.globals.get(name))
        if binding is None:
            self.fail(node, f"未定义名称 {name}", "XE-NAME-0001")
        if read:
            if not binding.initialized:
                self.fail(node, f"{name} 可能尚未初始化", "XE-INIT-0001")
            if binding.moved or (binding.moved_fields and not partial):
                self.fail(node, f"{name} 已移动或在某条分支中移动", "XE-MOVE-0001")
        return binding

    def by_uid(self, uid):
        return next((b for scope in self.scopes for b in scope.values() if b.uid == uid),
                    next((b for b in self.globals.values() if b.uid == uid), None))

    def bindings(self):
        return {b.uid: b for scope in self.scopes for b in scope.values()}

    def merge(self, before, alternatives):
        """合并所有可继续路径：初始化取交集，移动/部分移动取并集。"""
        for binding in self.bindings().values():
            states = [state[binding.uid] for state in alternatives if binding.uid in state]
            if states:
                binding.initialized = all(b.initialized for b in states)
                binding.moved = any(b.moved for b in states)
                binding.moved_fields = set().union(*(b.moved_fields for b in states))
                binding.origins = tuple(set().union(*(set(b.origins) for b in states)))
                for state in states:
                    binding.type = merge_unsafe(binding.type, state.type)
                self.record_type(binding.node, binding.type, binding.name)

    def loan(self, origins, node, exclude=None, position=None):
        # 兼容内部调用点。普通指针允许读写别名，不建立独占 loan。
        # 保留地址来源仅用于风险告警；写权限由 mutable_place 单独检查。
        return

    def consume(self, value: Value):
        if self.copyable(value.type) or value.type == NEVER:
            return
        if value.borrowed:
            self.fail(value.node, "该存储访问不提供资源所有权，不能移走资源", "XE-MOVE-0002",
                      "显式传递指针或 clone；Vec 可通过 pop() 取出末尾元素")
        self.note_closure_access(value, "once")
        if value.place:
            uid, fields = value.place
            binding = self.by_uid(uid)
            if binding:
                self.loan(((uid, True),), value.node, exclude=value.access_uid)
                if self.loop_depths and any(binding is b for scope in self.scopes[:self.loop_depths[-1]+1]
                                            for b in scope.values()):
                    self.fail(value.node, "循环内不能反复移动外层资源", "XE-MOVE-0003",
                              "使用借用，或在进入循环前转交给拥有迭代器")
                if fields:
                    ancestor = binding.type
                    for part in fields:
                        if self.has_drop(ancestor):
                            self.fail(value.node, "自定义 Drop 类型不能移出资源字段（包括嵌套字段）", "XE-OWN-0002")
                        if ancestor.name == "tuple":
                            ancestor = ancestor.args[int(part)]
                        else:
                            declaration = self.types.get(ancestor.name, {})
                            field_node = next((f for f in declaration.get("fields", []) if f["name"] == part), None)
                            if field_node:
                                field_type = self.type_of(field_node["type"], self.generic_set(declaration))
                                ancestor = substitute(field_type, {"$"+p["name"]: t for p, t in
                                                      zip(declaration.get("generics", []), ancestor.args)})
                    binding.moved_fields.add(fields)
                else:
                    binding.moved = True
                self.invalidate_storage(uid, value.node, "已移动或部分移动")

    def escape(self, value, leaving=None, function_exit=False):
        leaving = leaving or set()
        for uid, _ in value.origins:
            if uid in leaving or (function_exit and uid not in self.parameter_roots and uid not in self.static_roots):
                self.warn_pointer(value, "地址或视图可能超过所指存储的生存时间", "XE-PTR-0001")
                break
        if function_exit and has_unsafe(value.type):
            self.function_unsafe_return = True

    def default(self, type_):
        return I32 if type_ == INT_LITERAL else type_

    def unresolved(self, type_):
        return type_ == UNKNOWN or any(self.unresolved(t) for t in type_.args)

    def compatible(self, actual, expected, pointer_weakening=True):
        if actual == NEVER or actual == expected or actual == UNKNOWN or expected == UNKNOWN:
            return True
        if actual == INT_LITERAL and expected.name in NUMERIC:
            return True
        if actual.name == expected.name == "ptr":
            # 仅能丢掉当前这一级的写权限。指向类型必须完全相同；
            # 不递归降权，否则 T@[mut]@ -> T@@ 可经二级指针洗白写能力。
            return actual.args == expected.args and (actual.mutable == expected.mutable or
                    pointer_weakening and actual.mutable and not expected.mutable)
        if actual.name == expected.name and len(actual.args) == len(expected.args):
            return actual.identity == expected.identity and actual.mutable == expected.mutable and all(
                self.compatible(a, b, pointer_weakening=False) for a, b in zip(actual.args, expected.args))
        return False

    def convert(self, value, target, lift=False):
        if value.type == NEVER:
            return value
        if lift and target.name == "maybe" and not self.compatible(value.type, target):
            if value.type == NONE:
                if target.args[1] != NONE:
                    self.fail(value.node, "None 不能代替具体错误；使用 Maybe::No[error]", "XE-RESULT-0001")
            else:
                value = self.convert(value, target.args[0])
            lifted = mark_unsafe(target) if has_unsafe(value.type) else target
            return Value(lifted, value.node, value.place, value.origins, value.borrowed, value.access_uid,
                         writable=value.writable)
        if not self.compatible(value.type, target):
            self.fail(value.node, f"类型不匹配：需要 {target}，实际为 {value.type}", "XE-TYPE-0001")
        if value.type == INT_LITERAL and target.name in NUMERIC:
            for source_node, number in value.literal_sources:
                # 用已有的精度和边界检查，避免复合表达式丢掉原字面量。
                self.convert(Value(INT_LITERAL, source_node, literal=number), target)
        if value.type == INT_LITERAL and target.name in NUMERIC:
            literal = value.literal
            if isinstance(literal, int) and target.name in {"f32", "f64"}:
                import math
                import struct
                try:
                    rounded = float(literal)
                    if target.name == "f32":
                        rounded = struct.unpack("f", struct.pack("f", rounded))[0]
                    exact = math.isfinite(rounded) and rounded == literal
                except (OverflowError, struct.error):
                    exact = False
                if not exact:
                    self.fail(value.node, f"整数字面量不能被 {target} 精确表示", "XE-TYPE-0004")
            elif isinstance(literal, int):
                import struct
                bits = struct.calcsize("P") * 8 if target.name in {"isize", "usize"} else int(target.name[1:])
                low = -(1 << (bits-1)) if target.name[0] == "i" else 0
                high = (1 << (bits-1))-1 if target.name[0] == "i" else (1 << bits)-1
                if not low <= literal <= high:
                    self.fail(value.node, f"整数超出 {target} 范围", "XE-TYPE-0004")
        # origins 既描述访问路径，也描述结果携带的借用。复制出纯整数等值后，
        # 访问已结束，不得把原 self 指针借用附到整数变量/后续调用的实参上。
        origins = value.origins if self.carries_borrow(target) else ()
        if (value.borrowed and value.place and value.node["kind"] == "BracketApply"
                and self.copyable(target)):
            owner = self.by_uid(value.place[0])
            if owner and owner.type.name != "ptr":
                # x[i]@ 借用元素描述符，因此依赖数组存储；但复制 x[i] 的
                # str/指针等 Copy 值只保留元素实际携带的数据来源。
                # owner.origins 不能简单删去 owner uid：自指向元素也可能
                # 真实依赖同一数组，赋值时记录的这类来源仍须保留。
                origins = tuple(set(o for o in origins if o[0] != owner.uid) |
                                set(owner.origins)) if self.carries_borrow(target) else ()
        return Value(merge_unsafe(target, value.type), value.node, value.place, origins, value.borrowed,
                     value.access_uid, value.literal, value.writable,
                     value.literal_sources if target == INT_LITERAL else ())

    def common(self, values, node):
        usable = [v for v in values if v.type != NEVER]
        if not usable:
            return Value(NEVER, node)
        type_ = usable[0].type
        for value in usable[1:]:
            if type_ == INT_LITERAL:
                type_ = value.type
            elif type_.name == value.type.name == "ptr" and type_.args == value.type.args:
                # 两个分支权限不同，共同结果取较小权限，与分支顺序无关。
                type_ = ptr(type_.args[0], type_.mutable and value.type.mutable,
                            has_unsafe(type_) or has_unsafe(value.type))
            elif not self.compatible(value.type, type_):
                self.fail(node, f"分支/元素类型不一致：{type_} 与 {value.type}", "XE-TYPE-0001")
            type_ = merge_unsafe(type_, value.type)
        # 推导出具体数值类型后，仍须检查此前遇到的字面量是否能放入该类型。
        # 仅凭 compatible(INT_LITERAL, u8) 会让 [300, u8] 等表达式静默截断。
        if type_ != INT_LITERAL:
            for value in usable:
                if value.type == INT_LITERAL:
                    self.convert(value, type_)
        origins = tuple(set().union(*(set(v.origins) for v in usable)))
        sources = tuple(source for value in usable for source in value.literal_sources)
        return Value(type_, node, origins=origins,
                     literal_sources=sources if type_ == INT_LITERAL else ())

    def block(self, node, expected=None, lift=False):
        self.scopes.append({})
        terminated = False
        for statement in node["statements"]:
            if terminated:
                break
            self.position = self.offset(statement)
            kind = statement["kind"]
            if kind == "Binding":
                annotation = self.type_of(statement["type"]) if statement["type"] else None
                if statement["value"] is None:
                    if annotation is None:
                        self.fail(statement, "延后初始化需要显式类型", "XE-INIT-0001")
                    self.declare(statement["name"], annotation, statement, statement["mutable"], False)
                    continue
                value = self.infer(statement["value"], annotation)
                if annotation:
                    value = self.convert(value, annotation)
                else:
                    value = self.convert(value, self.default(value.type))
                if self.unresolved(value.type):
                    self.fail(statement, "不能确定枚举的成功/错误类型，请添加完整类型注解")
                if statement["operator"] == "=" and not self.copyable(value.type):
                    self.fail(statement, f"{value.type} 不能复制，必须使用 <<", "XE-OWN-0001")
                if statement["operator"] == "<<" and value.type != NEVER and self.copyable(value.type):
                    self.fail(statement, f"{value.type} 是 Copy 值，使用 =；<< 只用于转移所有权", "XE-OWN-0001")
                if statement["operator"] == "<<":
                    self.consume(value)
                self.declare(statement["name"], value.type, statement, statement["mutable"], origins=value.origins)
            elif kind == "Assignment":
                self.assignment(statement)
            elif kind == "Destructure":
                terminated = self.destructure(statement).type == NEVER
            elif kind == "Return":
                value = self.infer(statement["value"], self.result, lift=True) if statement["value"] else Value(UNIT, statement)
                value = self.convert(value, self.result, lift=True)
                self.consume(value)
                self.escape(value, function_exit=True)
                self.return_origins.update(value.origins)
                terminated = True
            elif kind in {"Break", "Continue"}:
                if not self.loop_depths:
                    self.fail(statement, f"{kind.lower()} 只能用于循环", "XE-FLOW-0001")
                terminated = True
            else:
                value = self.infer(statement["expression"])
                if value.type == INT_LITERAL:
                    self.convert(value, I32)
                self.consume(value)
                terminated = value.type == NEVER
        if terminated:
            result = Value(NEVER, node)
        elif node["tail"]:
            result = self.infer(node["tail"], expected, lift)
            if expected:
                result = self.convert(result, expected, lift)
            self.consume(result)
            # 值已经转交给块结果，外层不能再次移动原局部名字。
            result = Value(result.type, result.node, origins=result.origins,
                           literal=result.literal, literal_sources=result.literal_sources)
        else:
            result = Value(UNIT, node)
        local_ids = {b.uid for b in self.scopes[-1].values()}
        self.scope_escape(result, node, local_ids)
        for uid in local_ids:
            self.invalid_roots[uid] = "已离开作用域"
        self.scopes.pop()
        return result

    def scope_escape(self, result, node, local_ids):
        self.escape(result, local_ids)
        # 块结果不是唯一的逃逸渠道：外层变量可能被赋为内层地址。
        for scope in self.scopes[:-1]:
            for binding in scope.values():
                if binding.initialized and not binding.moved and binding.last_use >= node["span"]["end"]["offset"]:
                    escaped = Value(binding.type, binding.node, origins=binding.origins)
                    self.escape(escaped, local_ids)
                    if has_unsafe(escaped.type):
                        self.mark_binding_unsafe(binding)

    def place(self, node, read=True):
        if node["kind"] == "Group":
            return self.place(node["expression"], read)
        if node["kind"] == "Name" and len(node["path"]["parts"]) == 1:
            name = node["path"]["parts"][0]
            # 模块只读值有名称，但没有局部变量的可写/可取址存储。
            # 先尊重局部遮蔽，再用普通值诊断非法写入，而非谎报“未定义”。
            if name in self.constants and not any(name in scope for scope in self.scopes):
                return self.infer(node)
            binding = self.lookup(name, node, read, partial=True)
            return Value(binding.type, node, (binding.uid, ()), binding.origins,
                         borrowed=binding.capture_borrowed, access_uid=binding.uid,
                         writable=binding.capture_writable if binding.capture_borrowed else None)
        if not read and node["kind"] == "FieldAccess":
            # 重新初始化已移动的字段不读取旧值。父对象及更早的路径仍
            # 需要有效：只放过当前精确叶子，不允许越过已移动的父字段。
            previous = getattr(self, "_writing_target", None)
            self._writing_target = node
            try:
                return self.infer(node)
            finally:
                self._writing_target = previous
        return self.infer(node)

    def mutable_place(self, value):
        if value.borrowed:
            return value.writable if value.writable is not None else any(mutable for _, mutable in value.origins)
        if value.place:
            binding = self.by_uid(value.place[0])
            return bool(binding and binding.mutable)
        return False

    def note_closure_access(self, value, mode):
        """只记录捕获环境的移动/写入；通过捕获指针写外部对象不是写环境。"""
        if value.place and not value.borrowed and value.place[0] in self._closure_bindings:
            if mode == "once" or self._closure_mode != "once":
                self._closure_mode = mode

    def assignment(self, node):
        operator = node["operator"]
        target_node = node["right"] if operator == ">>" else node["left"]
        source_node = node["left"] if operator == ">>" else node["right"]
        target = self.place(target_node, read=False)
        binding = self.by_uid(target.place[0]) if target.place else None
        initializing = bool(binding and not binding.initialized and target.place and not target.place[1])
        if not initializing and not self.mutable_place(target):
            self.fail(target_node, "不能修改不可变绑定或只读指针", "XE-MUT-0001")
        self.note_closure_access(target, "mut")
        value = self.convert(self.infer(source_node, target.type), target.type)
        # The target was checked before evaluating the RHS, but a nested place
        # still needs its owner afterwards.  Reinitializing an entire variable
        # (or an exactly moved field) is legal; indexing into a moved owner is
        # not.  This is an ownership check for a directly named aggregate, not
        # an exclusive-borrow rule for ordinary pointers or captured aliases.
        leaf = target_node
        while leaf["kind"] == "Group":
            leaf = leaf["expression"]
        root = leaf
        while root["kind"] in {"Group", "FieldAccess", "BracketApply"}:
            root = root["expression"] if root["kind"] == "Group" else root["object"]
        if (value.type != NEVER and leaf["kind"] != "Name" and target.place and binding
                and root["kind"] == "Name" and len(root["path"]["parts"]) == 1
                and self.lookup(root["path"]["parts"][0], root, read=False).uid == binding.uid):
            if not binding.initialized:
                self.fail(target_node, f"{binding.name} 可能尚未初始化", "XE-INIT-0001")
            if binding.moved:
                self.fail(target_node, f"{binding.name} 已移动或在某条分支中移动", "XE-MOVE-0001")
            path = target.place[1]
            if any(path[:len(moved)] == moved and
                   (moved != path or leaf["kind"] == "BracketApply")
                   for moved in binding.moved_fields):
                self.fail(target_node, "目标的父资源已经移动", "XE-MOVE-0001")
        if binding and binding.uid in self.static_roots and any(
                uid not in self.static_roots for uid, _ in value.origins):
            # 即便写在自己的函数体里，这些地址会在函数返回后留在全局对象中。
            # 普通指针风险依旧只 warning；不会阻止编译，也不延长局部对象生命。
            self.warn_pointer(value, "写入全局变量的地址或视图可能比所指对象活得更久", "XE-PTR-0001")
        # 写入发生在右侧求值完成之后。仍活跃的共享借用不能被写操作绕过，
        # 但仅在右侧最后一次使用的借用可以在真正写入前结束。
        write_origins = (target.origins if target.borrowed else
                         ((target.place[0], True),) if target.place else ())
        write_origins = tuple((uid, True) for uid, _ in write_origins)
        saved_loans = list(self.temporary_loans)
        if not self.copyable(target.type):
            # 替换资源会回收旧值；新资源不能携带指向旧资源的视图。
            self.temporary_loans.extend(value.origins)
        try:
            self.loan(write_origins, node, exclude=target.access_uid,
                      position=node["span"]["end"]["offset"])
        finally:
            self.temporary_loans = saved_loans
        if target.borrowed and value.origins:
            target_roots = {uid for uid, _ in target.origins}
            foreign = [uid for uid, _ in value.origins if uid not in target_roots]
            if binding is None and foreign:
                if any(uid not in self.parameter_roots for uid in foreign):
                    self.warn_pointer(value, "写入外部对象的地址或视图可能在局部存储结束后失效", "XE-PTR-0001")
        if operator == "=" and not self.copyable(value.type):
            self.fail(source_node, "资源不能用 = 复制，使用 <<", "XE-OWN-0001")
        if operator == "<<" and value.type != NEVER and self.copyable(value.type):
            self.fail(source_node, "Copy 值使用 =；<< 只用于转移所有权", "XE-OWN-0001")
        if value.place == target.place and value.place and operator != "=" and not self.copyable(value.type):
            self.fail(node, "不能把资源传递给自身", "XE-MOVE-0001")
        if operator != "=":
            self.consume(value)
        if target.borrowed and not initializing and not self.copyable(target.type):
            # 写穿捕获别名或普通指针会替换外部资源，而非闭包环境字段。
            for uid in {uid for uid, _ in target.origins}:
                self.invalidate_storage(uid, node, "已被替换，旧资源可能已析构")
                self.invalid_roots.pop(uid, None)
        if target.place and not target.borrowed:
            self.loan(((target.place[0], True),), node, exclude=target.access_uid)
        if binding and not target.borrowed:
            assert target.place is not None  # binding 由这个具名位置查询而得。
            if not initializing and not self.copyable(target.type):
                self.invalidate_storage(binding.uid, node, "已被替换，旧资源可能已析构")
            binding.initialized, binding.moved = True, False
            binding.origins = value.origins
            binding.type = merge_unsafe(binding.type, value.type)
            self.record_type(binding.node, binding.type, binding.name)
            if target.place[1]:
                binding.moved_fields.discard(target.place[1])
            else:
                binding.moved_fields.clear()
            # 新值已建立。旧别名已附上 unsafe；新取址不应继承旧资源风险。
            self.invalid_roots.pop(binding.uid, None)
        elif binding and target.borrowed and value.origins:
            # 经指针写入本地视图也会影响所有者；让块退出检查发现外层变量逃逸。
            binding.origins = tuple(set(binding.origins + value.origins))

    def destructure(self, node):
        targets = node["targets"]
        names = [target["name"] for target in targets if target["name"] != "_"]
        if len(set(names)) != len(names):
            self.fail(node, "元组解包目标名称重复（_ 可以重复）", "XE-NAME-0002")
        annotations, destinations = [], []
        for target in targets:
            annotation = self.type_of(target["type"]) if target.get("type") else None
            binding = None
            if not node["declare"] and target["name"] != "_":
                binding = self.lookup(target["name"], target, read=False)
                if binding.initialized and not binding.mutable:
                    self.fail(target, "不能修改不可变绑定", "XE-MUT-0001")
                if annotation is not None and annotation != binding.type:
                    self.fail(target, f"解包目标类型注解与变量类型 {binding.type} 不符", "XE-TYPE-0001")
                annotation = binding.type
            annotations.append(annotation)
            destinations.append((binding, bool(binding and binding.initialized)))
        expected = Type("tuple", tuple(annotation or UNKNOWN for annotation in annotations))
        value = self.infer(node["value"], expected)
        if value.type == NEVER:
            return value
        if value.type.name != "tuple":
            self.fail(node["value"], f"元组解包需要 tuple 值，实际为 {value.type}", "XE-TYPE-0001")
        if len(value.type.args) != len(targets):
            self.fail(node, f"元组解包需要 {len(targets)} 个元素，实际为 {len(value.type.args)} 个", "XE-TYPE-0001")
        # 解包建立独立成员值，因此每个指针可分别降为只读；整个 tuple
        # 容器仍不协变，原始源类型保持不变并且只消费一次。
        element_types = [self.convert(Value(element, node["value"], origins=value.origins),
            annotation or self.default(element)).type
            for annotation, element in zip(annotations, value.type.args)]
        if any(self.unresolved(element) for element in element_types):
            self.fail(node, "不能确定元组元素类型，请添加成员类型注解", "XE-TYPE-0001")
        if node["operator"] == "=" and not self.copyable(value.type):
            self.fail(node["value"], f"{value.type} 不能复制，必须使用 <<", "XE-OWN-0001")
        if node["operator"] == "<<" and self.copyable(value.type):
            self.fail(node["value"], f"{value.type} 是 Copy 值，使用 =；<< 只用于转移所有权", "XE-OWN-0001")
        # 先完成整个 RHS 并转交所有权，然后建立或替换目标；交换也据此工作。
        self.consume(value)
        for target, element_type, destination in zip(targets, element_types, destinations):
            if has_unsafe(value.type) and self.carries_borrow(element_type):
                element_type = mark_unsafe(element_type)
            self.destructure_types[id(target)] = element_type
            self.record_type(target, element_type, target["name"])
            origins = value.origins if self.carries_borrow(element_type) else ()
            if target["name"] == "_":
                continue
            if node["declare"]:
                self.declare(target["name"], element_type, target, node["mutable"], origins=origins)
            else:
                binding, initialized = destination
                if initialized and not self.copyable(binding.type):
                    self.invalidate_storage(binding.uid, node, "已被替换，旧资源可能已析构")
                binding.initialized, binding.moved = True, False
                binding.moved_fields.clear()
                binding.origins = origins
                binding.type = merge_unsafe(binding.type, element_type)
                self.record_type(binding.node, binding.type, binding.name)
                self.invalid_roots.pop(binding.uid, None)
        return value

    def infer(self, node, expected=None, lift=False) -> Value:
        value = self._infer(node, expected, lift)
        invalid = [self.invalid_roots[uid] for uid, _ in value.origins if uid in self.invalid_roots]
        if invalid and self.carries_borrow(value.type):
            self.warn_pointer(value, "所指存储" + invalid[0] + "，地址或视图可能失效", "XE-PTR-0002")
        self.record_type(node, value.type)
        return value

    def _infer(self, node, expected=None, lift=False) -> Value:
        kind = node["kind"]
        self.position = self.offset(node)
        value_expected = expected.args[0] if expected and expected.name == "maybe" and lift else expected
        if kind == "Literal":
            category = node["literal_kind"]
            type_ = {"INTEGER": INT_LITERAL, "FLOAT": Type("f64"), "STRING": STR,
                     "CHAR": Type("char"), "BYTE": Type("u8"), "true": BOOL,
                     "false": BOOL, "unit": UNIT}[category]
            value = Value(type_, node, literal=node["value"],
                          literal_sources=((node, node["value"]),) if type_ == INT_LITERAL else ())
            numeric_expected = value_expected
            if type_ == INT_LITERAL and numeric_expected and numeric_expected.name in NUMERIC:
                return self.convert(value, numeric_expected)
            if category == "FLOAT" and numeric_expected and numeric_expected.name in {"f32", "f64"}:
                if numeric_expected.name == "f32" and abs(node["value"]) > 3.4028234663852886e38:
                    self.fail(node, "浮点字面量超出 f32 有限范围", "XE-TYPE-0004")
                return Value(numeric_expected, node, literal=node["value"])
            return value
        if kind == "NoneValue":
            if expected is not None and expected.name == "Step":
                self.fail(node, "Step[T] 的结束标记是 Step::Stop，不能使用 None", "XE-ITER-0001",
                          "用 Step::Item[value] 产生元素，用 Step::Stop 结束迭代")
            if expected is None or expected.name != "maybe":
                self.fail(node, "None 只能用于可选结果的返回位置；不是普通变量值",
                          "XE-RESULT-0001")
            return Value(NONE, node)
        if kind == "Name":
            name = "::".join(node["path"]["parts"])
            if len(node["path"]["parts"]) == 1 and (name in self.globals or any(name in s for s in self.scopes)):
                binding = self.lookup(name, node)
                if binding.type.name != "ptr":
                    self.loan(((binding.uid, False),), node, exclude=binding.uid)
                return Value(binding.type, node, (binding.uid, ()), binding.origins,
                             borrowed=binding.capture_borrowed, access_uid=binding.uid,
                             writable=binding.capture_writable if binding.capture_borrowed else None)
            if name in self.functions:
                signature = self.functions[self.call_targets.get(id(node), name)]
                self.member_access(signature.node, node)
                if signature.generics:
                    self.fail(node, "泛型函数值需要具体类型附件；直接调用或管道目标才可从实参推导",
                              "XE-GENERIC-0001", f"例如 {name}[i32]")
                result_type = self.call_result_type(signature, [])
                external_address = signature.node["body"] is None and self.carries_borrow(result_type)
                if external_address:
                    result_type = mark_unsafe(result_type)
                value = Value(callable_type(signature.parameters, result_type), node)
                if external_address:
                    self.warn_pointer(value, "外部函数值返回的地址或视图有效性无法由本编译器确认", "XE-PTR-0003")
                return value
            if name in self.constants:
                return self.constants[name]
            standard_signature = io_function(name) or env_function(name)
            if standard_signature is not None:
                if standard_signature.formatted:
                    self.fail(node, "格式化输出目前需要直接调用；异构格式化函数值尚未支持",
                              "XE-SEM-0001", '可包装成固定签名：fn(text: str) { println("{}", text); }')
                return Value(callable_type(standard_signature.parameters, standard_signature.result), node)
            if "::" in name:
                resolved = self.signature_target(node)
                if resolved:
                    signature, substitutions = resolved
                    self.member_access(signature.node, node)
                    concrete = self.instantiate(signature, substitutions, node)
                    if concrete.node["name"] in self.functions:
                        self.call_targets[id(node)] = concrete.node["name"]
                    parameters = [substitute(t, substitutions) for t in signature.parameters]
                    result = self.call_result_type(concrete, [], substitute(signature.result, substitutions))
                    return Value(callable_type(parameters, result), node)
            variant = self.variant(name, expected, node, optional=True)
            if variant is not None:
                owner, payload = variant
                if payload:
                    self.fail(node, "有负载的枚举变体必须用 [] 构造", "XE-TYPE-0005")
                return Value(owner, node)
            self.fail(node, f"未定义名称 {name}", "XE-NAME-0001")
        if kind == "Group":
            return self.infer(node["expression"], expected, lift)
        if kind == "Cast":
            value = self.infer(node["operand"])
            source = self.default(value.type)
            self.convert(value, source)
            target = self.type_of(node["type"])
            mods = [m["name"] for m in node["modifiers"]]
            if mods:
                # AST 可以由工具直接传入，因此除了解析器也须验证旧节点。
                self.fail(node, "as 不接受转换策略附件；使用 T::try_from(value)")
            if source.name not in NUMERIC or target.name not in NUMERIC:
                self.fail(node, "as 只用于数值转换；指针使用 @、# 或地址 API")
            integers = NUMERIC - {"f32", "f64"}
            def bits(t):
                import struct
                return struct.calcsize("P") * 8 if t.name in {"isize", "usize"} else int(t.name[1:])
            lossless = source == target or source.name == "f32" and target.name == "f64"
            if source.name in integers and target.name in integers:
                lossless = (source.name[0] == target.name[0] and bits(target) >= bits(source)
                            or source.name[0] == "u" and target.name[0] == "i" and bits(target) > bits(source))
            elif source.name in integers and target.name in {"f32", "f64"}:
                lossless = bits(source) - (source.name[0] == "i") <= (24 if target.name == "f32" else 53)
            if not lossless:
                self.fail(node, "该转换可能丢失信息；使用 T::try_from(value) 并处理转换错误")
            return Value(target, node)
        if kind == "Block":
            return self.block(node, expected, lift)
        if kind == "Borrow":
            value = self.place(node["operand"])
            if value.place is None:
                self.fail(node, "只能对有存储位置的变量、字段或解引用取地址", "XE-BORROW-0001")
            mods = [m["name"] for m in node["modifiers"]]
            if any(m not in {"mut", "unsafe"} and m not in self.generic_names for m in mods) or len(mods) != len(set(mods)):
                self.fail(node, "未知或重复的借用修饰")
            mutable = "mut" in mods
            if mutable and not self.mutable_place(value):
                self.fail(node, "可写借用需要 let[mut] 存储或已有可写指针", "XE-MUT-0001")
            if mutable:
                self.note_closure_access(value, "mut")
            origins = value.origins if value.borrowed else ((value.place[0], mutable),)
            if not origins:
                # 借用元素访问也必须有拥有者，不能因纯值没有自带视图来源
                # 而漏掉数组存储的生命周期。
                origins = ((value.place[0], mutable),)
            origins = tuple((uid, mutable) for uid, _ in origins)
            self.loan(origins, node, exclude=value.access_uid if value.borrowed else None)
            return Value(ptr(value.type, mutable, "unsafe" in mods), node, origins=origins)
        if kind == "Dereference":
            pointer = self.infer(node["operand"])
            if pointer.type.name != "ptr":
                self.fail(node, "# 只能解引用指针")
            if has_unsafe(pointer.type):
                self.warn_pointer(pointer, "正在解引用带 unsafe 风险注记的指针", "XE-PTR-0003")
            self.loan(pointer.origins, node, exclude=pointer.access_uid)
            root = pointer.origins[0][0] if pointer.origins else None
            target_type = pointer.type.args[0]
            if has_unsafe(pointer.type) and self.carries_borrow(target_type):
                target_type = mark_unsafe(target_type)
            return Value(target_type, node,
                         (root, ()) if root is not None else None, pointer.origins,
                         True, pointer.access_uid, writable=pointer.type.mutable)
        if kind == "FieldAccess":
            value = self.place(node["object"])
            base = value.type.args[0] if value.type.name == "ptr" else value.type
            borrowed = value.borrowed or value.type.name == "ptr"
            if base.name == "tuple" and node["field"].isdigit():
                index = int(node["field"])
                if index >= len(base.args):
                    self.fail(node, "元组字段越界")
                field_type = base.args[index]
            else:
                definition = self.types.get(base.name)
                fields = {f["name"]: f for f in definition.get("fields", [])} if definition else {}
                if node["field"] not in fields:
                    self.fail(node, f"{base} 没有字段 {node['field']}", "XE-NAME-0001")
                assert definition is not None
                self.member_access(fields[node["field"]], node)
                field_type = self.type_of(fields[node["field"]]["type"], self.generic_set(definition))
                field_type = substitute(field_type, {"$"+p["name"]: t for p, t in
                                        zip(definition.get("generics", []), base.args)})
            root_place = value.place
            if value.type.name == "ptr":
                root_place = (value.origins[0][0], ()) if value.origins else None
            place = (root_place[0], root_place[1] + (node["field"],)) if root_place else None
            if place:
                self.loan(value.origins if borrowed else ((place[0], False),), node, exclude=value.access_uid)
                binding = self.by_uid(place[0])
                if binding and any(place[1][:len(m)] == m and not (
                        getattr(self, "_writing_target", None) is node and m == place[1])
                        for m in binding.moved_fields):
                    self.fail(node, "该字段已经移动", "XE-MOVE-0001")
            origins = value.origins if borrowed or self.carries_borrow(field_type) else ()
            if has_unsafe(value.type) and self.carries_borrow(field_type):
                field_type = mark_unsafe(field_type)
            return Value(field_type, node, place, origins, borrowed, value.access_uid,
                         writable=value.type.mutable if value.type.name == "ptr" else value.writable)
        if kind == "BracketApply":
            return self.bracket(node, expected)
        if kind == "AssociatedAccess":
            variant = self.variant_expression(node, expected, optional=True)
            if variant:
                owner, payload = variant
                if payload:
                    self.fail(node, "有负载的枚举变体必须用 [] 构造", "XE-TYPE-0005")
                return Value(owner, node)
            resolved = self.signature_target(node)
            if resolved:
                signature, substitutions = resolved
                concrete = self.instantiate(signature, substitutions, node)
                if concrete.node["name"] in self.functions:
                    self.call_targets[id(node)] = concrete.node["name"]
                parameters = [substitute(t, substitutions) for t in signature.parameters]
                result = self.call_result_type(concrete, [], substitute(signature.result, substitutions))
                return Value(callable_type(parameters, result), node)
            self.fail(node, "未知关联名称", "XE-NAME-0001")
        if kind == "StructLiteral":
            name_node = node["constructor"]
            named = "::".join(name_node["path"]["parts"]) if name_node["kind"] == "Name" else None
            if named in self.types and self.types[named].get("generics"):
                if value_expected is None or value_expected.name != named:
                    self.fail(name_node, f"泛型结构体 {named} 需要具体类型附件或上下文类型",
                              "XE-GENERIC-0001", f"例如 {named}[i32] {{ ... }}")
                constructed = value_expected
            else:
                constructed = self.expression_type(name_node)
            name = constructed.name
            definition = self.types.get(name)
            if not definition or definition["kind"] != "Struct":
                self.fail(node, f"{name} 不是已声明结构体", "XE-NAME-0001")
            fields = {f["name"]: f for f in definition["fields"]}
            names = [f["name"] for f in node["fields"]]
            if len(set(names)) != len(names) or set(names) != set(fields):
                self.fail(node, "结构体必须恰好初始化所有字段", "XE-INIT-0002")
            origins, unsafe_fields = [], False
            substitutions = {"$" + p["name"]: t for p, t in zip(definition.get("generics", []), constructed.args)}
            for field in node["fields"]:
                self.member_access(fields[field["name"]], field)
                type_ = substitute(self.type_of(fields[field["name"]]["type"], self.generic_set(definition)), substitutions)
                value = self.convert(self.infer(field["value"], type_), type_)
                if field["operator"] == "=" and not self.copyable(value.type):
                    self.fail(field, "资源字段必须使用 <<", "XE-OWN-0001")
                if field["operator"] == "<<" and value.type != NEVER and self.copyable(value.type):
                    self.fail(field, "Copy 字段使用 =；<< 只用于转移所有权", "XE-OWN-0001")
                self.consume(value)
                origins.extend(value.origins)
                unsafe_fields = unsafe_fields or has_unsafe(value.type)
            result_type = mark_unsafe(constructed) if unsafe_fields else constructed
            return Value(result_type, node, origins=tuple(origins))
        if kind in {"Array", "Tuple"}:
            elem_expected = value_expected.args[0] if value_expected and value_expected.name == "Array" else None
            if kind == "Tuple" and value_expected and value_expected.name == "tuple":
                if len(node["elements"]) != len(value_expected.args):
                    self.fail(node, "元组元素数量与类型注解不符")
                values = []
                for element, element_type in zip(node["elements"], value_expected.args):
                    value = self.infer(element, None if element_type == UNKNOWN else element_type)
                    if value.type == NEVER:
                        return Value(NEVER, node)
                    value = self.convert(value, element_type) if element_type != UNKNOWN else value
                    self.consume(value)
                    values.append(value)
            else:
                values = []
                for element in node["elements"]:
                    value = self.infer(element, elem_expected)
                    if value.type == NEVER:
                        return Value(NEVER, node)
                    self.consume(value)
                    values.append(value)
            if kind == "Tuple":
                for value in values:
                    if value.type == INT_LITERAL:
                        self.convert(value, I32)
                type_ = Type("tuple", tuple(self.default(v.type) for v in values))
            else:
                if not values and (value_expected is None or value_expected.name != "Array"):
                    self.fail(node, "空数组需要显式 Array[T, 0] 类型注解")
                if values:
                    common = self.common(values, node)
                else:
                    assert value_expected is not None  # 上方已拒绝无类型注解的空数组。
                    common = Value(value_expected.args[0], node)
                if common.type == INT_LITERAL:
                    self.convert(common, I32)
                type_ = Type("Array", (self.default(common.type), Type(str(len(values)))))
            return Value(type_, node, origins=tuple(set().union(*(set(v.origins) for v in values))))
        if kind == "TupleBinding":
            self.fail(node, "_ 和成员类型注解只用于元组解包目标，不是元组值", "XE-SEM-0001")
        if kind == "Call":
            return self.call(node, expected)
        if kind == "Unary":
            if node["operator"] == "bitnot":
                target = value_expected if value_expected and value_expected.name in NUMERIC else None
                value = self.infer(node["operand"], target)
                integer_type = self.default(value.type)
                if integer_type.name not in NUMERIC - {"f32", "f64"}:
                    self.fail(node, "bitnot 需要整数；逻辑取反使用 not", "XE-TYPE-0004")
                self.convert(value, integer_type)
                return Value(integer_type, node)
            operand_expected = BOOL if node["operator"] == "not" else None
            if value_expected and value_expected.name in {"f32", "f64"}:
                operand_expected = value_expected
            value = self.infer(node["operand"], operand_expected)
            if node["operator"] == "not":
                self.convert(value, BOOL)
                return Value(BOOL, node)
            if value.type.name not in NUMERIC | {"$integer"}:
                self.fail(node, "一元正负号需要数值")
            literal = (-value.literal if node["operator"] == "-" and
                       value.literal is not None else value.literal)
            sources = (((node, literal),) if literal is not None else value.literal_sources)
            result = Value(value.type, node, literal=literal,
                           literal_sources=sources if value.type == INT_LITERAL else ())
            target = value_expected
            return self.convert(result, target) if target and target.name in NUMERIC else result
        if kind == "ComparisonChain":
            values = [self.infer(operand) for operand in node["operands"]]
            common = self.common(values, node)
            if common.type == INT_LITERAL:
                self.convert(common, I32)
            if common.type.name not in NUMERIC | {"$integer", "bool", "char", "str"}:
                self.fail(node, "该类型的比较 Trait 检查尚未实现", "XE-SEM-0001")
            return Value(BOOL, node)
        if kind == "Binary":
            left = self.infer(node["left"], value_expected)
            if node["operator"] in {"bitshl", "bitshr"}:
                type_ = self.default(left.type)
                if type_.name not in NUMERIC - {"f32", "f64"}:
                    self.fail(node, "移位左侧必须是整数", "XE-TYPE-0004")
                self.convert(left, type_)
                # 次数是独立的整数，不必与被移位值同宽；没有数值变量的隐式转换。
                right = self.infer(node["right"])
                count_type = self.default(right.type)
                if count_type.name not in NUMERIC - {"f32", "f64"}:
                    self.fail(node, "移位次数必须是整数", "XE-TYPE-0004")
                self.convert(right, count_type)
                constants = {name: value for name, value in self.constants.items()
                             if not any(name in scope for scope in self.scopes)}
                try:
                    count = scalar_static_value(node["right"], constants, count_type)
                    limits = integer_limits(type_)
                    assert limits is not None
                    if count is not None and not 0 <= count < limits[0]:
                        self.fail(node["right"], f"移位次数必须在 0..{limits[0]} 内", "XE-BIT-0001")
                    value = scalar_static_value(node["left"], constants, type_)
                    if node["operator"] == "bitshl" and value is not None and count is not None:
                        if not limits[1] <= value << count <= limits[2]:
                            self.fail(node, f"左移结果超出 {type_} 范围", "XE-BIT-0002")
                except StaticValueError as error:
                    self.fail(node, str(error), "XE-BIT-0001")
                return Value(type_, node)
            if node["operator"] in {"and", "or"}:
                self.convert(left, BOOL)
                before = deepcopy(self.scopes)
                right = self.infer(node["right"], BOOL)
                right_states = deepcopy(self.bindings())
                self.scopes = before
                self.merge({}, [self.bindings(), right_states])
                self.convert(right, BOOL)
                return Value(BOOL, node)
            right = self.infer(node["right"], left.type if left.type != INT_LITERAL else value_expected)
            common = self.common([left, right], node)
            if node["operator"] in {"bitand", "bitor", "bitxor"}:
                integer_type = self.default(common.type)
                if integer_type.name not in NUMERIC - {"f32", "f64"}:
                    self.fail(node, "bitand/bitor/bitxor 两侧必须是同类型整数；逻辑运算使用 and/or", "XE-TYPE-0004")
                self.convert(common, integer_type)
                return Value(integer_type, node)
            if common.type.name not in NUMERIC | {"$integer"}:
                self.fail(node, "算术运算需要数值")
            if node["operator"] == "%" and common.type.name in {"f32", "f64"}:
                self.fail(node, "% 只用于整数余数；浮点余数接口尚未设计", "XE-TYPE-0004")
            return Value(common.type, node, literal_sources=common.literal_sources)
        if kind == "Range":
            endpoint = expected.args[0] if expected and expected.name == "Range" else None
            values = [self.infer(node[key], endpoint) for key in ("lower", "upper") if node[key]]
            common = self.common(values, node) if values else Value(USIZE, node)
            if common.type == INT_LITERAL:
                self.convert(common, I32)
            type_ = self.default(common.type)
            if type_.name not in NUMERIC - {"f32", "f64"}:
                self.fail(node, "范围端点必须是整数")
            return Value(Type("Range", (type_,)), node)
        if kind == "If":
            self.convert(self.infer(node["condition"], BOOL), BOOL)
            before = deepcopy(self.scopes)
            left = self.infer(node["then"], expected, lift)
            alternatives = [deepcopy(self.bindings())] if left.type != NEVER else []
            self.scopes = deepcopy(before)
            right = self.infer(node["otherwise"], expected, lift) if node["otherwise"] else Value(UNIT, node)
            if right.type != NEVER:
                alternatives.append(deepcopy(self.bindings()))
            self.scopes = before
            self.merge({}, alternatives)
            return self.common([left, right], node)
        if kind in {"While", "For"}:
            return self.loop(node)
        if kind == "Pipeline":
            value = self.infer(node["input"])
            self.consume(value)
            return self.handle(node["handler"], [value], expected, lift)
        if kind == "Branch":
            return self.branch(node, expected, lift)
        if kind == "Propagate":
            value = self.infer(node["operand"])
            if value.type.name != "maybe" or self.result.name != "maybe":
                self.fail(node, "?[return] 需要结果值及返回结果的外层函数", "XE-RESULT-0002")
            if value.type.args[1] != self.result.args[1]:
                self.fail(node, "传播错误类型必须与外层函数相同", "XE-RESULT-0002")
            if self.carries_borrow(value.type.args[1]):
                error = Value(value.type.args[1], node, origins=value.origins)
                self.escape(error, function_exit=True)
                self.return_origins.update(error.origins)
            self.consume(value)
            return Value(value.type.args[0], node,
                         origins=value.origins if self.carries_borrow(value.type.args[0]) else ())
        if kind == "Unwrap":
            value = self.infer(node["operand"])
            if value.type.name != "maybe":
                self.fail(node, "?[panic] 需要 T? 或 T?[E] 结果值", "XE-RESULT-0002")
            # panic 无继续执行路径，不参与正常分支的状态合并。
            # 成功时传递负载所有权，与 ?[return] 相同，不能从指针移出资源。
            self.consume(value)
            return Value(value.type.args[0], node,
                         origins=value.origins if self.carries_borrow(value.type.args[0]) else ())
        if kind == "AnonymousFunction":
            return self.anonymous(node)
        if kind == "Unsafe":
            # 历史解析器保留这个节点供 AST/迁移工具识别，但它从未成为
            # 已确认的可执行语法。必须在 check 阶段拒绝，不能假装普通块
            # 检查成功后才让 C 后端报能力错误。风险附件属于指针的类型。
            self.fail(node, "unsafe 代码块不属于 Xe 当前支持范围；unsafe 是指针风险附件，不是代码块权限",
                      "XE-SEM-0001", "使用 T@[unsafe] 或 T@[mut, unsafe] 标注指针风险；普通代码块仍写 { ... }")
        self.fail(node, f"尚未支持的语义结构 {kind}", "XE-SEM-0001")

    def variant(self, name, expected, node, optional=False):
        parts = name.split("::")
        alias_owner = self.resolve_alias(parts[0]) if len(parts) == 2 and parts[0] in self.aliases else None
        if alias_owner:
            parts[0] = alias_owner.name
            expected = alias_owner
        if len(parts) == 2 and parts[0] == "Maybe":
            result = expected if expected and expected.name == "maybe" else maybe(UNKNOWN, UNKNOWN)
            if parts[1] == "Yes":
                return result, [result.args[0]]
            if parts[1] == "No":
                return result, [result.args[1]]
            if parts[1] == "None":
                return maybe(result.args[0]), []
        if len(parts) == 2 and parts[0] in self.types:
            declaration = self.types[parts[0]]
            for variant in declaration.get("variants", []):
                if variant["name"] == parts[1]:
                    if declaration.get("generics") and not (expected and expected.name == parts[0]):
                        self.fail(node, f"泛型枚举 {parts[0]} 需要具体类型附件或上下文类型",
                                  "XE-GENERIC-0001", f"例如 {parts[0]}[i32]::{parts[1]}，或给接收变量标注完整类型")
                    result = expected if expected and expected.name == parts[0] else Type(parts[0])
                    bindings = {"$"+p["name"]: t for p, t in zip(declaration.get("generics", []), result.args)}
                    return result, [substitute(self.type_of(t, self.generic_set(declaration)), bindings)
                                    for t in variant["payload"]]
        if not optional:
            self.fail(node, f"未知枚举变体 {name}", "XE-NAME-0001")
        return None

    def variant_expression(self, node, expected=None, optional=False):
        if node["kind"] == "Name":
            return self.variant("::".join(node["path"]["parts"]), expected, node, optional)
        if node["kind"] == "AssociatedAccess":
            owner = self.expression_type(node["object"])
            return self.variant(owner.name + "::" + node["member"], owner, node, optional)
        if not optional:
            self.fail(node, "此处需要枚举变体名称", "XE-TYPE-0005")
        return None

    def signature_target(self, node):
        """解析函数名或类型关联函数，返回模板及由类型限定名确定的代入。"""
        if node["kind"] == "Name":
            name = "::".join(node["path"]["parts"])
            if any(name in scope for scope in self.scopes):
                return None
            if name in self.functions:
                self.member_access(self.functions[name].node, node)
                return self.functions[name], {}
            if "::" not in name:
                return None
            owner_name, member = name.rsplit("::", 1)
            owner = self.resolve_alias(owner_name) if owner_name in self.aliases else None
            if owner:
                owner_name = owner.name
            signature = self.methods.get((owner_name, member))
            if signature:
                self.member_access(signature.node, node)
            if signature and owner:
                assert signature.self_type is not None
                substitutions = {}
                self.unify_generic(signature.self_type, owner, substitutions, node)
                if substitute(signature.self_type, substitutions) != owner:
                    self.fail(node, f"此关联函数属于 {signature.self_type}，不是 {owner}", "XE-TYPE-0001")
                return signature, substitutions
            return (signature, {}) if signature else None
        if node["kind"] == "AssociatedAccess":
            owner = self.expression_type(node["object"])
            signature = self.methods.get((owner.name, node["member"]))
            if not signature:
                return None
            assert signature.self_type is not None
            self.member_access(signature.node, node)
            substitutions = {}
            self.unify_generic(signature.self_type, owner, substitutions, node)
            if substitute(signature.self_type, substitutions) != owner:
                self.fail(node, f"此关联函数属于 {signature.self_type}，不是 {owner}", "XE-TYPE-0001")
            return signature, substitutions
        return None

    def bracket(self, node, expected):
        obj = node["object"]
        resolved = self.signature_target(obj)
        if resolved:
            signature, substitutions = resolved
            substitutions.update(self.explicit_substitutions(signature, node["arguments"], node))
            concrete = self.instantiate(signature, substitutions, node)
            self.call_targets[id(node)] = concrete.node["name"]
            # 机器代码缓存去除调用处风险，但源码显式 @[unsafe] 的函数值
            # 类型必须保留这些信息，不能借实例缓存洗掉地址风险。
            parameters = [substitute(t, substitutions) for t in signature.parameters]
            result = self.call_result_type(concrete, [], substitute(signature.result, substitutions))
            return Value(callable_type(parameters, result), node)
        variant = self.variant_expression(obj, expected, optional=True)
        if variant:
            result, parameters = variant
            values = self.arguments(node["arguments"], parameters, node)
            if result.name == "maybe":
                args = list(result.args)
                member = obj["path"]["parts"][-1] if obj["kind"] == "Name" else obj["member"]
                index = 0 if member == "Yes" else 1
                if values:
                    args[index] = self.default(values[0].type)
                result = Type("maybe", tuple(args))
            if any(has_unsafe(v.type) for v in values):
                result = mark_unsafe(result)
            return Value(result, node, origins=tuple(o for v in values for o in v.origins))
        value = self.infer(obj)
        base = value.type.args[0] if value.type.name == "ptr" else value.type
        if base.name not in {"Array", "Slice", "SliceMut", "Vec"} or len(node["arguments"]) != 1:
            self.fail(node, "此处 [] 只能构造枚举载荷或索引数组/切片", "XE-SEM-0001")
        assert base.args  # 这些容器类型均已解析出元素类型。
        element_type = base.args[0]
        index = self.infer(node["arguments"][0], USIZE)
        self.convert(index, USIZE)
        if base.name == "Array" and isinstance(index.literal, int) and len(base.args) > 1:
            if not 0 <= index.literal < int(base.args[1].name):
                self.fail(node, "数组索引越界", "XE-TYPE-0004")
        origins: tuple[tuple[int, bool], ...] = value.origins
        if base.name in {"Array", "Vec"} and value.place and not value.borrowed and value.type.name != "ptr":
            owner = self.by_uid(value.place[0])
            origins = tuple(set(origins + ((value.place[0], bool(owner and owner.mutable)),)))
        elif base.name == "SliceMut":
            # 描述符是否能被重新赋值，不决定其独占数据是否可写；
            # 但经只读指针访问描述符时，不可取得该独占写入能力。
            writable = value.type.name != "ptr" or value.type.mutable
            origins = tuple((uid, writable) for uid, _ in origins)
        # 可写的是描述符绑定，并不代表其指向的数据可写。Slice 始终只读；
        # SliceMut 才携带元素写权限，经只读描述符指针访问时仍须降为只读。
        writable = (False if base.name == "Slice" else
                    value.type.mutable if value.type.name == "ptr" else
                    base.name == "SliceMut" or self.mutable_place(value))
        if has_unsafe(value.type) and self.carries_borrow(element_type):
            element_type = mark_unsafe(element_type)
        return Value(element_type, node, value.place, origins, borrowed=True,
                     access_uid=value.access_uid, writable=writable)

    def arguments(self, nodes, parameters, node):
        if len(nodes) != len(parameters):
            self.fail(node, f"参数数量不匹配：需要 {len(parameters)} 个，实际 {len(nodes)} 个", "XE-CALL-0001")
        values, substitutions = [], {}
        saved = list(self.temporary_loans)
        try:
            for argument, parameter in zip(nodes, parameters):
                parameter = substitute(parameter, substitutions)
                value = self.infer(argument, None if parameter.name.startswith("$") else parameter)
                if parameter.name.startswith("$") and parameter != UNKNOWN:
                    substitutions[parameter.name] = self.default(value.type)
                    parameter = substitutions[parameter.name]
                value = self.convert(value, parameter) if parameter != UNKNOWN else value
                self.consume(value)
                values.append(value)
                self.temporary_loans.extend(value.origins)
        finally:
            self.temporary_loans = saved
        return values

    def box_constructor(self, owner, node):
        """Box 的普通实参先复制/转交；分配失败同样由被调用操作清理实参。"""
        values = self.arguments(node["arguments"], [owner.args[0]], node)
        if values[0].type == NEVER:
            return Value(NEVER, node)
        return Value(maybe(owner, Type("AllocError")), node, origins=values[0].origins)

    def call(self, node, expected=None):
        callee = node["callee"]
        if callee["kind"] == "FieldAccess":
            return self.method(callee, node["arguments"], node)
        if callee["kind"] == "BracketApply":
            target = callee["object"]
            if target["kind"] == "FieldAccess":
                return self.method(target, node["arguments"], node, callee["arguments"])
            resolved = self.signature_target(target)
            if resolved:
                signature, substitutions = resolved
                substitutions.update(self.explicit_substitutions(signature, callee["arguments"], callee))
                return self.apply_signature(signature, node["arguments"], node, substitutions)
        if callee["kind"] == "AssociatedAccess":
            resolved = self.signature_target(callee)
            if resolved:
                signature, substitutions = resolved
                return self.apply_signature(signature, node["arguments"], node, substitutions)
            owner = self.expression_type(callee["object"])
            sync = self.sync_constructor(owner, callee["member"], node)
            if sync is not None:
                return sync
            if owner.name == "Vec" and callee["member"] in {"new", "with_capacity"}:
                self.arguments(node["arguments"], [] if callee["member"] == "new" else [USIZE], node)
                return Value(owner, node)
            if owner.name == "Box" and callee["member"] == "new":
                return self.box_constructor(owner, node)
            if not owner.args:
                builtin = self.builtin_call(owner.name + "::" + callee["member"], node["arguments"], node)
                if builtin is not None:
                    return builtin
            if self.variant_expression(callee, expected, True):
                self.fail(node, "枚举载荷使用 []，不是函数调用 ()", "XE-TYPE-0005")
        if callee["kind"] == "Name":
            name = "::".join(callee["path"]["parts"])
            if self.variant(name, expected, callee, True):
                self.fail(node, "枚举载荷使用 []，不是函数调用 ()", "XE-TYPE-0005", "例如 Token::Integer[42]")
            local = name in self.globals or any(name in scope for scope in self.scopes)
            if name in self.functions and not local:
                return self.apply_signature(self.functions[name], node["arguments"], node)
            if "::" in name:
                owner_name, member = name.rsplit("::", 1)
                if owner_name in self.aliases:
                    owner = self.resolve_alias(owner_name)
                    name = owner.name + "::" + member
                    resolved = self.signature_target(callee)
                    if resolved:
                        signature, substitutions = resolved
                        return self.apply_signature(signature, node["arguments"], node, substitutions)
                    if owner.name == "Box" and member == "new":
                        return self.box_constructor(owner, node)
                    sync = self.sync_constructor(owner, member, node)
                    if sync is not None:
                        return sync
            builtin = self.builtin_call(name, node["arguments"], node) if not local else None
            if builtin is not None:
                return builtin
            if "::" in name:
                owner, method = name.rsplit("::", 1)
                signature = self.methods.get((owner, method))
                if signature:
                    return self.apply_signature(signature, node["arguments"], node)
        function = self.infer(callee)
        return self.invoke(function, node["arguments"], node)

    def apply_signature(self, signature, nodes, node, substitutions=None, prefix_values=()):
        """先按具体实参推导，再登记实例；具体函数体由检查队列独立复查。"""
        self.member_access(signature.node, node)
        if len(nodes) + len(prefix_values) != len(signature.parameters):
            self.fail(node, "函数参数数量不匹配", "XE-CALL-0001")
        substitutions, values = dict(substitutions or {}), []
        saved = list(self.temporary_loans)
        try:
            arguments = list(prefix_values) + list(nodes)
            for argument, pattern in zip(arguments, signature.parameters):
                parameter = substitute(pattern, substitutions)
                value = (argument if isinstance(argument, Value) else
                         self.infer(argument, None if self.has_variable(parameter) else parameter))
                # 已显式绑定的类型只进行普通转换，允许 mut 指针降为只读；
                # 尚未确定的类型才推导，不能覆盖调用者明确给出的附件。
                if self.has_variable(parameter):
                    self.unify_generic(parameter, value.type, substitutions, value.node)
                value = self.convert(value, substitute(pattern, substitutions))
                self.consume(value)
                self.temporary_loans.extend(value.origins)
                values.append(value)
        finally:
            self.temporary_loans = saved
        concrete = self.instantiate(signature, substitutions, node)
        if concrete.node["name"] in self.functions and self.functions[concrete.node["name"]] is concrete:
            self.call_targets[id(node)] = concrete.node["name"]
        result = self.call_result_type(concrete, values)
        value = Value(result, node, origins=self.call_origins(concrete, values)
                      if self.carries_borrow(result) else ())
        if concrete.node["body"] is None and self.carries_borrow(result):
            self.warn_pointer(value, "外部函数返回的地址或视图有效性无法由本编译器确认", "XE-PTR-0003")
        return value

    def invoke(self, function, nodes, node):
        signature = self.callable_signature(function, node)
        values = self.arguments(nodes, list(signature.args[:-1]), node)
        return self.finish_invoke(function, values, node)

    def callable_signature(self, function, node):
        signature = function.type.args[0] if function.type.name == "ptr" else function.type
        if signature.name not in {"fn", "closure"}:
            self.fail(node, f"{function.type} 不是可调用函数", "XE-CALL-0001")
        if signature.name == "closure":
            self.closure_receiver_mode(function, signature, node)
        return signature

    def closure_receiver_mode(self, function, signature, node):
        """像普通 self 方法一样选接收者，而不是把 f(...) 特判为总是移动。"""
        info = self.closures[signature]
        pointer_call = function.type.name == "ptr" or function.borrowed
        if pointer_call:
            writable = function.type.mutable if function.type.name == "ptr" else function.writable
            if info.mode == "once":
                self.fail(node, "这个闭包会移出捕获资源，必须按值调用；指针没有环境所有权", "XE-MOVE-0002")
            if info.mode == "mut" and not writable:
                self.fail(node, "这个闭包会修改捕获环境，需可写存储或可写指针", "XE-MUT-0001")
            return "mutable" if writable else "readonly"
        if info.mode == "once":
            return "owning"
        if info.mode == "mut":
            if function.place and not self.mutable_place(function):
                self.fail(node, "这个闭包会修改捕获环境，请把闭包定义为 let[mut]", "XE-MUT-0001")
            self.note_closure_access(function, "mut")
            return "mutable"
        return "readonly"

    def finish_invoke(self, function, values, node):
        signature = self.callable_signature(function, node)
        pointer_call = function.type.name == "ptr" or function.borrowed
        info = self.closures.get(signature)
        mode = None
        if info:
            mode = self.closure_receiver_mode(function, signature, node)
            self.closure_call_modes[id(node)] = mode
        if mode == "owning" or info is None and not pointer_call:
            self.consume(function)
        if info:
            self.invalidate_closure_call(function, info, node)
        result = signature.args[-1]
        if self.carries_borrow(result) and any(has_unsafe(v.type) for v in [function, *values]):
            result = mark_unsafe(result)
        if not self.carries_borrow(result):
            return Value(result, node)
        if info is None:
            return Value(result, node, origins=function.origins + tuple(o for v in values for o in v.origins))
        origins = tuple(o for index, value in enumerate(values) if index in info.borrow_parameters for o in value.origins)
        if info.borrow_captures:
            # 环境自身的返回视图依赖 closure 的存储；外部指针捕获不拥有目标。
            owner = self.by_uid(function.origins[0][0]) if pointer_call and function.origins else None
            external = owner.origins if owner and owner.type.name in {"closure", "FromFn"} else function.origins
            if info.borrow_captures - info.storage_captures:
                origins += external
            if info.storage_captures:
                origins += function.origins if function.type.name == "ptr" else (((function.place[0], False),) if function.place else function.origins)
        value = Value(mark_unsafe(result) if info.unsafe_result else result, node, origins=tuple(set(origins)))
        if info.storage_captures and (mode == "owning" or not function.place and not pointer_call):
            self.warn_pointer(value, "闭包拥有调用或临时环境结束后，返回的地址或视图可能失效", "XE-PTR-0003")
        return value

    def invalidate_closure_call(self, function, info, node):
        """旧视图失效提示在返回值建立前执行，新视图来自调用后的存储。"""
        environment = function.origins if function.type.name == "ptr" else (
            ((function.place[0], False),) if function.place else function.origins)
        owner = self.by_uid(environment[0][0]) if environment else None
        external = owner.origins if owner and owner.type.name in {"closure", "FromFn"} else function.origins
        roots = {uid for uid, _ in environment} if info.invalidated_captures else set()
        if info.invalidated_external_captures:
            roots.update(uid for uid, _ in external)
        excluded = {uid for uid in (function.access_uid, owner.uid if owner else None) if uid is not None}
        for uid in roots:
            self.invalidate_storage(uid, node, "可能因闭包替换资源或追加内容而使旧缓冲区失效", excluded)
            # 这里的对象/新缓冲区仍存在；保留旧别名上的 unsafe，清掉区域旧状态。
            self.invalid_roots.pop(uid, None)

    def builtin_call(self, name, nodes, node):
        if name == FROM_FN:
            if len(nodes) != 1:
                self.fail(node, "from_fn 需要一个无参数回调，回调返回 Step[T]", "XE-CALL-0001")
            callback = self.infer(nodes[0])
            if callback.type == NEVER:
                return callback
            # 适配器保留自己的环境，通过可写指针重复调用，而非反复移动环境。
            self.iterator_callback_result(callback.type, node)
            self.consume(callback)
            return Value(Type("FromFn", (callback.type,)), node, origins=callback.origins)
        # 标准库沿用普通参数/类型规则，公开签名只在各库接口表中登记。
        # 用户/局部函数的名称解析已在 call() 中先行处理。
        standard_signature = io_function(name) or env_function(name)
        if standard_signature is not None:
            name = normalize_io_name(name)
            if not standard_signature.formatted:
                self.arguments(nodes, list(standard_signature.parameters), node)
                return Value(standard_signature.result, node)
        parts = name.split("::")
        if len(parts) == 2 and parts[0] in NUMERIC and parts[1] == "try_from":
            if len(nodes) != 1:
                self.fail(node, "数值 try_from 需要且仅需要一个参数", "XE-CALL-0001")
            value = self.infer(nodes[0])
            source = self.default(value.type)
            integers = NUMERIC - {"f32", "f64"}
            if source.name in {"f32", "f64"} or parts[0] in {"f32", "f64"}:
                self.fail(node, "数值 try_from 当前仅定义整数之间的检查转换；浮点策略尚未确定",
                          "XE-SEM-0001")
            if source.name not in integers:
                self.fail(nodes[0], "数值 try_from 的实参必须是整数", "XE-TYPE-0001")
            self.convert(value, source)
            self.consume(value)
            # ConversionError 是一个无资源负载的标准错误值，不臆造公开字段。
            # 转换的范围检查在后端执行；这里不将失败改成编译期拒绝。
            return Value(maybe(Type(parts[0]), CONVERSION_ERROR), node)
        if name == "format":
            self.fail(node, "内建 format(...) 字符串构造尚未在当前版本实现", "XE-SEM-0001",
                      "可使用已实现的 print/println，或定义普通 format 函数；不把目标库接口当作已实现能力")
        if standard_signature is not None and standard_signature.formatted:
            if not nodes:
                self.fail(node, "格式化调用需要格式字符串", "XE-CALL-0001")
            template = self.convert(self.infer(nodes[0]), STR)
            values = [self.infer(n) for n in nodes[1:]]
            if template.literal is None:
                self.fail(nodes[0], "当前阶段要求格式字符串为字面量", "XE-SEM-0001")
            try:
                parts = parse_format_template(template.literal)
                fields = [(field, spec, conversion) for _, field, spec, conversion in parts if field is not None]
            except ValueError:
                self.fail(nodes[0], "格式字符串花括号不匹配", "XE-FORMAT-0001")
            if len(fields) != len(values):
                self.fail(node, "格式占位符数量与参数不一致", "XE-FORMAT-0001")
            for (field, spec, conversion), value in zip(fields, values):
                issue = format_field_issue(field, spec, conversion)
                if issue:
                    self.fail(nodes[0], issue, "XE-SEM-0001")
                issue = format_argument_issue(value.type, spec)
                if issue:
                    self.fail(value.node, issue, "XE-TYPE-0001" if spec == "p" else "XE-SEM-0001")
                self.consume(value)
            return Value(UNIT, node)
        signatures = {"String::from": ([STR], STRING), "String::new": ([], STRING),
                      "File::open": ([STR], maybe(FILE, IO_ERROR)),
                      "File::create": ([STR], maybe(FILE, IO_ERROR)),
                      "panic": ([STR], NEVER)}
        if name not in signatures:
            return None
        parameters, result = signatures[name]
        self.arguments(nodes, parameters, node)
        return Value(result, node)

    def iterator_callback_result(self, callback, node):
        """构造调用和显式 FromFn[F] 类型使用相同校验，拒绝无效布局。"""
        repeated = Value(callback if callback.name == "ptr" else ptr(callback, True), node)
        signature = self.callable_signature(repeated, node)
        if len(signature.args) != 1:
            self.fail(node, "from_fn 回调不能有参数", "XE-ITER-0001")
        result = signature.args[-1]
        if result.name == "maybe":
            self.fail(node, "旧迭代协议 T? 已移除：from_fn 回调需要返回 Step[T]", "XE-ITER-0001",
                      "用 Step::Item[value] 产生元素，用 Step::Stop 结束迭代")
        if result.name != "Step" or len(result.args) != 1:
            self.fail(node, "from_fn 回调必须返回 Step[T]", "XE-ITER-0001",
                      "用 Step::Item[value] 产生元素，用 Step::Stop 结束迭代")
        return result

    def from_fn_result(self, receiver, node):
        """复用闭包的返回来源/失效摘要，不把所有视图都绑到迭代器存储。

        拥有闭包环境存在 FromFn 字段里，指针闭包环境存在外部对象里。
        正文返回字面量、外部指针或内部 String 视图应保留各自来源。
        """
        base = receiver.type.args[0] if receiver.type.name == "ptr" else receiver.type
        callback = callback_type(base)
        signature = callback.args[0] if callback.name == "ptr" else callback
        if callback.name == "ptr":
            origins = receiver.origins
            owner = self.by_uid(origins[0][0]) if origins else None
            if owner and owner.type.name == "FromFn":
                origins = owner.origins
        elif receiver.type.name == "ptr":
            origins = receiver.origins
        else:
            origins = ((receiver.place[0], True),) if receiver.place else receiver.origins
        info = self.closures.get(signature)
        function = Value(ptr(signature, True), node, origins=origins if info else ())
        value = self.finish_invoke(function, [], node)
        if has_unsafe(receiver.type) and self.carries_borrow(value.type):
            value.type = mark_unsafe(value.type)
        if (info and info.storage_captures and callback.name != "ptr" and
                receiver.type.name != "ptr" and not receiver.place):
            self.warn_pointer(value, "临时迭代器产生的内部地址或视图可能在使用前失效", "XE-PTR-0003")
        return value

    def method(self, callee, nodes, node, type_arguments=None):
        receiver = self.place(callee["object"])
        base = receiver.type.args[0] if receiver.type.name == "ptr" else receiver.type
        name = callee["field"]
        element = base.args[0] if base.args else I32
        if base.name in {"FromFn", "Bytes", "Chars"} and name == "next":
            if nodes or type_arguments is not None:
                self.fail(node, "next() 不接受参数或类型附件", "XE-CALL-0001")
            writable = (receiver.type.mutable if receiver.type.name == "ptr" else
                        self.mutable_place(receiver) or not receiver.place and not receiver.borrowed)
            if not writable:
                self.fail(node, "next() 修改迭代状态，需要 let[mut] 或 T@[mut]", "XE-MUT-0001")
            if receiver.type.name != "ptr":
                self.note_closure_access(receiver, "mut")
            if base.name == "FromFn":
                return self.from_fn_result(receiver, node)
            return Value(Type("Step", (Type("u8" if base.name == "Bytes" else "char"),)), node)
        signature = self.methods.get((base.name, name))
        mutable, owning, receiver_loans = False, False, ()
        if signature:
            if not signature.parameters or signature.node["parameters"][0]["name"] != "self":
                self.fail(node, "关联函数不能使用对象调用", "XE-CALL-0001")
            assert signature.self_type is not None
            substitutions = {}
            self.unify_generic(signature.self_type, base, substitutions, node)
            owner = substitute(signature.self_type, substitutions)
            if owner != base:
                self.fail(node, f"方法属于 {owner}，不能用 {base} 调用")
            if type_arguments is not None:
                explicit = self.explicit_substitutions(signature, type_arguments, node)
                for key, type_ in explicit.items():
                    if key in substitutions and substitutions[key] != type_:
                        self.fail(node, "方法类型参数与对象类型冲突", "XE-GENERIC-0001")
                    substitutions[key] = type_
            self_parameter = substitute(signature.parameters[0], substitutions)
            temporary_receiver = not receiver.place and not receiver.origins
            if self_parameter.name == "ptr":
                writable = receiver.type.mutable if receiver.type.name == "ptr" else self.mutable_place(receiver)
                if self_parameter.mutable and not writable:
                    self.fail(node, "可写方法需要 let[mut] 或 T@[mut]", "XE-BORROW-0004")
                if self_parameter.mutable and receiver.type.name != "ptr":
                    self.note_closure_access(receiver, "mut")
                origins = receiver.origins or (((receiver.place[0], self_parameter.mutable),) if receiver.place else ())
                receiver = Value(ptr(base, self_parameter.mutable, has_unsafe(receiver.type)), receiver.node,
                                 receiver.place, origins, receiver.borrowed, receiver.access_uid)
            elif receiver.type.name == "ptr":
                if not self.copyable(base):
                    self.fail(node, f"{base}::{name} 的 self: {self_parameter} 会消耗资源；"
                              f"不能通过 {receiver.type} 指针调用", "XE-MOVE-0002",
                              "用拥有的值调用，或把方法定义为 self: Self@ / self: Self@[mut]")
                # Copy 接收器按已确认的规则复制所指值，不消耗指针或原对象。
                # 只有非 Copy 的 self: Self 才需要资源所有权而被拒绝。
                receiver = Value(base, receiver.node, receiver.place, receiver.origins, True, receiver.access_uid)
            result = self.apply_signature(signature, nodes, node, substitutions, [receiver])
            concrete = self.functions.get(self.call_targets.get(id(node), ""), signature)
            depends_on_receiver = concrete.borrow_parameters is None or 0 in concrete.borrow_parameters
            if (temporary_receiver and self_parameter.name == "ptr" and self.carries_borrow(result.type)
                    and (depends_on_receiver or not self.copyable(base))):
                self.warn_pointer(result, "临时对象产生的地址或视图可能在使用前失效", "XE-PTR-0003")
            return result
        elif base.name == "maybe" and name == "expect":
            parameters, result, owning = [STR], base.args[0], True
        else:
            table = {
                ("String", "len"): ([], USIZE, False), ("str", "len"): ([], USIZE, False),
                ("Array", "len"): ([], USIZE, False),
                ("Slice", "len"): ([], USIZE, False), ("SliceMut", "len"): ([], USIZE, False),
                ("String", "as_str"): ([], STR, False), ("String", "clone"): ([], STRING, False),
                ("String", "push_str"): ([STR], UNIT, True),
                ("String", "push_char"): ([Type("char")], UNIT, True),
                ("String", "bytes"): ([], Type("Bytes"), False),
                ("String", "chars"): ([], Type("Chars"), False),
                ("str", "bytes"): ([], Type("Bytes"), False),
                ("str", "chars"): ([], Type("Chars"), False),
                ("str", "byte_at"): ([USIZE], Type("u8"), False),
                ("str", "slice_bytes"): ([Type("Range", (USIZE,))], maybe(STR), False),
                ("str", "data"): ([], ptr(Type("u8")), False),
                ("File", "size"): ([], USIZE, False),
                ("File", "read_to_string"): ([], maybe(STRING, IO_ERROR), False),
                ("File", "write_all"): ([STR], maybe(UNIT, IO_ERROR), True),
                ("File", "flush"): ([], maybe(UNIT, IO_ERROR), True),
                ("Vec", "len"): ([], USIZE, False),
                ("Vec", "capacity"): ([], USIZE, False),
                ("Vec", "is_empty"): ([], BOOL, False),
                ("Vec", "push"): ([element], UNIT, True),
                ("Vec", "pop"): ([], maybe(element), True),
                ("Vec", "clear"): ([], UNIT, True),
                ("Vec", "reserve"): ([USIZE], UNIT, True),
                ("Vec", "as_slice"): ([], Type("Slice", (element,)), False),
                ("Vec", "as_slice_mut"): ([], Type("SliceMut", (element,)), True),
                ("Box", "ptr"): ([], ptr(element), False),
                ("Box", "ptr_mut"): ([], ptr(element, True), True),
                ("Box", "into_value"): ([], element, False),
                ("Shared", "ptr"): ([], ptr(element), False),
                ("Shared", "share"): ([], base, False),
                ("Shared", "weak"): ([], Type("Weak", (element,)), False),
                ("Weak", "share"): ([], base, False),
                ("Weak", "upgrade"): ([], maybe(Type("Shared", (element,)), Type("Expired")), False),
                ("Mutex", "lock"): ([], maybe(Type("MutexGuard", (element,)), Type("SyncError")), False),
                ("MutexGuard", "ptr"): ([], ptr(element), False),
                ("MutexGuard", "ptr_mut"): ([], ptr(element, True), True),
                ("Thread", "join"): ([], maybe(element, Type("ThreadError")), False),
                ("Array", "slice"): ([Type("Range", (USIZE,))], Type("Slice", (element,)), False),
                ("Array", "slice_mut"): ([Type("Range", (USIZE,))], Type("SliceMut", (element,)), True),
                ("SliceMut", "copy_from"): ([Type("Slice", (element,))], UNIT, True),
            }
            entry = table.get((base.name, name))
            if entry is None:
                self.fail(node, f"类型 {base} 没有已支持的方法 {name}", "XE-NAME-0001")
            parameters, result, mutable = entry
            owning = base.name == "Box" and name == "into_value" or base.name == "Thread" and name == "join"
            if base.name == "SliceMut" and name == "copy_from" and not self.copyable(element):
                self.fail(node, "copy_from 的元素必须是 Copy；资源不能被重复复制", "XE-OWN-0001")
        if owning:
            if receiver.type.name == "ptr":
                if not self.copyable(base):
                    self.fail(node, f"{base}::{name} 会消耗资源；不能通过 {receiver.type} 指针调用",
                              "XE-MOVE-0002", "用拥有的结果值调用，或显式创建新的拥有值")
                receiver = Value(base, receiver.node, receiver.place, receiver.origins, True, receiver.access_uid)
            self.consume(receiver)
        elif self.check_borrows:
            writable = receiver.type.mutable if receiver.type.name == "ptr" else self.mutable_place(receiver)
            if mutable and not writable:
                self.fail(node, "可写方法需要 let[mut] 或 T@[mut]", "XE-BORROW-0004")
            if mutable and receiver.type.name != "ptr":
                self.note_closure_access(receiver, "mut")
            origins = receiver.origins if receiver.type.name == "ptr" else (((receiver.place[0], mutable),) if receiver.place else ())
            self.loan(origins, node, receiver.access_uid)
            receiver_loans = origins
        # self 的短借用覆盖整个参数求值过程，不能在最后使用位置提前释放。
        saved = list(self.temporary_loans)
        self.temporary_loans.extend(receiver_loans)
        try:
            values = self.arguments(nodes, parameters, node)
        finally:
            self.temporary_loans = saved
        if base.name == "Vec" and name == "push" and self.carries_borrow(element):
            # 容器保存的地址来源应随 Vec 移动、pop 和函数返回传播。
            owners = ({receiver.place[0]} if receiver.place and receiver.type.name != "ptr" else
                      {uid for uid, _ in receiver.origins})
            for uid in owners:
                binding = self.by_uid(uid)
                if binding and binding.type.name == "Vec":
                    binding.origins = tuple(set(binding.origins + values[0].origins))
        if (base == STRING and name in {"push_str", "push_char"} or
                base.name == "Vec" and name in {"push", "pop", "clear", "reserve"}):
            roots = {uid for uid, _ in receiver.origins}
            if receiver.place and receiver.type.name != "ptr":
                roots.add(receiver.place[0])
            for uid in roots:
                # push_str 可能搬动字节缓冲区，但不搬动接收者 String 本身。
                # 尤其不能把捕获的 String@ 自己标 unsafe，污染随后取得的新视图。
                excluded = (receiver.access_uid,) if receiver.access_uid is not None else ()
                self.invalidate_storage(uid, node, "的数据缓冲区可能因追加而重新分配", excluded)
                # 对象自身仍然存在；已有别名已标风险，新视图对应新缓冲区。
                self.invalid_roots.pop(uid, None)
        origins = ()
        if self.carries_borrow(result):
            depends_on_receiver = (signature is not None and
                                   (signature.borrow_parameters is None or 0 in signature.borrow_parameters))
            temporary_storage = (base.name == "Array" and not receiver.place or
                                 depends_on_receiver and not owning and not receiver.place and not receiver.origins)
            risky_temporary = temporary_storage or not self.copyable(base) and not owning and not receiver.place and not receiver.origins
            receiver_origins = receiver.origins or (((receiver.place[0], mutable),) if receiver.place else ())
            if base.name in {"Box", "Shared", "MutexGuard"} and name in {"ptr", "ptr_mut"} and receiver.type.name != "ptr" and receiver.place:
                # ptr() 指向 Box 自己的堆存储，不只是 T 中保存的外部地址。
                # 若 T 本身含指针，不能因已有 payload origins 而漏掉本地
                # Box 的生命周期，否则返回局部 Box 的数据指针会漏报悬垂。
                receiver_origins = tuple(set(receiver_origins + ((receiver.place[0], mutable),)))
            if (base.name == "Vec" and name == "pop" or
                    base.name == "Box" and name == "into_value" or
                    base.name in SYNC_TYPES and name not in {"ptr", "ptr_mut"}):
                # 取出的值只保留自己携带的地址来源，不依赖已释放的外层容器。
                origins = receiver.origins
            else:
                origins = (self.call_origins(signature, [Value(receiver.type, node, origins=receiver_origins), *values])
                           if signature else receiver_origins + tuple(o for v in values for o in v.origins))
            if signature:
                result = self.call_result_type(signature, [receiver, *values], result)
            elif has_unsafe(receiver.type):
                result = mark_unsafe(result)
            value = Value(result, node, origins=origins)
            if risky_temporary:
                self.warn_pointer(value, "临时对象产生的地址或视图可能在使用前失效", "XE-PTR-0003")
            return value
        return Value(result, node, origins=origins)

    def handle(self, handler, payloads, expected=None, lift=False):
        if handler["kind"] == "FunctionTarget":
            target = handler["target"]
            resolved = self.signature_target(target)
            if resolved and resolved[0].generics:
                signature, substitutions = resolved
                if len(signature.parameters) != len(payloads):
                    self.fail(handler, "管道函数参数数量与分支载荷不一致", "XE-CALL-0001")
                for pattern, value in zip(signature.parameters, payloads):
                    parameter = substitute(pattern, substitutions)
                    if self.has_variable(parameter):
                        self.unify_generic(parameter, value.type, substitutions, handler)
                concrete = self.instantiate(signature, substitutions, handler)
                self.call_targets[id(target)] = concrete.node["name"]
                self.call_targets[id(handler)] = concrete.node["name"]
            function = self.infer(handler["target"])
            signature_type = self.callable_signature(function, handler)
            parameters = list(signature_type.args[:-1])
            if len(parameters) != len(payloads):
                self.fail(handler, "管道函数参数数量与分支载荷不一致", "XE-CALL-0001")
            for value, type_ in zip(payloads, parameters):
                self.consume(self.convert(value, type_))
            result = self.finish_invoke(function, payloads, handler)
            # 命名处理函数和参数绑定处理函数遵守同一结果上下文：
            # 只有在允许成功返回简写的位置，T 才能升为 T?。
            if expected:
                result = self.convert(result, expected, lift)
            self.consume(result)
            return result
        parameters = handler["parameters"]
        ignore = len(parameters) == 1 and parameters[0]["name"] == "_" and parameters[0]["type"] is None
        if not ignore and len(parameters) != len(payloads):
            self.fail(handler, "分支绑定数量与载荷不一致（None/无载荷分支使用 _）", "XE-CALL-0001")
        self.scopes.append({})
        if not ignore:
            for parameter, value in zip(parameters, payloads):
                type_ = self.type_of(parameter["type"]) if parameter["type"] else value.type
                value = self.convert(value, type_)
                self.declare(parameter["name"], value.type, parameter, parameter["mutable"], origins=value.origins)
        result = self.infer(handler["body"], expected, lift)
        if expected:
            result = self.convert(result, expected, lift)
        self.consume(result)
        self.scope_escape(result, handler, set(b.uid for b in self.scopes[-1].values()))
        self.scopes.pop()
        return Value(result.type, handler, origins=result.origins)

    def selector_cases(self, selector, base, variants):
        """验证一层选择器，返回独立的常量/_ 选择行。

        选择器只筛选，不定义参数。枚举完整载荷按原顺序交给处理器，
        元组则交整个元组；过滤位置的常量或 _ 不改变这个传递规则。
        """
        kind = selector["kind"]
        if kind == "OrSelector":
            cases = [case for choice in selector["choices"]
                     for case in self.selector_cases(choice, base, variants)]
            if any(case.handler_types != cases[0].handler_types for case in cases):
                self.fail(selector, "组合选择器必须给处理器传入相同数量和类型的载荷",
                          "XE-MATCH-0004", "不同载荷签名分别写成两个 :> 分支")
            return cases
        if kind == "WildcardSelector":
            return [SelectorCase("*", (), (base,))]
        if kind == "LiteralSelector":
            literal = self.convert(self.infer(selector["value"], base), base)
            key = literal_key(literal.literal, base, selector["value"]["literal_kind"])
            return [SelectorCase("$value", (key,), (base,))]
        if kind == "TupleSelector":
            if base.name != "tuple":
                self.fail(selector, "tuple 选择器只用于元组", "XE-MATCH-0001")
            key, types, filters = "$tuple", list(base.args), selector["elements"]
            # tuple 选择器只筛选成员，沿用冻结规则传入整个元组。
            # 显式 let tuple[...] 解包与 selector 必须保持不同职责。
            payload_types = (base,)
        elif kind == "VariantSelector":
            path = selector["path"]["parts"]
            key, owner = path[-1], path[0]
            alias_type = self.resolve_alias(owner) if owner in self.aliases else None
            if alias_type:
                owner = alias_type.name
            if (len(path) != 2 or owner != ("Maybe" if base.name == "maybe" else base.name)
                    or key not in variants or alias_type is not None and alias_type != base):
                self.fail(selector, "变体不属于被匹配类型", "XE-MATCH-0001")
            types, filters = variants[key], selector.get("filters")
            payload_types = None
            if filters is None:
                return [SelectorCase(key, (None,) * len(types), tuple(types))]
        else:
            self.fail(selector, "该选择器不能用于一层模式匹配", "XE-MATCH-0001")
        if len(filters) != len(types):
            self.fail(selector, f"选择器需要 {len(types)} 个直接载荷位置，实际 {len(filters)} 个",
                      "XE-MATCH-0001")

        def choices(filter_, type_):
            if filter_["kind"] == "WildcardSelector":
                return [None]
            if filter_["kind"] == "OrSelector":
                return [value for choice in filter_["choices"] for value in choices(choice, type_)]
            if filter_["kind"] == "LiteralSelector":
                literal = self.convert(self.infer(filter_["value"], type_), type_)
                return [literal_key(literal.literal, type_, filter_["value"]["literal_kind"])]
            self.fail(filter_, "载荷过滤只允许直接字面量或 _，不能递归解构",
                      "XE-MATCH-0001", "把载荷交给 :> 参数后再显式匹配下一层")

        options = [choices(filter_, type_) for filter_, type_ in zip(filters, types)]
        return [SelectorCase(key, tuple(values), tuple(types), payload_types) for values in product(*options)]

    def branch(self, node, expected=None, lift=False):
        value = self.infer(node["input"])
        borrowed = "borrow" in node["modifiers"]
        mutable = "mut" in node["modifiers"]
        modifiers = node["modifiers"]
        if len(modifiers) != len(set(modifiers)) or any(m not in {"borrow", "mut", "unsafe"} for m in modifiers):
            self.fail(node, "未知或重复的匹配借用修饰", "XE-TYPE-0001")
        base = value.type.args[0] if value.type.name == "ptr" else value.type
        if value.type.name == "ptr" and not borrowed:
            self.fail(node, "指针匹配必须显式使用 ?[@]，不能取得其指向资源", "XE-MOVE-0002")
        if mutable and self.check_borrows and not (value.type.mutable if value.type.name == "ptr" else self.mutable_place(value)):
            self.fail(node, "可写匹配需要可写对象", "XE-BORROW-0004")
        origins = value.origins
        if borrowed and value.type.name != "ptr" and value.place:
            # 指向负载描述符的指针既依赖对象存储，也可能依赖视图底层数据。
            # 只保留底层来源会错误允许返回局部枚举内的 str@。
            origins = tuple(set(origins + ((value.place[0], mutable),)))
        elif not origins and value.place:
            origins = ((value.place[0], mutable),)
        if borrowed:
            if self.check_borrows and not value.place and not value.origins:
                self.fail(node, "借用匹配需要稳定的存储位置；先绑定被匹配值", "XE-BORROW-0001")
            self.loan(origins, node, value.access_uid)
        else:
            self.consume(value)
        if self.unresolved(base):
            self.fail(node, "匹配前需要确定完整枚举类型，请给输入值添加类型注解")
        declaration = self.types.get(base.name, {})
        substitutions = {"$" + p["name"]: t for p, t in zip(declaration.get("generics", []), base.args)}
        variants = {v["name"]: [substitute(self.type_of(t, self.generic_set(declaration)), substitutions) for t in v["payload"]]
                    for v in declaration.get("variants", [])}
        if base.name == "maybe":
            variants = {"Yes": [base.args[0]], "None" if base.args[1] == NONE else "No": [] if base.args[1] == NONE else [base.args[1]]}
        before = deepcopy(self.scopes)
        results, states, previous_cases = [], [], []
        full_cases = ([SelectorCase(name, (None,) * len(types), tuple(types))
                       for name, types in variants.items()] if variants else
                      [SelectorCase("$tuple", (None,) * len(base.args), base.args)] if base.name == "tuple" else
                      [SelectorCase("$value", (None,), (base,))])
        for arm in node["arms"]:
            self.scopes = deepcopy(before)
            if all(covered(case, previous_cases) for case in full_cases):
                self.fail(arm, "该分支不可达：前面的分支已覆盖全部情况", "XE-MATCH-0002")
            if arm["kind"] == "ChannelArm":
                if base.name != "maybe":
                    self.fail(arm, "1>/2> 仅用于 T? 或 T?[E]", "XE-MATCH-0001")
                key = "Yes" if arm["channel"] == 1 else ("None" if base.args[1] == NONE else "No")
                types = variants[key]
                cases = [SelectorCase(key, (None,) * len(types), tuple(types))]
            else:
                cases = self.selector_cases(arm["selector"], base, variants)
                types = list(cases[0].handler_types)
            for index, case in enumerate(cases):
                # | 的选择集合与书写次序无关。检测同一组合内冗余时
                # 同时比较两边，不能让 "true | _" 和 "_ | true"
                # 因排序不同而一个通过、另一个报错。
                other_choices = cases[:index] + cases[index + 1:]
                if covered(case, previous_cases) or covered(case, other_choices):
                    self.fail(arm, "选择器重复或冗余：已由之前分支或同一组合的其他选择器完整覆盖",
                              "XE-MATCH-0002")
            previous_cases.extend(cases)
            if (not borrowed and self.has_drop(base) and any(case.tag != "*" for case in cases)
                    and any(not self.copyable(t) for t in types)):
                self.fail(arm, "自定义 Drop 枚举不能移出资源载荷", "XE-OWN-0002")
            if has_unsafe(value.type):
                types = [mark_unsafe(t) if self.carries_borrow(t) else t for t in types]
            payloads = [Value(ptr(t, mutable, has_unsafe(value.type)) if borrowed else t,
                              arm, origins=origins if borrowed or self.carries_borrow(t) else ()) for t in types]
            result = self.handle(arm["handler"], payloads, expected, lift)
            results.append(result)
            if result.type != NEVER:
                states.append(deepcopy(self.bindings()))
        self.scopes = before
        self.merge({}, states)
        if not all(covered(case, previous_cases) for case in full_cases):
            self.fail(node, "模式匹配没有覆盖所有情况", "XE-MATCH-0003", "补齐所有变体，或添加 _ 分支")
        return self.common(results, node)

    def loop(self, node):
        source, element, iterator = None, None, False
        signature: Signature | None = None
        if node["kind"] != "While":
            source = self.infer(node["source"])
            if source.type == NEVER:
                return Value(NEVER, node)
            base = source.type.args[0] if source.type.name == "ptr" else source.type
            if source.type.name == "Range":
                element = source.type.args[0]
            elif base.name in {"Array", "Slice", "SliceMut", "Vec"}:
                element = ptr(base.args[0], base.name == "SliceMut" and
                              (source.type.name != "ptr" or source.type.mutable))
            else:
                iterator = True
                if source.type.name == "ptr" and not source.type.mutable:
                    self.fail(node, "迭代会改变状态，请传入 iterator@[mut]", "XE-MUT-0001")
                signature = self.methods.get((base.name, "next"))
                if base.name == "FromFn":
                    result = next_result(base)
                    self.for_iterators[id(node)] = None
                elif base.name in {"Bytes", "Chars"}:
                    result = Type("Step", (Type("u8" if base.name == "Bytes" else "char"),))
                    self.for_iterators[id(node)] = base.name
                elif signature:
                    assert signature.self_type is not None
                    substitutions = {}
                    self.unify_generic(signature.self_type, base, substitutions, node)
                    if (len(signature.parameters) != 1 or
                            substitute(signature.parameters[0], substitutions) != ptr(base, True) or
                            signature.node["parameters"][0]["name"] != "self"):
                        self.fail(node, "迭代器需要 fn next(self: Self@[mut]) -> Step[T]", "XE-ITER-0001")
                    result = self.apply_signature(signature, [], node, substitutions,
                        [Value(ptr(base, True), node, origins=source.origins)]).type
                    self.for_iterators[id(node)] = self.functions.get(self.call_targets.get(id(node), ""), signature)
                else:
                    self.fail(node, "for 需要范围、数组、切片或提供 next(self: Self@[mut]) -> Step[T] 的对象",
                              "XE-ITER-0001")
                if result.name == "maybe":
                    self.fail(node, "旧迭代协议 T? 已移除：next() 必须返回 Step[T]", "XE-ITER-0001",
                              "用 Step::Item[value] 产生元素，用 Step::Stop 结束迭代")
                if result.name != "Step" or len(result.args) != 1:
                    self.fail(node, "迭代器 next() 必须返回 Step[T]", "XE-ITER-0001",
                              "用 Step::Item[value] 产生元素，用 Step::Stop 结束迭代")
                element = result.args[0]
                # 先转移拥有源，再进入循环：即使循环一次都不执行，转移也已发生。
                self.consume(source)
        before = deepcopy(self.scopes)
        self.loop_depths.append(len(self.scopes)-1)
        self.scopes.append({})
        if node["kind"] == "While":
            self.convert(self.infer(node["condition"]), BOOL)
        else:
            assert source is not None and element is not None  # for 的源与元素已在上方解析。
            annotation = self.type_of(node["type"]) if node["type"] else self.default(element)
            self.convert(Value(element, node), annotation)
            origins = source.origins
            if iterator and source.type.name != "ptr":
                owner = self.declare("$iteration-owner", source.type, node, mutable=True, origins=origins)
                if source.type.name == "FromFn":
                    origins = self.from_fn_result(Value(source.type, node, (owner.uid, ()), origins), node).origins
                elif self.carries_borrow(element):
                    assert signature is not None  # 内置 Bytes/Chars 只产生基本类型。
                    if signature.borrow_parameters is None or 0 in signature.borrow_parameters:
                        origins = tuple(set(origins + ((owner.uid, False),)))
            elif iterator and source.type.name == "ptr" and source.type.args[0].name == "FromFn":
                origins = self.from_fn_result(source, node).origins
            elif element.name == "ptr" and source.type.name in {"Array", "Vec"} and not source.place:
                # for 的临时数组确实由后端拥有至循环结束；引入不可命名的
                # 所有者，让元素指针不能从循环返回或写到外层后继续使用。
                owner = self.declare("$iteration-owner", source.type, node)
                origins = tuple(set(origins + ((owner.uid, element.mutable),)))
            elif not iterator and element.name == "ptr" and source.place and not source.borrowed:
                origins = tuple(set(origins + ((source.place[0], element.mutable),)))
            if element.name == "ptr":
                origins = tuple((uid, element.mutable) for uid, _ in origins)
            self.declare(node["name"], annotation, node, origins=origins)
        self.block(node["body"])
        self.scope_escape(Value(UNIT, node), node, {b.uid for b in self.scopes[-1].values()})
        self.scopes.pop()
        after = deepcopy(self.bindings())
        self.scopes = before
        self.merge({}, [deepcopy(self.bindings()), after])
        self.loop_depths.pop()
        return Value(UNIT, node)

    def anonymous(self, node):
        captures = []
        names = [capture["name"] for capture in node["captures"]]
        if len(names) != len(set(names)):
            self.fail(node, "闭包捕获名称重复", "XE-NAME-0002")
        for capture in node["captures"]:
            binding = self.lookup(capture["name"], capture)
            value = Value(binding.type, capture, (binding.uid, ()), binding.origins,
                          borrowed=binding.capture_borrowed, access_uid=binding.uid,
                          writable=binding.capture_writable if binding.capture_borrowed else None)
            if capture["borrow"]:
                mods = [m["name"] for m in capture["modifiers"]]
                if len(mods) != len(set(mods)) or any(m not in {"mut", "unsafe"} for m in mods):
                    self.fail(capture, "未知或重复的捕获借用修饰")
                mutable = "mut" in mods
                if self.check_borrows and mutable and not self.mutable_place(value):
                    self.fail(capture, "可写捕获需要 let[mut] 存储", "XE-MUT-0001")
                if mutable:
                    self.note_closure_access(value, "mut")
                origins = value.origins if value.borrowed else ((binding.uid, mutable),)
                origins = tuple((uid, mutable) for uid, _ in origins)
                self.loan(origins, capture)
                value = Value(ptr(binding.type, mutable, "unsafe" in mods), capture, origins=origins)
            else:
                self.consume(value)
                # 从别名按值复制 Copy 数据后，新环境不再依赖原存储。
                value = Value(value.type, capture,
                              origins=value.origins if self.carries_borrow(value.type) else ())
            captures.append((capture["name"], value))
        saved = (self.scopes, self.result, self.last_uses, self.loop_depths,
                 self.parameter_roots, self.return_origins, self.invalid_roots,
                 self.function_unsafe_return, self.temporary_loans,
                 self._closure_bindings, self._closure_mode, self.position,
                 self._closure_invalidated, self._closure_external_invalidated, self._closure_external_roots)
        # 失败也恢复外层上下文，避免一个坏闭包改变后续函数的检查结果。
        try:
            self.scopes, self.result = [{}], self.type_of(node["result"])
            self.last_uses, self.loop_depths, self.temporary_loans = names_used(node["body"]), [], []
            self.parameter_roots = self.static_roots | {uid for _, value in captures for uid, _ in value.origins}
            self.return_origins, self.invalid_roots = set(), {}
            self.function_unsafe_return = False
            self._closure_bindings, self._closure_mode = {}, "read"
            self._closure_invalidated, self._closure_external_invalidated = set(), set()
            self._closure_external_roots = {}
            capture_bindings = []
            for index, ((name, value), capture_node) in enumerate(zip(captures, node["captures"])):
                # 环境保存 T@，但正文读到 T；写入借用别名只影响外部对象。
                borrowed = capture_node["borrow"]
                body_type = value.type.args[0] if borrowed else value.type
                binding = self.declare(name, body_type, capture_node, mutable=True, origins=value.origins)
                binding.capture_borrowed = borrowed
                binding.capture_writable = value.type.mutable if borrowed else False
                capture_bindings.append(binding)
                self._closure_bindings[binding.uid] = index
                for uid, _ in value.origins:
                    self._closure_external_roots.setdefault(uid, set()).add(index)
                self.parameter_roots.add(binding.uid)
            parameters, parameter_sources = [], {}
            for index, parameter in enumerate(node["parameters"]):
                if parameter["type"] is None:
                    self.fail(parameter, "独立闭包参数需要类型注解", "XE-TYPE-0001")
                type_ = self.type_of(parameter["type"])
                parameters.append(type_)
                binding = self.declare(parameter["name"], type_, parameter, parameter["mutable"], parameter=True)
                if self.carries_borrow(type_):
                    self.uid += 1
                    self.parameter_roots.add(self.uid)
                    parameter_sources[self.uid] = index
                    binding.origins = ((self.uid, type_.mutable),)
            result = self.block(node["body"], self.result, True)
            self.convert(result, self.result, True)
            self.escape(result, function_exit=True)
            self.return_origins.update(result.origins)
            result_type = self.result
            risky = has_unsafe(result.type) or self.function_unsafe_return
            if (self.carries_borrow(result_type) or result_type.name == "fn") and risky:
                result_type = mark_unsafe(result_type)
            type_ = callable_type(parameters, result_type, bool(captures), id(node) if captures else None)
            if captures:
                roots = {uid for uid, _ in self.return_origins}
                storage = frozenset(index for index, binding in enumerate(capture_bindings) if binding.uid in roots)
                dependencies = storage | frozenset(index for index, (_, value) in enumerate(captures)
                                                   if any(uid in roots for uid, _ in value.origins))
                self.closures[type_] = ClosureInfo(node, tuple((name, value.type) for name, value in captures),
                    tuple(parameters), result_type, self._closure_mode,
                    frozenset(parameter_sources[uid] for uid in roots if uid in parameter_sources),
                    dependencies, storage, risky, frozenset(self._closure_invalidated),
                    frozenset(self._closure_external_invalidated))
            return Value(type_, node, origins=tuple(o for _, value in captures for o in value.origins))
        finally:
            (self.scopes, self.result, self.last_uses, self.loop_depths,
             self.parameter_roots, self.return_origins, self.invalid_roots,
             self.function_unsafe_return, self.temporary_loans,
             self._closure_bindings, self._closure_mode, self.position,
             self._closure_invalidated, self._closure_external_invalidated, self._closure_external_roots) = saved


def check_source(text: str, filename: str = "<input>", check_borrows: bool = True) -> list[Diagnostic]:
    """解析失败也使用同一种定位诊断；语法成功才进入语义阶段。"""
    from .parser import parse_source
    source = Source(text, filename)
    try:
        tree = parse_source(text, filename)
        return Checker(source, tree, check_borrows).check()
    except Diagnostic as error:
        return [error]
