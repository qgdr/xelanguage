"""静态 Trait 与条件 Copy/Drop：声明是契约，调用仍生成普通函数。

这里不实现动态对象、虚表或运行时查找。注册 impl 后按具体目标类型匹配，
验证方法签名和 where 条件；条件 Copy 另外验证真实字段，不能仅凭类型名
把 Holder[i32] 的复制资格误给 Holder[String]。析构函数也使用同一实例缓存。
"""
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .typesys import Type, substitute

if TYPE_CHECKING:
    from .semantic import Checker


@dataclass
class TraitImplementation:
    node: dict[str, Any]
    trait: Type
    target: Type
    generics: set[str]


def match_type(pattern: Type, actual: Type, substitutions: dict[str, Type]) -> bool:
    """精确匹配实现目标；不能利用普通指针的写权限降级挑选另一个 impl。"""
    if pattern.name.startswith("$"):
        previous = substitutions.get(pattern.name)
        if previous is not None:
            return previous == actual
        substitutions[pattern.name] = actual
        return True
    if (pattern.name != actual.name or pattern.mutable != actual.mutable
            or len(pattern.args) != len(actual.args)):
        return False
    return all(match_type(p, a, substitutions) for p, a in zip(pattern.args, actual.args))


def patterns_overlap(left: Type, right: Type) -> bool:
    """不允许重叠实现/特化：无需让用户猜测哪个方法优先。

左右模板的 T 是不同未知量；先改名再做有限的结构统一。where 条件不用于
证明互斥，因为开放 Trait 以后还可以有新实现。这比依赖声明顺序可靠。
"""
    def rename(type_, prefix):
        name = prefix + type_.name if type_.name.startswith("$") else type_.name
        return Type(name, tuple(rename(t, prefix) for t in type_.args), type_.mutable)
    substitutions = {}
    def resolve(type_):
        while type_.name in substitutions:
            type_ = substitutions[type_.name]
        return type_
    def contains(type_, variable):
        type_ = resolve(type_)
        return type_.name == variable or any(contains(t, variable) for t in type_.args)
    def unify(a, b):
        a, b = resolve(a), resolve(b)
        if a == b:
            return True
        if a.name.startswith("$"):
            if contains(b, a.name):
                return False
            substitutions[a.name] = b
            return True
        if b.name.startswith("$"):
            return unify(b, a)
        return (a.name == b.name and a.mutable == b.mutable and len(a.args) == len(b.args)
                and all(unify(x, y) for x, y in zip(a.args, b.args)))
    return unify(rename(left, "$L"), rename(right, "$R"))


