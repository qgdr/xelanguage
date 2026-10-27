"""源码类型到现有语义类型的解析边界，不负责值的赋值或子类型判断。

TypeResolver 是 Checker 的协作型 mixin，不是另一套独立检查器。本轮只搬移
已有运行时类型方法，保留别名作用域、附件检查顺序、错误消息及延迟实例化的行为。
尤其不能因一个类型被解析成功，就推断它可赋给另一类型、具有 Copy/Drop、
采用某种运行布局。set / union 仅接入独立的编译期声明表，newtype 尚未实现。

宿主提供声明表与当前泛型/Self 环境、fail 诊断出口，以及 FromFn 所需的既有
回调签名检查。依赖只在 TYPE_CHECKING 中声明；运行时不导入 semantic，
因此 semantic 可以反向导入本 mixin，而不形成循环初始化。
"""
from typing import TYPE_CHECKING, Any, NoReturn

from .access import view_type
from .declaration_domains import resolve_declaration_domain
from .language_policy import current_profile
from .semantic_sync import SYNC_TYPES
from .source import Source
from .stdlib_io import IO_TYPE_ALIASES
from .type_domains import Domain
from .typesys import NONE, PRIMITIVES, STANDARD, UNIT, Type, callable_type, maybe, ptr

Node = dict[str, Any]


