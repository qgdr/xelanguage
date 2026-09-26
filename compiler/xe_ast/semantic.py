"""单文件语义检查：名称、类型、初始化、移动、分支合并和基础借用检查。

这是 AST 上的参考检查器，不生成代码。每个函数至多报告一个主错误，继续检查其他函数。
借用使用保守的最后引用位置分析；它不是完整 Rust 区域求解器。不能证明的复杂规则应报错，
不能用 Unknown/Any 吞掉错误。标准库接口在 builtin_call/method 中有明确白名单。
"""
from copy import deepcopy
from dataclasses import dataclass, field
import string
from typing import Any
from .source import Source, Diagnostic
from .typesys import (
    Type, NUMERIC, PRIMITIVES, STANDARD, UNIT, NEVER, BOOL, STR, I32, USIZE,
    INT_LITERAL, UNKNOWN, NONE, STRING, FILE, IO_ERROR,
    ptr, maybe, callable_type, substitute,
)

Node = dict[str, Any]


@dataclass
class Value:
    type: Type
    node: Node
    place: tuple[int, tuple[str, ...]] | None = None
    # (拥有者 uid, 是否可写)。str/切片/闭包也可以携带这些来源。
    origins: tuple[tuple[int, bool], ...] = ()
    borrowed: bool = False
    access_uid: int | None = None
    literal: Any = None


@dataclass
class Binding:
    uid: int
    name: str
    type: Type
    node: Node
    mutable: bool = False
    initialized: bool = True
    moved: bool = False
    moved_fields: set[tuple[str, ...]] = field(default_factory=set)
    origins: tuple[tuple[int, bool], ...] = ()
    last_use: int = -1
    parameter: bool = False


@dataclass
class Signature:
    node: Node
    parameters: list[Type]
    result: Type
    generics: set[str] = field(default_factory=set)
    self_type: Type | None = None


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