class TraitChecker:
    """Checker 的协作型 mixin；cast 只给编辑器声明宿主字段，不改变对象。"""

    def trait_type(self, node, generics=None, self_type=None):
        self = cast("Checker", self)
        if node["kind"] != "NamedType":
            self.fail(node, "Trait 约束需要 Trait 名称", "XE-TRAIT-0001")
        name = "::".join(node["path"]["parts"])
        declaration = self.types.get(name)
        if name not in {"Copy", "Drop"} and (declaration is None or declaration["kind"] != "Trait"):
            self.fail(node, f"{name} 不是已声明的 Trait", "XE-TRAIT-0001")
        arguments = tuple(self.type_of(t, generics, self_type) for t in node["arguments"])
        arity = len(declaration.get("generics", [])) if declaration else 0
        if len(arguments) != arity:
            self.fail(node, f"Trait {name} 需要 {arity} 个类型附件，实际为 {len(arguments)} 个",
                      "XE-TRAIT-0001")
        return Type(name, arguments)

    def collect_traits(self):
        self = cast("Checker", self)
        self.trait_implementations = {}
        self.trait_assumptions = set()
        self.drop_instances = {}
        self._drop_type_visits = set()
        self._traits_ready = False
        for declaration in self.types.values():
            if declaration["kind"] != "Trait":
                continue
            methods = declaration["methods"]
            if len({method["name"] for method in methods}) != len(methods):
                self.fail(declaration, "Trait 方法名称重复", "XE-NAME-0002")
            for method in methods:
                signature = self.signature(method, Type("$Self"), self.generic_set(declaration))
                for constraint in method.get("constraints", []):
                    self.constraint_types(constraint, signature.generics, signature.self_type, {})
        for node in self.tree["items"]:
            if node["kind"] != "Impl" or node["trait"] is None:
                continue
            generics = self.generic_set(node)
            trait_name = "::".join(node["trait"].get("path", {}).get("parts", []))
            target_name = "::".join(node["target"].get("path", {}).get("parts", []))
            if (trait_name in {"Copy", "Drop"} and target_name in self.types
                    and self.types[target_name]["kind"] == "Trait"):
                self.fail(node, f"{trait_name} 只能显式实现于用户结构体或枚举",
                          "XE-OWN-0001" if trait_name == "Copy" else "XE-OWN-0002")
            target = self.type_of(node["target"], generics)
            trait = self.trait_type(node["trait"], generics, target)
            declaration = self.types.get(target.name)
            if trait.name in {"Copy", "Drop"} and (declaration is None or declaration["kind"] not in {"Struct", "Enum"}):
                self.fail(node, f"{trait.name} 只能显式实现于用户结构体或枚举；内建类型的复制/清理规则由语言定义",
                          "XE-OWN-0001" if trait.name == "Copy" else "XE-OWN-0002")
            # impl 的未知量必须出现在目标或 Trait 参数中，才能从使用处推导。
            def variables(type_):
                return ({type_.name[1:]} if type_.name.startswith("$") else set()) | set().union(
                    *(variables(t) for t in type_.args))
            missing = generics - variables(target) - variables(trait)
            if missing:
                self.fail(node, "impl 的泛型无法从目标和 Trait 附件推导：" + ", ".join(sorted(missing)),
                          "XE-GENERIC-0001")
            implementation = TraitImplementation(node, trait, target, generics)
            for previous in self.trait_implementations.get(trait.name, []):
                # 同一 Trait 的不同具体附件仍可定义不同契约；如果两个目标及
                # Trait 附件都重叠才冲突。方法名冲突由普通静态方法表另外检查。
                pair = Type("tuple", (target, trait))
                old_pair = Type("tuple", (previous.target, previous.trait))
                if patterns_overlap(pair, old_pair):
                    self.fail(node, f"{target} 的 {trait} 实现重复或与已有泛型实现重叠",
                              "XE-OWN-0001" if trait.name == "Copy" else "XE-TRAIT-0002")
            self.trait_implementations.setdefault(trait.name, []).append(implementation)
            if trait.name == "Copy":
                self.copy_types.add(target.name)
            if trait.name == "Drop":
                self.drop_types.add(target.name)
        for copy in self.trait_implementations.get("Copy", []):
            for drop in self.trait_implementations.get("Drop", []):
                if patterns_overlap(copy.target, drop.target):
                    self.fail(copy.node, "Copy 与 Drop 互斥：实现目标不能重叠", "XE-OWN-0001")

    def constraint_types(self, constraint, generics, self_type, substitutions):
        self = cast("Checker", self)
        target = substitute(self.type_of(constraint["target"], generics, self_type), substitutions)
        trait = substitute(self.trait_type(constraint["trait"], generics, self_type), substitutions)
        return target, trait

    def find_trait_implementation(self, target, trait, visited: set[tuple[Type, Type]] | None = None):
        self = cast("Checker", self)
        visited = set(visited or ())
        key = (target, trait)
        if key in visited:
            return None
        visited.add(key)
        for implementation in self.trait_implementations.get(trait.name, []):
            substitutions = {}
            if (not match_type(implementation.target, target, substitutions)
                    or not match_type(implementation.trait, trait, substitutions)):
                continue
            constraints = implementation.node.get("constraints", [])
            conditions = [self.constraint_types(c, implementation.generics, implementation.target, substitutions)
                          for c in constraints]
            if all(self.has_trait(t, bound, visited) for t, bound in conditions):
                return implementation, substitutions
        return None

    def has_trait(self, target, trait, visited=None):
        self = cast("Checker", self)
        if (target, trait) in self.trait_assumptions:
            return True
        if trait.name == "Copy":
            return self.copyable(target, visited)
        return self.find_trait_implementation(target, trait, visited) is not None

    def has_drop(self, type_):
        self = cast("Checker", self)
        return self.find_trait_implementation(type_, Type("Drop")) is not None

    def check_trait_constraints(self, signature, substitutions, node):
        self = cast("Checker", self)
        for constraint in signature.node.get("constraints", []) + self.implementation_constraints.get(id(signature), []):
            target, trait = self.constraint_types(constraint, signature.generics, signature.self_type, substitutions)
            if not self.has_trait(target, trait):
                self.fail(node, f"{target} 不满足 {trait} 约束",
                          "XE-OWN-0001" if trait.name == "Copy" else "XE-TRAIT-0001")

    def validate_copy_implementations(self):
        self = cast("Checker", self)
        saved = self.trait_assumptions
        try:
            for implementation in self.trait_implementations.get("Copy", []):
                target, node = implementation.target, implementation.node
                self.trait_assumptions = {
                    self.constraint_types(c, implementation.generics, target, {})
                    for c in node.get("constraints", [])}
                declaration = self.types[target.name]
                substitutions = {"$" + g["name"]: t for g, t in zip(declaration.get("generics", []), target.args)}
                fields = ([f["type"] for f in declaration.get("fields", [])]
                          + [t for v in declaration.get("variants", []) for t in v["payload"]])
                for field in fields:
                    type_ = substitute(self.type_of(field, self.generic_set(declaration)), substitutions)
                    if not self.copyable(type_):
                        self.fail(node, f"实现 Copy 的类型要求所有字段和枚举负载都实现 Copy；{type_} 尚不满足",
                                  "XE-OWN-0001", "泛型字段需要 where T implements Copy；资源字段不能复制")
        finally:
            self.trait_assumptions = saved

    def validate_trait_implementation(self, implementation):
        """一次验证契约形状；每个实例的正文仍由原有单态化重新检查。"""
        self = cast("Checker", self)
        trait, target, node = implementation.trait, implementation.target, implementation.node
        if trait.name in {"Copy", "Drop"}:
            return
        declaration = self.types[trait.name]
        declared = {m["name"]: m for m in declaration["methods"]}
        if len(declared) != len(declaration["methods"]):
            self.fail(declaration, "Trait 方法名称重复", "XE-NAME-0002")
        provided = {m["name"]: m for m in node["methods"]}
        for method in node["methods"]:
            if method["name"] not in declared:
                self.fail(method, f"{trait} 没有声明方法 {method['name']}", "XE-TRAIT-0003")
        trait_substitutions = {"$" + g["name"]: t for g, t in zip(declaration.get("generics", []), trait.args)}
        for name, method in declared.items():
            if name not in provided:
                if method["body"] is None:
                    self.fail(node, f"实现 {trait} 缺少方法 {name}", "XE-TRAIT-0003")
                # 默认正文仅复制进这个 impl 的方法表，不修改 Trait 的原始 AST。
                default = self.specialize_node(method, trait_substitutions, target)
                self.register_impl_method(node, target, default)
                provided[name] = default
            # 契约的 T 与 impl 的同名 T 不在同一作用域。Self 必须先保留
            # 为独立占位量，最后整体替换，不能把实现目标里的 T 再误代入。
            contract_self = Type("$__traitSelf")
            expected = self.signature(method, contract_self, self.generic_set(declaration))
            actual = self.signature(provided[name], target, implementation.generics)
            expected_generics = method.get("generics", [])
            actual_generics = provided[name].get("generics", [])
            if len(expected_generics) != len(actual_generics):
                self.fail(provided[name], f"{trait}::{name} 的方法泛型数量与声明不一致", "XE-TRAIT-0003")
            mapping = dict(trait_substitutions)
            mapping[contract_self.name] = target
            mapping.update({"$" + a["name"]: Type("$" + b["name"])
                            for a, b in zip(expected_generics, actual_generics)})
            parameters = [substitute(t, mapping) for t in expected.parameters]
            result = substitute(expected.result, mapping)
            expected_receiver = bool(method["parameters"] and method["parameters"][0]["name"] == "self")
            actual_receiver = bool(provided[name]["parameters"] and provided[name]["parameters"][0]["name"] == "self")
            if parameters != actual.parameters or result != actual.result or expected_receiver != actual_receiver:
                self.fail(provided[name], f"{trait}::{name} 的签名与 Trait 声明不一致",
                          "XE-TRAIT-0003", "参数类型、指针写权限、返回类型以及 self 接收器必须一致")
            expected_constraints = {
                self.constraint_types(c, expected.generics, contract_self, mapping) for c in method.get("constraints", [])}
            actual_constraints = {
                self.constraint_types(c, actual.generics, target, {}) for c in provided[name].get("constraints", [])}
            if expected_constraints != actual_constraints:
                self.fail(provided[name], f"{trait}::{name} 的方法约束与 Trait 声明不一致", "XE-TRAIT-0003")

    def register_impl_method(self, implementation, target, method):
        self = cast("Checker", self)
        key = (target.name, method["name"])
        if key in self.methods:
            self.fail(method, "方法名称重复；静态调用不能从同名 Trait 方法中猜测实现", "XE-NAME-0002")
        signature = self.signature(method, target, self.generic_set(implementation))
        self.methods[key] = signature
        self.implementation_constraints[id(signature)] = implementation.get("constraints", [])

    def ensure_drop_type(self, type_, node, visited=None):
        """生成具体 Drop 并加入前端检查队列，后端不能悄悄实例化未经检查的代码。"""
        self = cast("Checker", self)
        if not self._traits_ready or type_.name == "ptr":
            return
        # 清理布局也可能递归扩大，例如 Vec[Node[Node[T]]]。先用迭代
        # 深度检查限制真实类型，再递归查字段，避免 Python 的栈溢出代替诊断。
        pending = [(type_, 1)]
        while pending:
            current, depth = pending.pop()
            if depth > 64:
                self.fail(node, "资源清理的具体类型超过 64 层；请检查递归泛型是否不断扩大类型参数",
                          "XE-GENERIC-0002")
            pending.extend((argument, depth + 1) for argument in current.args)
        if self.has_variable(type_) or type_ in self._drop_type_visits:
            return
        visited = set(visited or ())
        if type_ in visited:
            return
        visited.add(type_)
        implementation = self.find_trait_implementation(type_, Type("Drop"))
        if implementation is not None and type_ not in self.drop_instances:
            _, substitutions = implementation
            signature = self.methods[(type_.name, "drop")]
            self.drop_instances[type_] = self.instantiate(signature, substitutions, node)
        # 按值字段和容器元素在析构时也会到达；指针目标绝不展开。
        for argument in type_.args:
            self.ensure_drop_type(argument, node, visited)
        declaration = self.types.get(type_.name, {})
        substitutions = {"$" + g["name"]: t for g, t in zip(declaration.get("generics", []), type_.args)}
        fields = ([f["type"] for f in declaration.get("fields", [])]
                  + [t for v in declaration.get("variants", []) for t in v["payload"]])
        for field in fields:
            self.ensure_drop_type(substitute(self.type_of(field, self.generic_set(declaration)), substitutions), node, visited)
        self._drop_type_visits.add(type_)