class TypeResolver:
    """消费类型 AST 的单一入口；字段仍由 Checker 初始化与持有。

    下面的静态契约没有运行时实现或第二份状态：
    - aliases/alias_types/_alias_stack 属于模块别名解析和缓存；
    - generic_names/self_type/type_substitutions 属于当前检查上下文；
    - types 是既有名义声明表，不是值域或运行时类型注册表；
    - fail/has_variable/iterator_callback_result 由同一个 Checker 提供。
    """

    if TYPE_CHECKING:
        aliases: dict[str, Node]
        alias_types: dict[str, Type]
        alias_domains: dict[str, Domain]
        source: Source
        _alias_stack: list[str]
        generic_names: set[str]
        self_type: Type | None
        type_substitutions: dict[str, Type]
        types: dict[str, Node]

        def fail(self, node: Node, message: str, code="XE-TYPE-0001", hint=None) -> NoReturn: ...

        @staticmethod
        def has_variable(type_: Type) -> bool: ...

        def iterator_callback_result(self, callback: Type, node: Node) -> Type: ...

    def resolve_alias(self, name):
        if name in self.alias_domains:
            self.fail(self.aliases[name], f"{name} 仅完成编译期集合/并集声明；尚未开放运行时类型使用",
                      "XE-CORE-0002")
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

    def resolve_type_declarations(self) -> None:
        """Collect scalar declarations without inventing a runtime Type/layout.

        The two caches deliberately remain distinct: alias_types contains
        runtime types; alias_domains contains compile-time normal forms only.
        Resolve dependencies before classification so a forward Domain alias
        keeps that distinction even when its normal form equals i32 or Never.
        """
        def lookup(name: str) -> Type | Domain | None:
            if name in PRIMITIVES:
                return Type(name)
            if name in self.alias_domains:
                return self.alias_domains[name]
            return self.resolve_alias(name) if name in self.aliases else None

        visiting: list[str] = []
        done: set[str] = set()

        def dependencies(node: Node) -> None:
            kind = node["kind"]
            if kind == "NamedType":
                target = "::".join(node["path"]["parts"])
                if target in self.aliases:
                    resolve(target)
                for argument in node["arguments"]:
                    dependencies(argument)
            elif kind == "PointerType":
                dependencies(node["target"])
            elif kind == "UnionType":
                dependencies(node["left"])
                dependencies(node["right"])
            elif kind == "SetType":
                for member in node["members"]:
                    if member["type"] is not None:
                        dependencies(member["type"])
            # Nominal declarations are intentionally not followed here. An
            # alias naming an enum stops at that declaration's type identity;
            # payload layout remains the separate nominal checker contract.

        def resolve(name: str) -> None:
            if name in done:
                return
            declaration = self.aliases[name]
            if name in visiting:
                cycle = " -> ".join(visiting[visiting.index(name):] + [name])
                self.fail(declaration, f"循环透明别名的规则尚未确定：{cycle}", "XE-CORE-0001")
            if len(visiting) >= 128:
                self.fail(declaration, "类型别名依赖超过当前128层展开上限", "XE-CORE-0002")
            visiting.append(name)
            try:
                node = declaration["type"]
                if current_profile() == "core":
                    dependencies(node)
                is_domain = node["kind"] in {"SetType", "UnionType"}
                if node["kind"] == "NamedType":
                    target = "::".join(node["path"]["parts"])
                    is_domain = target in self.alias_domains
                if is_domain:
                    self.alias_domains[name] = resolve_declaration_domain(self.source, node, lookup)
                else:
                    self.resolve_alias(name)
                done.add(name)
            finally:
                visiting.pop()

        for name in self.aliases:
            resolve(name)

    def require_no_type_arguments(self, name, arguments):
        """T 与 Self 表示完整类型，不是等待继续代入的类型构造器。"""
        if arguments:
            self.fail(arguments[0], f"{name} 已经表示完整类型，不能再附加类型参数",
                      hint=f"直接使用 {name}；当前不支持高阶类型参数")

    def array_length_type(self, node):
        """Array 的既有长度位置只接收字面量，不是通用常量泛型。

        表达式类型应用此前允许把该字面量用括号分组，继续保留；但不能把
        Python 中恰好是 int 子类的 bool 当成整数长度。
        """
        literal = node
        while literal["kind"] == "Group":
            literal = literal["expression"]
        if (literal["kind"] != "Literal" or literal.get("literal_kind") != "INTEGER"
                or type(literal["value"]) is not int or literal["value"] < 0):
            self.fail(node, "Array 的第二个附件必须是非负整数字面量长度")
        return Type(str(literal["value"]))

    def type_of(self, node: Node | None, generics=None, self_type=None) -> Type:
        if node is not None and node.get("kind") == "InternalResolvedType":
            invalid = node.get("_invalid_type_application")
            if invalid:
                self.require_no_type_arguments(invalid["name"], invalid["arguments"])
            return node["resolved_type"]
        if node is None:
            return UNIT
        generics = self.generic_names if generics is None else generics
        self_type = self.self_type if self_type is None else self_type
        kind = node["kind"]
        if kind in {"SetType", "UnionType"}:
            self.fail(node, "集合/并集类型目前只接入 type 声明，不生成运行时表示", "XE-CORE-0002")
        if kind == "ViewTypeModifier":
            self.fail(node, "旧 View[T][mut] 已取消；View[P] 的 P 写完整指针类型", "XE-CORE-0001")
        if kind == "NamedType":
            name = "::".join(node["path"]["parts"])
            if name == "View" and current_profile() == "core":
                # Admission limits the source occurrence to a direct local
                # annotation. Use the existing internal capability identity,
                # never a new Type("View") layout or legacy builtin.
                if len(node["arguments"]) != 1:
                    self.fail(node, "View 需要一个完整定位结果指针类型附件")
                pointer = self.type_of(node["arguments"][0], generics, self_type)
                if pointer.name != "ptr":
                    self.fail(node, "View[P] 的 P 必须是完整指针类型，例如 i32@", "XE-CORE-0001")
                return view_type(pointer)
            if name == "Self":
                if self_type is None:
                    self.fail(node, "Self 只能用于 impl/Trait 上下文", "XE-NAME-0001")
                self.require_no_type_arguments(name, node["arguments"])
                return self_type
            if name in generics:
                self.require_no_type_arguments(name, node["arguments"])
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
            arities = {"Array": 2, "Vec": 1, "Slice": 1, "SliceMut": 1,
                       "Box": 1, "Map": 2, "Set": 1, "Range": 1, "Iterator": 1, "FromFn": 1, "Step": 1,
                       **{name: 1 for name in SYNC_TYPES}}
            arity = len(self.types[name].get("generics", [])) if name in self.types else arities.get(name, 0)
            args = [self.array_length_type(arg) if name == "Array" and index == 1
                    else self.type_of(arg, generics, self_type)
                    for index, arg in enumerate(node["arguments"])]
            # 保留既有优先级：附件里的未知名称先报名称错误，再检查附件数目。
            if len(args) != arity:
                self.fail(node, f"{name} 需要 {arity} 个类型附件，实际为 {len(args)} 个")
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
        if kind == "Literal":
            self.fail(node, "这里需要类型实参，不能使用值字面量", hint="整数长度仅用于 Array[T, N] 的第二个附件")
        self.fail(node, f"尚未支持的类型结构 {kind}", "XE-SEM-0001")