class Checker:
    def __init__(self, source: Source, tree: Node, check_borrows: bool = False):
        self.source, self.tree, self.check_borrows = source, tree, check_borrows
        self.types: dict[str, Node] = {}
        self.functions: dict[str, Signature] = {}
        self.methods: dict[tuple[str, str], Signature] = {}
        self.constants: dict[str, Value] = {}
        self.drop_types: set[str] = set()
        self.copy_types: set[str] = set()
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
        # 调用参数中的临时借用也须互斥，不能因为没赋给名字就逃过检查。
        self.temporary_loans: list[tuple[int, bool]] = []
        # 输入所携带的外部区域，区别于“参数变量本身”的栈存储。
        self.parameter_roots: set[int] = set()

    def fail(self, node: Node, message: str, code="XE-TYPE-0001", hint=None):
        span = node["span"]
        raise Diagnostic(self.source, span["start"]["offset"], span["end"]["offset"],
                         message, code, hint)

    def offset(self, node: Node):
        return node["span"]["start"]["offset"]

    def generic_set(self, node):
        return {p["name"] for p in node.get("generics", [])}

    def type_of(self, node: Node | None, generics=None, self_type=None) -> Type:
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
            if name not in PRIMITIVES | STANDARD and name not in self.types:
                self.fail(node, f"未知类型 {name}", "XE-NAME-0001")
            args = []
            for arg in node["arguments"]:
                if arg["kind"] == "Literal":
                    args.append(Type(str(arg["value"])))
                else:
                    args.append(self.type_of(arg, generics, self_type))
            arities = {"Array": 2, "Vec": 1, "Slice": 1, "SliceMut": 1, "RawPtr": 1,
                       "Box": 1, "Map": 2, "Set": 1, "Range": 1, "Iterator": 1}
            arity = len(self.types[name].get("generics", [])) if name in self.types else arities.get(name, 0)
            if len(args) != arity:
                self.fail(node, f"{name} 需要 {arity} 个类型附件，实际为 {len(args)} 个")
            if name == "Array" and not args[1].name.isdigit():
                self.fail(node, "Array 的第二个附件必须是非负整数长度")
            return Type(name, tuple(args))
        if kind == "PointerType":
            base = self.type_of(node["target"], generics, self_type)
            if base.name == "ptr" and self.check_borrows:
                self.fail(node, "检查模式下的多级指针区域投影尚未实现", "XE-SEM-0001")
            if base == STR:
                self.fail(node, "str 已经是地址与长度视图，禁止 str@", "XE-TYPE-0003")
            modifiers = [m["name"] for m in node["modifiers"]]
            if len(modifiers) != len(set(modifiers)):
                self.fail(node, "借用修饰符重复")
            if any(m not in {"mut"} | set(generics) for m in modifiers):
                self.fail(node, "未知借用修饰符")
            if modifiers and not self.check_borrows:
                self.fail(node, "借用修饰需要开启 --check-borrows", "XE-BORROW-0001")
            return ptr(base, "mut" in modifiers)
        if kind == "MaybeType":
            error = self.type_of(node["error"], generics, self_type) if node["error"] else NONE
            return maybe(self.type_of(node["value"], generics, self_type), error)
        if kind == "NoneTypeMarker":
            return NONE
        if kind == "FunctionType":
            return callable_type([self.type_of(p, generics, self_type) for p in node["parameters"]],
                                 self.type_of(node["result"], generics, self_type))
        if kind == "TupleType":
            return Type("tuple", tuple(self.type_of(t, generics, self_type) for t in node["elements"]))
        self.fail(node, f"尚未支持的类型结构 {kind}", "XE-SEM-0001")

    def copyable(self, type_: Type, visited=None) -> bool:
        if type_.name in PRIMITIVES | {"$integer", "fn", "None"}:
            return True
        if type_.name in self.copy_generics:
            return True
        if type_.name == "ptr":
            return not type_.mutable or not self.check_borrows
        if type_.name in {"tuple", "Array", "maybe"}:
            return all(self.copyable(arg, visited) for arg in type_.args
                       if not arg.name.isdigit())
        if type_.name in {"Slice", "RawPtr", "Range"}:
            return True
        if type_.name in self.copy_types and type_.name not in self.drop_types:
            return True
        return False

    def carries_borrow(self, type_: Type) -> bool:
        if type_.name in {"ptr", "str", "Slice", "SliceMut", "closure"}:
            return True
        if type_.name in {"maybe", "tuple", "Array"}:
            return any(self.carries_borrow(t) for t in type_.args)
        # 用户结构体是否有借用字段由构造 Value.origins 同样跟踪。
        return type_.name in self.types

    def signature(self, node, self_type=None, extra_generics=None):
        if any(p["category"] == "region" for p in node.get("generics", [])):
            self.fail(node, "命名区域求解尚未实现；目前支持单文件基础借用检查", "XE-SEM-0001")
        generics = self.generic_set(node) | (extra_generics or set())
        return Signature(node,
            [self.type_of(p["type"], generics, self_type) for p in node["parameters"]],
            self.type_of(node["result"], generics, self_type), generics, self_type)

    def collect(self):
        seen = set()
        for node in self.tree["items"]:
            if node["kind"] in {"Struct", "Enum", "Trait", "Function", "Constant"}:
                name = node["name"]
                if name in seen:
                    self.fail(node, f"顶层名称 {name} 重复", "XE-NAME-0002")
                seen.add(name)
                if node["kind"] in {"Struct", "Enum", "Trait"}:
                    self.types[name] = node
        for node in self.tree["items"]:
            if node["kind"] == "Impl" and node["trait"]:
                if node["trait"]["kind"] != "NamedType" or node["target"]["kind"] != "NamedType":
                    self.fail(node, "此阶段 impl 仅支持命名类型", "XE-SEM-0001")
                trait = "::".join(node["trait"]["path"]["parts"])
                target = "::".join(node["target"]["path"]["parts"])
                if trait not in {"Drop", "Copy"}:
                    self.fail(node, "通用 Trait 实现检查尚未实现", "XE-SEM-0001")
                if trait == "Drop":
                    self.drop_types.add(target)
                if trait == "Copy":
                    self.copy_types.add(target)
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
                    if drop.parameters != [ptr(target, self.check_borrows)] or drop.result != UNIT:
                        self.fail(node, "drop 签名必须为 fn drop(self: Self@[mut])；默认模式使用 Self@", "XE-OWN-0002")
                if trait == "Copy" and node["methods"]:
                    self.fail(node, "Copy 是无方法标记 Trait", "XE-OWN-0001")
                for method in node["methods"]:
                    key = (target.name, method["name"])
                    if key in self.methods:
                        self.fail(method, "方法名称重复", "XE-NAME-0002")
                    self.methods[key] = self.signature(method, target, self.generic_set(node))
            elif kind == "Use":
                self.fail(node, "当前语义阶段只检查单文件，尚未加载 use 依赖", "XE-SEM-0001",
                          "将相关声明放在同一文件，或等待模块加载阶段")
        # 验证用户数据字段，不能把未知字段类型留给函数体来猜。
        for name, node in self.types.items():
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
            if name in self.copy_types:
                if name in self.drop_types:
                    self.fail(node, "Copy 与 Drop 互斥", "XE-OWN-0001")
                payloads = ([f["type"] for f in node.get("fields", [])] +
                            [t for v in node.get("variants", []) for t in v["payload"]])
                if any(not self.copyable(self.type_of(t, self.generic_set(node))) for t in payloads):
                    self.fail(node, "含资源字段的类型不能实现 Copy", "XE-OWN-0001")

    def check(self) -> list[Diagnostic]:
        try:
            self.collect()
            for node in self.tree["items"]:
                if node["kind"] == "Constant":
                    value = self.infer(node["value"], self.type_of(node["type"]))
                    self.constants[node["name"]] = self.convert(value, self.type_of(node["type"]))
                    if not self.copyable(value.type):
                        self.fail(node, "当前 const 只支持可复制值；资源请在函数内创建", "XE-SEM-0001")
        except Diagnostic as error:
            return [error]
        for signature in list(self.functions.values()) + list(self.methods.values()):
            if signature.node["body"] is None:
                continue
            try:
                self.check_function(signature)
            except Diagnostic as error:
                self.diagnostics.append(error)
        return self.diagnostics

    def check_function(self, signature):
        self.scopes = [{}]
        self.generic_names, self.self_type = signature.generics, signature.self_type
        self.copy_generics = set()
        for constraint in signature.node.get("constraints", []):
            if constraint["trait"]["kind"] == "NamedType" and constraint["trait"]["path"]["parts"] == ["Copy"]:
                self.copy_generics.add(self.type_of(constraint["target"]).name)
            else:
                self.fail(constraint, "当前语义阶段只支持 Copy 泛型约束", "XE-SEM-0001")
        self.result = signature.result
        self.last_uses = names_used(signature.node["body"])
        self.loop_depths, self.temporary_loans = [], []
        self.parameter_roots = set()
        for parameter, type_ in zip(signature.node["parameters"], signature.parameters):
            binding = self.declare(parameter["name"], type_, parameter, parameter["mutable"], parameter=True)
            if self.carries_borrow(type_):
                self.uid += 1
                self.parameter_roots.add(self.uid)
                binding.origins = ((self.uid, type_.mutable),)
        result = self.block(signature.node["body"], signature.result, lift=True)
        self.convert(result, signature.result, lift=True)
        self.escape(result, function_exit=True)

    def declare(self, name, type_, node, mutable=False, initialized=True, origins=(), parameter=False):
        if name in self.scopes[-1]:
            self.fail(node, f"当前作用域中的 {name} 重复", "XE-NAME-0002")
        self.uid += 1
        binding = Binding(self.uid, name, type_, node, mutable, initialized,
                          origins=origins, last_use=self.last_uses.get(name, -1), parameter=parameter)
        self.scopes[-1][name] = binding
        return binding

    def lookup(self, name, node, read=True, partial=False):
        binding = next((scope[name] for scope in reversed(self.scopes) if name in scope), None)
        if binding is None:
            self.fail(node, f"未定义名称 {name}", "XE-NAME-0001")
        if read:
            if not binding.initialized:
                self.fail(node, f"{name} 可能尚未初始化", "XE-INIT-0001")
            if binding.moved or (binding.moved_fields and not partial):
                self.fail(node, f"{name} 已移动或在某条分支中移动", "XE-MOVE-0001")
        return binding

    def by_uid(self, uid):
        return next((b for scope in self.scopes for b in scope.values() if b.uid == uid), None)

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

    def loan(self, origins, node, exclude=None):
        if not self.check_borrows:
            return
        position = self.offset(node)
        active = list(self.temporary_loans)
        for binding in self.bindings().values():
            if (binding.uid != exclude and binding.initialized and not binding.moved
                    and binding.last_use >= position):
                active.extend(binding.origins)
        for uid, mutable in origins:
            for other_uid, other_mutable in active:
                if uid == other_uid and (mutable or other_mutable):
                    self.fail(node, "该对象仍存在冲突的共享/可写借用", "XE-BORROW-0002",
                              "先结束已有借用的最后一次使用，再建立新的借用")

    def consume(self, value: Value):
        if self.copyable(value.type) or value.type == NEVER:
            return
        if value.borrowed:
            self.fail(value.node, "不能通过非拥有指针移走资源所有权", "XE-MOVE-0002",
                      "传递指针，或显式 clone 创建新的拥有值")
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
                    if binding.type.name in self.drop_types:
                        self.fail(value.node, "自定义 Drop 类型不能移出资源字段", "XE-OWN-0002")
                    binding.moved_fields.add(fields)
                else:
                    binding.moved = True

    def escape(self, value, leaving=None, function_exit=False):
        if not self.check_borrows:
            return
        leaving = leaving or set()
        for uid, _ in value.origins:
            if uid in leaving or (function_exit and uid not in self.parameter_roots):
                self.fail(value.node, "返回的借用/视图可能超过其所有者生命周期", "XE-BORROW-0003")

    def default(self, type_):
        return I32 if type_ == INT_LITERAL else type_

    def unresolved(self, type_):
        return type_ == UNKNOWN or any(self.unresolved(t) for t in type_.args)

    def compatible(self, actual, expected):
        if actual == NEVER or actual == expected or actual == UNKNOWN or expected == UNKNOWN:
            return True
        if actual == INT_LITERAL and expected.name in NUMERIC - {"f32", "f64"}:
            return True
        if actual.name == expected.name and len(actual.args) == len(expected.args):
            return actual.mutable == expected.mutable and all(
                self.compatible(a, b) for a, b in zip(actual.args, expected.args))
        return False

    def convert(self, value, target, lift=False):
        if value.type == NEVER:
            return value
        if lift and target.name == "maybe" and value.type.name != "maybe":
            if value.type == NONE:
                if target.args[1] != NONE:
                    self.fail(value.node, "None 不能代替具体错误；使用 Maybe::No[error]", "XE-RESULT-0001")
            else:
                self.convert(value, target.args[0])
            return Value(target, value.node, value.place, value.origins, value.borrowed, value.access_uid)
        if not self.compatible(value.type, target):
            self.fail(value.node, f"类型不匹配：需要 {target}，实际为 {value.type}", "XE-TYPE-0001")
        if value.type == INT_LITERAL and target.name in NUMERIC:
            literal = value.literal
            if isinstance(literal, int) and target.name not in {"isize", "usize"}:
                bits = int(target.name[1:])
                low = -(1 << (bits-1)) if target.name[0] == "i" else 0
                high = (1 << (bits-1))-1 if target.name[0] == "i" else (1 << bits)-1
                if not low <= literal <= high:
                    self.fail(value.node, f"整数超出 {target} 范围", "XE-TYPE-0004")
        return Value(target, value.node, value.place, value.origins, value.borrowed,
                     value.access_uid, value.literal)

    def common(self, values, node):
        usable = [v for v in values if v.type != NEVER]
        if not usable:
            return Value(NEVER, node)
        type_ = usable[0].type
        for value in usable[1:]:
            if type_ == INT_LITERAL:
                type_ = value.type
            elif not self.compatible(value.type, type_):
                self.fail(node, f"分支/元素类型不一致：{type_} 与 {value.type}", "XE-TYPE-0001")
        origins = tuple(set().union(*(set(v.origins) for v in usable)))
        return Value(type_, node, origins=origins)

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
                if statement["operator"] == "<<":
                    self.consume(value)
                self.declare(statement["name"], value.type, statement, statement["mutable"], origins=value.origins)
            elif kind == "Assignment":
                self.assignment(statement)
            elif kind == "Return":
                value = self.infer(statement["value"], self.result, lift=True) if statement["value"] else Value(UNIT, statement)
                value = self.convert(value, self.result, lift=True)
                self.consume(value)
                self.escape(value, function_exit=True)
                terminated = True
            elif kind in {"Break", "Continue"}:
                if not self.loop_depths:
                    self.fail(statement, f"{kind.lower()} 只能用于循环", "XE-FLOW-0001")
                terminated = True
            else:
                value = self.infer(statement["expression"])
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
            result = Value(result.type, result.node, origins=result.origins)
        else:
            result = Value(UNIT, node)
        local_ids = {b.uid for b in self.scopes[-1].values()}
        self.scope_escape(result, node, local_ids)
        self.scopes.pop()
        return result

    def scope_escape(self, result, node, local_ids):
        self.escape(result, local_ids)
        # 块结果不是唯一的逃逸渠道：外层变量可能被赋为内层地址。
        for scope in self.scopes[:-1]:
            for binding in scope.values():
                if binding.initialized and not binding.moved and binding.last_use >= node["span"]["end"]["offset"]:
                    self.escape(Value(binding.type, binding.node, origins=binding.origins), local_ids)

    def place(self, node, read=True):
        if node["kind"] == "Group":
            return self.place(node["expression"], read)
        if node["kind"] == "Name" and len(node["path"]["parts"]) == 1:
            binding = self.lookup(node["path"]["parts"][0], node, read, partial=True)
            return Value(binding.type, node, (binding.uid, ()), binding.origins,
                         access_uid=binding.uid)
        return self.infer(node)

    def mutable_place(self, value):
        if value.borrowed:
            return not self.check_borrows or any(mutable for _, mutable in value.origins)
        if value.place:
            binding = self.by_uid(value.place[0])
            return bool(binding and binding.mutable)
        return False

    def assignment(self, node):
        operator = node["operator"]
        target_node = node["right"] if operator == ">>" else node["left"]
        source_node = node["left"] if operator == ">>" else node["right"]
        target = self.place(target_node, read=False)
        binding = self.by_uid(target.place[0]) if target.place else None
        initializing = bool(binding and not binding.initialized and not target.place[1])
        if not initializing and not self.mutable_place(target):
            self.fail(target_node, "不能修改不可变绑定或只读指针", "XE-MUT-0001")
        value = self.convert(self.infer(source_node, target.type), target.type)
        if operator == "=" and not self.copyable(value.type):
            self.fail(source_node, "资源不能用 = 复制，使用 <<", "XE-OWN-0001")
        if value.place == target.place and value.place and operator != "=":
            self.fail(node, "不能把资源传递给自身", "XE-MOVE-0001")
        if operator != "=":
            self.consume(value)
        if target.place and not target.borrowed:
            self.loan(((target.place[0], True),), node, exclude=target.access_uid)
        if binding and not target.borrowed:
            binding.initialized, binding.moved = True, False
            binding.origins = value.origins
            if target.place[1]:
                binding.moved_fields.discard(target.place[1])
            else:
                binding.moved_fields.clear()

    def infer(self, node, expected=None, lift=False) -> Value:
        kind = node["kind"]
        self.position = self.offset(node)
        if kind == "Literal":
            category = node["literal_kind"]
            type_ = {"INTEGER": INT_LITERAL, "FLOAT": Type("f64"), "STRING": STR,
                     "CHAR": Type("char"), "BYTE": Type("u8"), "true": BOOL,
                     "false": BOOL, "unit": UNIT}[category]
            value = Value(type_, node, literal=node["value"])
            numeric_expected = expected.args[0] if expected and expected.name == "maybe" and lift else expected
            if type_ == INT_LITERAL and numeric_expected and numeric_expected.name in NUMERIC:
                return self.convert(value, numeric_expected)
            return value
        if kind == "NoneValue":
            return Value(NONE, node)
        if kind == "Name":
            name = "::".join(node["path"]["parts"])
            if len(node["path"]["parts"]) == 1 and any(name in s for s in self.scopes):
                binding = self.lookup(name, node)
                if binding.type.name != "ptr":
                    self.loan(((binding.uid, False),), node, exclude=binding.uid)
                return Value(binding.type, node, (binding.uid, ()), binding.origins,
                             access_uid=binding.uid)
            if name in self.functions:
                signature = self.functions[name]
                if signature.generics:
                    self.fail(node, "泛型函数作为一等函数尚未实现；当前支持直接调用推导", "XE-SEM-0001")
                return Value(callable_type(signature.parameters, signature.result), node)
            if name in self.constants:
                return self.constants[name]
            variant = self.variant(name, expected, node, optional=True)
            if variant is not None:
                owner, payload = variant
                if payload:
                    self.fail(node, "有负载的枚举变体必须用 [] 构造", "XE-TYPE-0005")
                return Value(owner, node)
            self.fail(node, f"未定义名称 {name}", "XE-NAME-0001")
        if kind == "Group":
            return self.infer(node["expression"], expected, lift)
        if kind == "Block":
            return self.block(node, expected, lift)
        if kind == "Borrow":
            value = self.place(node["operand"])
            if self.check_borrows and value.type.name == "ptr":
                self.fail(node, "检查模式下的多级指针区域投影尚未实现", "XE-SEM-0001")
            if value.place is None:
                self.fail(node, "只能对有存储位置的变量、字段或解引用取地址", "XE-BORROW-0001")
            if value.type == STR:
                self.fail(node, "str 已经是视图，禁止取 str@", "XE-TYPE-0003")
            mods = [m["name"] for m in node["modifiers"]]
            if mods and not self.check_borrows:
                self.fail(node, "借用修饰需要 --check-borrows", "XE-BORROW-0001")
            if any(m != "mut" and m not in self.generic_names for m in mods) or len(mods) != len(set(mods)):
                self.fail(node, "未知或重复的借用修饰")
            mutable = "mut" in mods
            if mutable and not self.mutable_place(value):
                self.fail(node, "可写借用需要 var 存储或已有可写指针", "XE-MUT-0001")
            origins = value.origins if value.borrowed else ((value.place[0], mutable),)
            origins = tuple((uid, mutable) for uid, _ in origins)
            self.loan(origins, node, exclude=value.access_uid if value.borrowed else None)
            return Value(ptr(value.type, mutable), node, origins=origins)
        if kind == "Dereference":
            pointer = self.infer(node["operand"])
            if pointer.type.name != "ptr":
                self.fail(node, "# 只能解引用指针")
            self.loan(pointer.origins, node, exclude=pointer.access_uid)
            root = pointer.origins[0][0] if pointer.origins else None
            return Value(pointer.type.args[0], node,
                         (root, ()) if root is not None else None, pointer.origins,
                         True, pointer.access_uid)
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
                field_type = self.type_of(fields[node["field"]]["type"], self.generic_set(definition))
                field_type = substitute(field_type, {"$"+p["name"]: t for p, t in
                                        zip(definition.get("generics", []), base.args)})
            place = (value.place[0], value.place[1] + (node["field"],)) if value.place else None
            if place:
                self.loan(value.origins if borrowed else ((place[0], False),), node, exclude=value.access_uid)
                binding = self.by_uid(place[0])
                if binding and any(place[1][:len(m)] == m for m in binding.moved_fields):
                    self.fail(node, "该字段已经移动", "XE-MOVE-0001")
            origins = value.origins if borrowed or self.carries_borrow(field_type) else ()
            return Value(field_type, node, place, origins, borrowed, value.access_uid)
        if kind == "BracketApply":
            return self.bracket(node, expected)
        if kind == "StructLiteral":
            name_node = node["constructor"]
            if name_node["kind"] != "Name":
                self.fail(node, "显式泛型结构体构造尚未支持", "XE-SEM-0001")
            name = "::".join(name_node["path"]["parts"])
            definition = self.types.get(name)
            if not definition or definition["kind"] != "Struct":
                self.fail(node, f"{name} 不是已声明结构体", "XE-NAME-0001")
            fields = {f["name"]: f for f in definition["fields"]}
            names = [f["name"] for f in node["fields"]]
            if len(set(names)) != len(names) or set(names) != set(fields):
                self.fail(node, "结构体必须恰好初始化所有字段", "XE-INIT-0002")
            origins = []
            for field in node["fields"]:
                type_ = self.type_of(fields[field["name"]]["type"], self.generic_set(definition))
                value = self.convert(self.infer(field["value"], type_), type_)
                if field["operator"] == "=" and not self.copyable(value.type):
                    self.fail(field, "资源字段必须使用 <<", "XE-OWN-0001")
                self.consume(value)
                origins.extend(value.origins)
            return Value(Type(name), node, origins=tuple(origins))
        if kind in {"Array", "Tuple"}:
            elem_expected = expected.args[0] if expected and expected.name == "Array" else None
            values = [self.infer(n, elem_expected) for n in node["elements"]]
            for value in values:
                self.consume(value)
            if kind == "Tuple":
                type_ = Type("tuple", tuple(self.default(v.type) for v in values))
            else:
                if not values and (expected is None or expected.name != "Array"):
                    self.fail(node, "空数组需要显式 Array[T, 0] 类型注解")
                common = self.common(values, node) if values else Value(expected.args[0], node)
                type_ = Type("Array", (self.default(common.type), Type(str(len(values)))))
            return Value(type_, node, origins=tuple(set().union(*(set(v.origins) for v in values))))
        if kind == "Call":
            return self.call(node, expected)
        if kind == "Unary":
            value = self.infer(node["operand"], BOOL if node["operator"] == "not" else None)
            if node["operator"] == "not":
                self.convert(value, BOOL)
                return Value(BOOL, node)
            if value.type.name not in NUMERIC | {"$integer"}:
                self.fail(node, "一元正负号需要数值")
            result = Value(value.type, node, literal=(-value.literal if node["operator"] == "-" and
                                                     value.literal is not None else value.literal))
            target = expected.args[0] if expected and expected.name == "maybe" and lift else expected
            return self.convert(result, target) if target and target.name in NUMERIC else result
        if kind == "ComparisonChain":
            values = [self.infer(operand) for operand in node["operands"]]
            common = self.common(values, node)
            if common.type.name not in NUMERIC | {"$integer", "bool", "char", "str"}:
                self.fail(node, "该类型的比较 Trait 检查尚未实现", "XE-SEM-0001")
            return Value(BOOL, node)
        if kind == "Binary":
            left = self.infer(node["left"], expected)
            if node["operator"] in {"and", "or"}:
                self.convert(left, BOOL)
                before = deepcopy(self.scopes)
                right = self.infer(node["right"], BOOL)
                right_states = deepcopy(self.bindings())
                self.scopes = before
                self.merge({}, [self.bindings(), right_states])
                self.convert(right, BOOL)
                return Value(BOOL, node)
            right = self.infer(node["right"], left.type if left.type != INT_LITERAL else expected)
            common = self.common([left, right], node)
            if common.type.name not in NUMERIC | {"$integer"}:
                self.fail(node, "算术运算需要数值")
            return Value(common.type, node)
        if kind == "Range":
            endpoint = expected.args[0] if expected and expected.name == "Range" else None
            values = [self.infer(node[key], endpoint) for key in ("lower", "upper") if node[key]]
            type_ = self.default(self.common(values, node).type) if values else USIZE
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
            self.consume(value)
            return Value(value.type.args[0], node, origins=value.origins)
        if kind == "AnonymousFunction":
            return self.anonymous(node)
        if kind == "Unsafe":
            return self.block(node["body"], expected, lift)
        self.fail(node, f"尚未支持的语义结构 {kind}", "XE-SEM-0001")

    def variant(self, name, expected, node, optional=False):
        parts = name.split("::")
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
                    result = expected if expected and expected.name == parts[0] else Type(parts[0])
                    bindings = {"$"+p["name"]: t for p, t in zip(declaration.get("generics", []), result.args)}
                    return result, [substitute(self.type_of(t, self.generic_set(declaration)), bindings)
                                    for t in variant["payload"]]
        if not optional:
            self.fail(node, f"未知枚举变体 {name}", "XE-NAME-0001")
        return None

    def bracket(self, node, expected):
        obj = node["object"]
        variant = self.variant("::".join(obj["path"]["parts"]), expected, obj, True) if obj["kind"] == "Name" else None
        if variant:
            result, parameters = variant
            values = self.arguments(node["arguments"], parameters, node)
            if result.name == "maybe":
                args = list(result.args)
                index = 0 if obj["path"]["parts"][-1] == "Yes" else 1
                if values:
                    args[index] = self.default(values[0].type)
                result = Type("maybe", tuple(args))
            return Value(result, node, origins=tuple(o for v in values for o in v.origins))
        value = self.infer(obj)
        base = value.type.args[0] if value.type.name == "ptr" else value.type
        if base.name not in {"Array", "Slice", "SliceMut", "Vec"} or len(node["arguments"]) != 1:
            self.fail(node, "此处 [] 只能构造枚举载荷或索引数组/切片", "XE-SEM-0001")
        index = self.infer(node["arguments"][0], USIZE)
        self.convert(index, USIZE)
        if base.name == "Array" and isinstance(index.literal, int) and len(base.args) > 1:
            if not 0 <= index.literal < int(base.args[1].name):
                self.fail(node, "数组索引越界", "XE-TYPE-0004")
        return Value(base.args[0], node, value.place, value.origins, borrowed=True, access_uid=value.access_uid)

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

    def call(self, node, expected=None):
        callee = node["callee"]
        if callee["kind"] == "FieldAccess":
            return self.method(callee, node["arguments"], node)
        if callee["kind"] == "Name":
            name = "::".join(callee["path"]["parts"])
            if self.variant(name, expected, callee, True):
                self.fail(node, "枚举载荷使用 []，不是函数调用 ()", "XE-TYPE-0005", "例如 Token::Integer[42]")
            local = any(name in scope for scope in self.scopes)
            if name in self.functions and not local:
                return self.apply_signature(self.functions[name], node["arguments"], node)
            builtin = self.builtin_call(name, node["arguments"], node) if not local else None
            if builtin is not None:
                return builtin
            if "::" in name:
                owner, method = name.rsplit("::", 1)
                signature = self.methods.get((owner, method))
                if signature:
                    values = self.arguments(node["arguments"], signature.parameters, node)
                    return Value(signature.result, node, origins=tuple(o for v in values for o in v.origins)
                                 if self.carries_borrow(signature.result) else ())
        function = self.infer(callee)
        return self.invoke(function, node["arguments"], node)

    def apply_signature(self, signature, nodes, node):
        """直接泛型调用从实参统一类型变量；不尝试隐式转换或 Trait 搜索。"""
        if len(nodes) != len(signature.parameters):
            self.fail(node, "函数参数数量不匹配", "XE-CALL-0001")
        substitutions, values = {}, []
        def has_variable(type_):
            return type_.name.startswith("$") or any(has_variable(t) for t in type_.args)
        def unify(pattern, actual, at):
            if pattern.name.startswith("$"):
                actual = self.default(actual)
                if pattern.name in substitutions and substitutions[pattern.name] != actual:
                    self.fail(at, "同一泛型参数被推导成不同类型")
                substitutions[pattern.name] = actual
            elif pattern.name == actual.name and len(pattern.args) == len(actual.args):
                for p, a in zip(pattern.args, actual.args):
                    unify(p, a, at)
        saved = list(self.temporary_loans)
        try:
            for argument, pattern in zip(nodes, signature.parameters):
                parameter = substitute(pattern, substitutions)
                value = self.infer(argument, None if has_variable(parameter) else parameter)
                unify(pattern, value.type, argument)
                value = self.convert(value, substitute(pattern, substitutions))
                self.consume(value)
                self.temporary_loans.extend(value.origins)
                values.append(value)
        finally:
            self.temporary_loans = saved
        for constraint in signature.node.get("constraints", []):
            trait = constraint["trait"]
            if trait["kind"] != "NamedType" or trait["path"]["parts"] != ["Copy"]:
                self.fail(constraint, "目前泛型约束只支持 Copy", "XE-SEM-0001")
            target = substitute(self.type_of(constraint["target"], signature.generics, signature.self_type), substitutions)
            if not self.copyable(target):
                self.fail(node, f"{target} 不满足 Copy 约束", "XE-OWN-0001")
        result = substitute(signature.result, substitutions)
        if has_variable(result):
            self.fail(node, "无法从实参推导返回泛型，请明确实参类型", "XE-TYPE-0001")
        return Value(result, node, origins=tuple(o for v in values for o in v.origins)
                     if self.carries_borrow(result) else ())

    def invoke(self, function, nodes, node):
        if function.type.name not in {"fn", "closure"}:
            self.fail(node, f"{function.type} 不是可调用函数", "XE-CALL-0001")
        values = self.arguments(nodes, list(function.type.args[:-1]), node)
        self.consume(function)
        result = function.type.args[-1]
        return Value(result, node, origins=function.origins + tuple(o for v in values for o in v.origins)
                     if self.carries_borrow(result) else ())

    def builtin_call(self, name, nodes, node):
        if name in {"println", "print", "eprintln", "format"}:
            if not nodes:
                self.fail(node, "格式化调用需要格式字符串", "XE-CALL-0001")
            template = self.convert(self.infer(nodes[0]), STR)
            values = [self.infer(n) for n in nodes[1:]]
            if template.literal is None:
                self.fail(nodes[0], "当前阶段要求格式字符串为字面量", "XE-SEM-0001")
            try:
                fields = [field for _, field, _, _ in string.Formatter().parse(template.literal) if field is not None]
            except ValueError:
                self.fail(nodes[0], "格式字符串花括号不匹配", "XE-FORMAT-0001")
            if len(fields) != len(values):
                self.fail(node, "格式占位符数量与参数不一致", "XE-FORMAT-0001")
            for value in values:
                self.consume(value)
            return Value(STRING if name == "format" else UNIT, node)
        signatures = {"String::from": ([STR], STRING), "File::open": ([STR], maybe(FILE, IO_ERROR)),
                      "panic": ([STR], NEVER)}
        if name not in signatures:
            return None
        parameters, result = signatures[name]
        self.arguments(nodes, parameters, node)
        return Value(result, node)

    def method(self, callee, nodes, node):
        receiver = self.place(callee["object"])
        base = receiver.type.args[0] if receiver.type.name == "ptr" else receiver.type
        name = callee["field"]
        signature = self.methods.get((base.name, name))
        mutable, owning, receiver_loans = False, False, ()
        if signature:
            if not signature.parameters:
                self.fail(node, "关联函数不能使用对象调用", "XE-CALL-0001")
            self_parameter = signature.parameters[0]
            owning = self_parameter.name != "ptr"
            mutable = self_parameter.mutable
            parameters, result = signature.parameters[1:], signature.result
        elif base.name == "maybe" and name == "expect":
            parameters, result, owning = [STR], base.args[0], True
        else:
            element = base.args[0] if base.args else I32
            table = {
                ("String", "len"): ([], USIZE, False), ("str", "len"): ([], USIZE, False),
                ("String", "as_str"): ([], STR, False), ("String", "clone"): ([], STRING, False),
                ("String", "push_str"): ([STR], UNIT, True),
                ("str", "byte_at"): ([USIZE], Type("u8"), False),
                ("str", "slice_bytes"): ([Type("Range", (USIZE,))], maybe(STR), False),
                ("str", "data"): ([], Type("RawPtr", (Type("u8"),)), False),
                ("File", "size"): ([], USIZE, False),
                ("File", "read_to_string"): ([], maybe(STRING, IO_ERROR), False),
                ("Array", "slice"): ([Type("Range", (USIZE,))], Type("Slice", (element,)), False),
                ("Array", "slice_mut"): ([Type("Range", (USIZE,))], Type("SliceMut", (element,)), True),
                ("SliceMut", "copy_from"): ([Type("Slice", (element,))], UNIT, True),
            }
            entry = table.get((base.name, name))
            if entry is None:
                self.fail(node, f"类型 {base} 没有已支持的方法 {name}", "XE-NAME-0001")
            parameters, result, mutable = entry
        if owning:
            if receiver.type.name == "ptr":
                receiver = Value(base, receiver.node, receiver.place, receiver.origins, True, receiver.access_uid)
            self.consume(receiver)
        elif self.check_borrows:
            writable = receiver.type.mutable if receiver.type.name == "ptr" else self.mutable_place(receiver)
            if mutable and not writable:
                self.fail(node, "可写方法需要 var 或 T@[mut]", "XE-BORROW-0004")
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
        origins = ()
        if self.carries_borrow(result):
            if self.check_borrows and not self.copyable(base) and not owning and not receiver.place and not receiver.origins:
                self.fail(node, "不能返回临时资源的借用视图；先将资源绑定到变量", "XE-BORROW-0003")
            origins = receiver.origins or (((receiver.place[0], mutable),) if receiver.place else ())
            origins += tuple(o for v in values for o in v.origins)
        return Value(result, node, origins=origins)

    def handle(self, handler, payloads, expected=None, lift=False):
        if handler["kind"] == "FunctionTarget":
            function = self.infer(handler["target"])
            if function.type.name not in {"fn", "closure"}:
                self.fail(handler, "管道目标必须是函数", "XE-CALL-0001")
            parameters = list(function.type.args[:-1])
            if len(parameters) != len(payloads):
                self.fail(handler, "管道函数参数数量与分支载荷不一致", "XE-CALL-0001")
            for value, type_ in zip(payloads, parameters):
                self.consume(self.convert(value, type_))
            self.consume(function)
            return Value(function.type.args[-1], handler, origins=function.origins + tuple(o for v in payloads for o in v.origins)
                         if self.carries_borrow(function.type.args[-1]) else ())
        parameters = handler["parameters"]
        ignore = len(parameters) == 1 and parameters[0]["name"] == "_" and parameters[0]["type"] is None
        if not ignore and len(parameters) != len(payloads):
            self.fail(handler, "分支绑定数量与载荷不一致（None/无载荷分支使用 _）", "XE-CALL-0001")
        self.scopes.append({})
        if not ignore:
            for parameter, value in zip(parameters, payloads):
                type_ = self.type_of(parameter["type"]) if parameter["type"] else value.type
                self.convert(value, type_)
                self.declare(parameter["name"], type_, parameter, parameter["mutable"], origins=value.origins)
        result = self.infer(handler["body"], expected, lift)
        if expected:
            result = self.convert(result, expected, lift)
        self.consume(result)
        self.scope_escape(result, handler, set(b.uid for b in self.scopes[-1].values()))
        self.scopes.pop()
        return Value(result.type, handler, origins=result.origins)

    def branch(self, node, expected=None, lift=False):
        value = self.infer(node["input"])
        borrowed = "borrow" in node["modifiers"]
        mutable = "mut" in node["modifiers"]
        base = value.type.args[0] if value.type.name == "ptr" else value.type
        if value.type.name == "ptr" and not borrowed:
            self.fail(node, "指针匹配必须显式使用 ?[@]，不能取得其指向资源", "XE-MOVE-0002")
        if mutable and self.check_borrows and not (value.type.mutable if value.type.name == "ptr" else self.mutable_place(value)):
            self.fail(node, "可写匹配需要可写对象", "XE-BORROW-0004")
        origins = value.origins or (((value.place[0], mutable),) if value.place else ())
        if borrowed:
            if self.check_borrows and not value.place and not value.origins:
                self.fail(node, "借用匹配需要稳定的存储位置；先绑定被匹配值", "XE-BORROW-0001")
            self.loan(origins, node, value.access_uid)
        else:
            self.consume(value)
        if self.unresolved(base):
            self.fail(node, "匹配前需要确定完整枚举类型，请给输入值添加类型注解")
        declaration = self.types.get(base.name, {})
        variants = {v["name"]: [self.type_of(t, self.generic_set(declaration)) for t in v["payload"]]
                    for v in declaration.get("variants", [])}
        if base.name == "maybe":
            variants = {"Yes": [base.args[0]], "None" if base.args[1] == NONE else "No": [] if base.args[1] == NONE else [base.args[1]]}
        before = deepcopy(self.scopes)
        results, states, covered = [], [], set()
        wildcard = False
        for arm in node["arms"]:
            self.scopes = deepcopy(before)
            if wildcard:
                self.fail(arm, "该分支不可达：前面的 _ 已覆盖全部情况", "XE-MATCH-0002")
            if arm["kind"] == "ChannelArm":
                if base.name != "maybe":
                    self.fail(arm, "1>/2> 仅用于 T? 或 T?[E]", "XE-MATCH-0001")
                key = "Yes" if arm["channel"] == 1 else ("None" if base.args[1] == NONE else "No")
                types = variants[key]
            else:
                selector = arm["selector"]
                if selector["kind"] == "WildcardSelector":
                    wildcard, key, types = True, "_", [base]
                elif selector["kind"] == "VariantSelector":
                    path = selector["path"]["parts"]
                    key = path[-1]
                    if len(path) != 2 or path[0] != ("Maybe" if base.name == "maybe" else base.name) or key not in variants:
                        self.fail(selector, "变体不属于被匹配类型", "XE-MATCH-0001")
                    if selector.get("filters") is not None:
                        self.fail(selector, "载荷过滤模式尚未实现", "XE-SEM-0001")
                    types = variants[key]
                elif selector["kind"] == "LiteralSelector":
                    literal = self.convert(self.infer(selector["value"]), base)
                    key, types = repr(literal.literal), [base]
                else:
                    self.fail(selector, "组合模式尚未实现", "XE-SEM-0001")
            if key in covered:
                self.fail(arm, "重复的分支不可达", "XE-MATCH-0002")
            covered.add(key)
            if not borrowed and base.name in self.drop_types and any(not self.copyable(t) for t in types):
                self.fail(arm, "自定义 Drop 枚举不能移出资源载荷", "XE-OWN-0002")
            payloads = [Value(ptr(t, mutable) if borrowed and t.name not in {"str", "Slice"} else t,
                              arm, origins=origins if borrowed or self.carries_borrow(t) else ()) for t in types]
            result = self.handle(arm["handler"], payloads, expected, lift)
            results.append(result)
            if result.type != NEVER:
                states.append(deepcopy(self.bindings()))
        self.scopes = before
        self.merge({}, states)
        if not wildcard and (set(variants) - covered if variants else not (base == BOOL and {"True", "False"} <= covered)):
            self.fail(node, "模式匹配没有覆盖所有情况", "XE-MATCH-0003", "补齐所有变体，或添加 _ 分支")
        return self.common(results, node)

    def loop(self, node):
        before = deepcopy(self.scopes)
        self.loop_depths.append(len(self.scopes)-1)
        self.scopes.append({})
        if node["kind"] == "While":
            self.convert(self.infer(node["condition"]), BOOL)
        else:
            source = self.infer(node["source"])
            if source.type.name in {"Range", "Iterator"}:
                element = source.type.args[0]
            elif source.type.name in {"Array", "Slice", "SliceMut"}:
                element = ptr(source.type.args[0], source.type.name == "SliceMut")
            else:
                self.fail(node, "当前 for 支持范围、数组和切片", "XE-SEM-0001")
            annotation = self.type_of(node["type"]) if node["type"] else self.default(element)
            self.convert(Value(element, node), annotation)
            origins = source.origins
            if element.name == "ptr" and not origins and source.place:
                origins = ((source.place[0], element.mutable),)
            self.declare(node["name"], annotation, node, origins=origins)
        self.block(node["body"])
        self.scopes.pop()
        after = deepcopy(self.bindings())
        self.scopes = before
        self.merge({}, [deepcopy(self.bindings()), after])
        self.loop_depths.pop()
        return Value(UNIT, node)

    def anonymous(self, node):
        captures = []
        for capture in node["captures"]:
            binding = self.lookup(capture["name"], capture)
            value = Value(binding.type, capture, (binding.uid, ()), binding.origins)
            if capture["borrow"]:
                mods = [m["name"] for m in capture["modifiers"]]
                if mods and not self.check_borrows:
                    self.fail(capture, "借用捕获修饰需要 --check-borrows", "XE-BORROW-0001")
                if len(mods) != len(set(mods)) or any(m != "mut" for m in mods):
                    self.fail(capture, "未知或重复的捕获借用修饰")
                if binding.type == STR:
                    self.fail(capture, "str 视图按值捕获，禁止 str@", "XE-TYPE-0003")
                if self.check_borrows and binding.type.name == "ptr":
                    self.fail(capture, "检查模式下的多级指针捕获尚未实现", "XE-SEM-0001")
                mutable = "mut" in mods
                if mutable and not binding.mutable:
                    self.fail(capture, "可写捕获需要 var 存储", "XE-MUT-0001")
                origins = ((binding.uid, mutable),)
                self.loan(origins, capture)
                value = Value(ptr(binding.type, origins[0][1]), capture, origins=origins)
            else:
                self.consume(value)
            captures.append((capture["name"], value))
        saved = self.scopes, self.result, self.last_uses, self.loop_depths, self.parameter_roots
        self.scopes, self.result = [{}], self.type_of(node["result"])
        self.last_uses, self.loop_depths = names_used(node["body"]), []
        self.parameter_roots = {uid for _, value in captures for uid, _ in value.origins}
        for name, value in captures:
            self.declare(name, value.type, node, origins=value.origins)
        parameters = []
        for parameter in node["parameters"]:
            if parameter["type"] is None:
                self.fail(parameter, "独立闭包参数需要类型注解", "XE-TYPE-0001")
            type_ = self.type_of(parameter["type"])
            parameters.append(type_)
            binding = self.declare(parameter["name"], type_, parameter, parameter["mutable"], parameter=True)
            if self.carries_borrow(type_):
                self.uid += 1
                self.parameter_roots.add(self.uid)
                binding.origins = ((self.uid, type_.mutable),)
        result = self.block(node["body"], self.result, True)
        self.convert(result, self.result, True)
        self.escape(result, function_exit=True)
        result_type = self.result
        self.scopes, self.result, self.last_uses, self.loop_depths, self.parameter_roots = saved
        return Value(callable_type(parameters, result_type, bool(captures)), node,
                     origins=tuple(o for _, value in captures for o in value.origins))


def check_source(text: str, filename: str = "<input>", check_borrows: bool = False) -> list[Diagnostic]:
    """解析失败也使用同一种定位诊断；语法成功才进入语义阶段。"""
    from .parser import parse_source
    source = Source(text, filename)
    try:
        tree = parse_source(text, filename)
        return Checker(source, tree, check_borrows).check()
    except Diagnostic as error:
        return [error]
