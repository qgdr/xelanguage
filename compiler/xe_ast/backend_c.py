"""可读 C 后端，独立于旧 LLVM 实验实现。

先用 AnnotatedChecker 验证语义并记录 AST 的类型，不猜测名称/隐式借用。
表达式按 Xe 顺序降低成显式临时变量，避免 C 参数/字段求值顺序的不确定性。
每个拥有存储位置有运行时活跃标记；移动清除标记，退出作用域按逆序清理。
这样条件移动、提前返回及部分字段移动不需要依赖 Python 的线性分析状态。
未支持的节点必须报诊断，不能产生空代码或忽略语义。
"""
from dataclasses import dataclass, field
from pathlib import Path
import string
from .parser import parse_source
from .semantic import Checker, Signature
from .source import Source
from .stdlib_io import IO_NATIVE_FUNCTIONS, normalize_io_name
from .stdlib_env import env_function
from .stdlib_iter import FROM_FN, callback_type, next_result
from .typesys import Type, UNIT, NEVER, BOOL, STR, STRING, NONE, INT_LITERAL, I32, NUMERIC, ptr, substitute


class AnnotatedChecker(Checker):
    """记录侧表，不改变冻结的 AST JSON。id(node) 在本次编译期间稳定。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expression_types, self.binding_types = {}, {}
        self.expression_expected_types = {}

    def infer(self, node, expected=None, lift=False):
        value = super().infer(node, expected, lift)
        self.expression_types[id(node)] = value.type
        if expected is not None:
            self.expression_expected_types[id(node)] = expected
        return value

    def place(self, node, read=True):
        value = super().place(node, read)
        self.expression_types[id(node)] = value.type
        return value

    def declare(self, name, type_, node, *args, **kwargs):
        self.binding_types[id(node)] = type_
        return super().declare(name, type_, node, *args, **kwargs)


@dataclass
class Slot:
    name: str
    type: Type
    flags: dict[tuple[str, ...], str] = field(default_factory=dict)
    order: str | None = None
    epoch: str | None = None
    # 捕获环境的非拥有字段别名不参与当前函数的 Drop；原位替换仍要释放旧值。
    borrowed: bool = False


@dataclass
class CValue:
    code: str
    type: Type
    slot: Slot | None = None
    path: tuple[str, ...] = ()


@dataclass
class Scope:
    names: dict[str, Slot] = field(default_factory=dict)
    owned: list[Slot] = field(default_factory=list)
    epoch: str | None = None


def identifier(name):
    """生成标识符不会与 C 关键字冲突，也支持 Xe Unicode 名称。"""
    return "xe_" + "".join(c if c.isascii() and c.isalnum() else f"_{ord(c):x}_" for c in name)


def literal_string(text):
    data = text.encode("utf-8")
    # 三位八进制转义不会像 \\x 那样吞掉后续十六进制字符。
    encoded = "".join(f"\\{byte:03o}" for byte in data)
    return f'(XeStr){{(const unsigned char *)"{encoded}", {len(data)}}}'


class CBackend:
    def __init__(self, checker):
        self.checker = checker
        self.tree = checker.tree
        self.lines, self.scopes, self.loop_scopes = [], [], []
        self.counter, self.indent = 0, 0
        self.signature = None
        self.definitions, self.defining = {}, set()
        self.tuple_names, self.layout_types = {}, {}
        self.function_typedefs, self.function_typedef_names = {}, {}
        self.anonymous_names, self.pending_functions = {}, []
        self.closure_function_names, self.pending_closure_functions = {}, []
        self.user_layout_names = {}
        self.io_function_definitions = {}
        self.env_function_definitions = {}

    def fail(self, node, message):
        self.checker.fail(node, message, "XE-BACKEND-0001",
                          "该程序可能已通过语义检查，但此功能尚未接入 C 后端")

    def fresh(self, label="tmp"):
        self.counter += 1
        return f"{identifier(label)}_{self.counter}"

    def line(self, text):
        self.lines.append("    " * self.indent + text)

    def type_at(self, node, expected=None):
        type_ = self.checker.expression_types.get(id(node), expected)
        if type_ is None:
            self.fail(node, "缺少表达式类型信息")
        if type_ == INT_LITERAL:
            expected = expected or self.checker.expression_expected_types.get(id(node))
            return expected if expected and expected.name in NUMERIC else I32
        return type_

    def ctype(self, type_, at=None):
        at = at or self.tree
        primitives = {f"{s}{b}": f"{'int' if s == 'i' else 'uint'}{b}_t"
                      for s in ("i", "u") for b in (8, 16, 32, 64)}
        primitives.update({"bool": "bool", "char": "uint32_t", "str": "XeStr",
                           "String": "XeString", "Unit": "XeUnit", "Never": "XeUnit",
                           "usize": "size_t", "isize": "intptr_t", "f32": "float", "f64": "double",
                           "None": "XeUnit", "ConversionError": "XeUnit",
                           "File": "XeFile", "io::Error": "int"})
        if type_.name in primitives:
            return primitives[type_.name]
        if type_.name == "ptr":
            if type_.args[0].name in self.checker.types:
                # A pointer only needs its target's forward declaration. Do not
                # eagerly expand Node[Node[T]]@ into infinitely many layouts.
                return self.user_layout_name(type_.args[0], at) + " *"
            return self.ctype(type_.args[0], at) + " *"
        if type_.name == "fn":
            if type_ not in self.function_typedef_names:
                name = identifier(f"function_value_{len(self.function_typedef_names)}")
                self.function_typedef_names[type_] = name
                # A typedef keeps declarations simple even when a function takes
                # or returns another function value. Dependencies are emitted first.
                parameters = [self.ctype(t, at) for t in type_.args[:-1]]
                result = self.ctype(type_.args[-1], at)
                self.function_typedefs[type_] = f"typedef {result} (*{name})({', '.join(parameters) or 'void'});"
            return self.function_typedef_names[type_]
        if type_.name == "closure":
            if type_ not in self.tuple_names:
                cname = identifier(f"closure_environment_{len(self.tuple_names)}")
                self.tuple_names[type_] = cname
                self.layout_types[type_] = type_
                members = []
                for name, capture_type in self.fields(type_):
                    capture_c = self.ctype(capture_type, at)
                    if capture_type.name in self.checker.types:
                        self.define_type(capture_type)
                    members.append(f"    {capture_c} {identifier(name)};")
                self.definitions[type_] = (f"struct {cname} {{\n" + "\n".join(members) + "\n};")
            return self.tuple_names[type_]
        if type_.name == "FromFn":
            if type_ not in self.tuple_names:
                cname = identifier(f"iterator_from_fn_{len(self.tuple_names)}")
                self.tuple_names[type_] = cname
                self.layout_types[type_] = type_
                callback_c = self.ctype(callback_type(type_), at)
                self.definitions[type_] = (f"struct {cname} {{ {callback_c} "
                    f"{identifier('callback')}; bool {identifier('done')}; }};")
            return self.tuple_names[type_]
        if type_.name in {"maybe", "Array"}:
            if type_ not in self.tuple_names:
                cname = identifier(f"{type_.name}_layout_{len(self.tuple_names)}")
                self.tuple_names[type_] = cname
                self.layout_types[type_] = type_
                members = ["    int tag;"] if type_.name == "maybe" else []
                if type_.name == "maybe":
                    payloads = [(v + '_' + str(i), t) for v, ts in self.variants(type_)
                                for i, t in enumerate(ts)]
                    for field_name, element in payloads:
                        ctype = self.ctype(element, at)
                        if element.name in self.checker.types:
                            self.define_type(element)
                        members.append(f"    {ctype} {identifier(field_name)};")
                else:
                    element, count = type_.args
                    ctype = self.ctype(element, at)
                    if element.name in self.checker.types:
                        self.define_type(element)
                    # ISO C disallows zero-sized arrays; length remains statically zero in Xe.
                    members.append(f"    {ctype} items[{max(1, int(count.name))}];")
                self.definitions[type_] = f"struct {cname} {{\n" + "\n".join(members) + "\n};"
            return self.tuple_names[type_]
        if type_.name in {"Slice", "SliceMut"}:
            if type_ not in self.tuple_names:
                cname = identifier(f"{type_.name}_layout_{len(self.tuple_names)}")
                self.tuple_names[type_] = cname
                self.layout_types[type_] = type_
                element = type_.args[0]
                # Slice stores an element pointer, not an element by value.
                # Recursive Node -> Slice[Node] is finite just like Node@.
                ctype = (self.user_layout_name(element, at) if element.name in self.checker.types
                         else self.ctype(element, at))
                self.definitions[type_] = f"struct {cname} {{ {ctype} *data; size_t len; }};"
            return self.tuple_names[type_]
        if type_.name == "Range":
            if type_ not in self.tuple_names:
                cname = identifier(f"Range_layout_{len(self.tuple_names)}")
                self.tuple_names[type_] = cname
                self.layout_types[type_] = type_
                ctype = self.ctype(type_.args[0], at)
                self.definitions[type_] = (f"struct {cname} {{ {ctype} lower, upper; "
                                           "bool has_lower, has_upper, closed; };")
            return self.tuple_names[type_]
        if type_.name == "tuple":
            if type_ not in self.tuple_names:
                # 内部布局以 Type 对象为键，不能与用户结构体名称碰撞。
                key = type_
                cname = identifier(f"tuple_layout_{len(self.tuple_names)}")
                self.tuple_names[type_] = cname
                self.layout_types[key] = type_
                members = []
                for i, element in enumerate(type_.args):
                    ctype = self.ctype(element, at)
                    if element.name in self.checker.types:
                        self.define_type(element)
                    members.append(f"    {ctype} {identifier(str(i))};")
                self.definitions[key] = f"struct {cname} {{\n" + "\n".join(members) + "\n};"
            return self.tuple_names[type_]
        if type_.name in self.checker.types:
            cname = self.user_layout_name(type_, at)
            self.define_type(type_)
            return cname
        self.fail(at, f"C 后端尚不支持类型 {type_}")

    def user_layout_name(self, type_, at):
        node = self.checker.types[type_.name]
        if node["kind"] not in {"Struct", "Enum"}:
            self.fail(at, "Trait 约束不能作为具体 C 数据布局")
        if type_ not in self.user_layout_names:
            if len(self.user_layout_names) >= getattr(self.checker, "instance_limit", 256):
                self.checker.fail(at, "具体数据布局过多；请检查不断增加类型层数的递归泛型",
                                  "XE-GENERIC-0002")
            # Type, not declaration name, is the instance cache key.
            key = type_ if type_.args else type_.name
            cname = identifier("type_" + type_.name +
                               (f"_instance_{len(self.user_layout_names)}" if type_.args else ""))
            self.user_layout_names[type_] = cname
            self.layout_types[key] = type_
        return self.user_layout_names[type_]

    def concrete_member_type(self, node, owner):
        """Resolve declaration-local T/Self using the owning concrete instance."""
        declaration = self.checker.types[owner.name]
        bindings = {"$" + parameter["name"]: argument for parameter, argument in
                    zip(declaration.get("generics", []), owner.args)}
        template = self.checker.type_of(node, self.checker.generic_set(declaration), owner)
        return substitute(template, bindings)

    def fields(self, type_):
        if type_.name == "FromFn":
            return [("callback", callback_type(type_)), ("done", BOOL)]
        if type_.name == "closure":
            info = self.checker.closures.get(type_)
            if info is None:
                self.fail(self.tree, f"闭包 {type_} 缺少已验证的捕获环境")
            return list(info.captures)
        if type_.name == "tuple":
            return [(str(i), t) for i, t in enumerate(type_.args)]
        if type_.name == "Array":
            # Each resource element has a flag: an initializer may return midway.
            return [(f"[{i}]", type_.args[0]) for i in reversed(range(int(type_.args[1].name)))]
        node = self.checker.types.get(type_.name, {})
        return [(f["name"], self.concrete_member_type(f["type"], type_)) for f in node.get("fields", [])]

    def variants(self, type_):
        """载荷布局与标签顺序均来自已验证的枚举声明。"""
        if type_.name == "maybe":
            success, error = type_.args
            return [("Yes", [success]), ("None", []) if error == NONE else ("No", [error])]
        node = self.checker.types.get(type_.name, {})
        return [(v["name"], [self.concrete_member_type(t, type_) for t in v["payload"]])
                for v in node.get("variants", [])]

    def payload_code(self, code, variant, index):
        return f"({code}).{identifier(variant + '_' + str(index))}"

    def define_type(self, type_):
        type_ = Type(type_) if isinstance(type_, str) else type_
        key = type_ if type_.args else type_.name
        if key in self.definitions:
            return
        node = self.checker.types[type_.name]
        if node["kind"] not in {"Struct", "Enum"}:
            self.fail(node, "通用 Trait 布局尚不支持")
        if len(type_.args) != len(node.get("generics", [])):
            self.fail(node, f"布局 {type_} 缺少具体泛型参数")
        if key in self.defining:
            self.fail(node, "类型包含按值递归字段，必须通过指针或拥有容器间接连接")
        if len(self.defining) >= 64:
            # Expanding generic recursion can keep changing the Type key, so
            # the ordinary cycle set alone is insufficient. Bound nesting
            # before Python's call stack overflows and retain a Xe diagnostic.
            self.checker.fail(node, "具体数据布局嵌套超过 64 层；请检查按值递归泛型",
                              "XE-GENERIC-0002")
        self.defining.add(key)
        fields, payloads = self.fields(type_), self.variants(type_)
        cname = self.user_layout_name(type_, node)
        for member_type in [t for _, t in fields] + [t for _, ts in payloads for t in ts]:
            self.ctype(member_type, node)
            if member_type.name in self.checker.types:
                self.define_type(member_type)
        members = [f"    {self.ctype(t)} {identifier(f)};" for f, t in fields]
        if node["kind"] == "Enum":
            members = ["    int tag;"]
            for variant, types in payloads:
                for index, type_ in enumerate(types):
                    members.append(f"    {self.ctype(type_)} {identifier(variant+'_'+str(index))};")
        if not members:
            members = ["    unsigned char empty; /* C 不允许空结构体 */"]
        self.definitions[key] = "struct " + cname + " {\n" + "\n".join(members) + "\n};"
        self.defining.remove(key)

    def resource_paths(self, type_, path=()):
        if self.checker.copyable(type_) or type_.name == "ptr":
            return []
        result = [path]
        for name, field_type in self.fields(type_):
            result.extend(self.resource_paths(field_type, path + (name,)))
        return result

    def storage(self, type_, label="tmp", initial=None):
        name = self.fresh(label)
        self.line(f"{self.ctype(type_)} {name}" + (f" = {initial}" if initial is not None else "") + ";")
        slot = Slot(name, type_)
        self.scopes[-1].owned.append(slot)
        for path in self.resource_paths(type_):
            flag = self.fresh("live")
            slot.flags[path] = flag
            self.line(f"bool {flag} = {'true' if initial is not None else 'false'};")
        self.register_order(slot)
        return slot

    def register_order(self, slot):
        if not slot.flags:
            return
        scope = self.scopes[-1]
        if scope.epoch is None:
            scope.epoch = self.fresh("scope_epoch")
            self.line(f"uint64_t {scope.epoch} = 0;")
        slot.epoch, slot.order = scope.epoch, self.fresh("init_order")
        self.line(f"uint64_t {slot.order} = ++{slot.epoch};")

    def initialized(self, slot):
        if slot.order:
            self.line(f"{slot.order} = ++{slot.epoch}; /* initialization completed */")

    def value(self, slot):
        return CValue(slot.name, slot.type, slot)

    def temp(self, type_, code):
        return self.value(self.storage(type_, initial=code))

    def argument(self, value):
        """在本次求值位置快照实参，不能等后续实参修改对象后再读取变量。"""
        if value.type == NEVER:
            return value
        result = self.temp(value.type, value.code)
        self.transfer(value)
        return result

    def lookup(self, name, node):
        for scope in reversed(self.scopes):
            if name in scope.names:
                return self.value(scope.names[name])
        self.fail(node, f"后端找不到局部变量 {name}")

    def field_code(self, code, path):
        for name in path:
            code = f"({code}).items{name}" if name.startswith("[") else f"({code}).{identifier(name)}"
        return code

    def transfer(self, value):
        """转交只清活跃标记，不清数据：后续生成的赋值/调用仍需要这些位。"""
        if value.slot and not self.checker.copyable(value.type):
            for path, flag in value.slot.flags.items():
                if path[:len(value.path)] == value.path:
                    self.line(f"{flag} = false; /* move */")

    def assign(self, slot, value, at):
        if value.type == NEVER:
            return
        if slot.type.name == "maybe" and (value.type.name != "maybe" or value.type == slot.type.args[0]):
            # Only the checker authorizes implicit lifting (function/handler return positions).
            # Backend wrapping must not lose ownership of a resource success payload.
            absent = value.type == NONE
            self.transfer(value)
            self.line(f"{slot.name} = ({self.ctype(slot.type)}){{.tag = {1 if absent else 0}}};")
            if not absent:
                self.line(f"{self.payload_code(slot.name, 'Yes', 0)} = {value.code};")
            for flag in slot.flags.values():
                self.line(f"{flag} = true;")
            self.initialized(slot)
            return
        if value.type != slot.type and not self.checker.compatible(value.type, slot.type):
            if value.type.name not in NUMERIC or slot.type.name not in NUMERIC:
                self.fail(at, f"尚未实现从 {value.type} 到 {slot.type} 的后端转换")
        self.transfer(value)
        self.line(f"{slot.name} = {value.code};")
        for flag in slot.flags.values():
            self.line(f"{flag} = true;")
        self.initialized(slot)

    def cleanup_slot(self, slot):
        for path, flag in slot.flags.items():
            type_ = slot.type
            for part in path:
                type_ = dict(self.fields(type_))[part]
            code = self.field_code(slot.name, path)
            if type_ == STRING:
                self.line(f"if ({flag}) xe_string_drop(&({code}));")
            elif type_.name == "File":
                self.line(f"if ({flag}) xe_file_drop(&({code}));")
            elif type_.name == "maybe" or self.checker.types.get(type_.name, {}).get("kind") == "Enum":
                self.line(f"if ({flag}) {{")
                self.indent += 1
                self.drop_complete(type_, code)
                self.indent -= 1
                self.line("}")
            elif type_.name in self.checker.drop_types:
                self.line(f"if ({flag}) {self.method_name(type_.name, 'drop')}(&({code}));")
            self.line(f"{flag} = false;")
        if slot.order:
            self.line(f"{slot.order} = 0;")

    def drop_complete(self, type_, code):
        """清理枚举中的完整拥有载荷，绝不能触碰非活动变体。

        普通结构体的部分移动由 Slot.flags 管理；枚举载荷只允许整体分流，
        因此这里的嵌套对象始终完整，不需要另一套部分移动标记。
        """
        if self.checker.copyable(type_) or type_.name == "ptr":
            return
        if type_ == STRING:
            self.line(f"xe_string_drop(&({code}));")
            return
        if type_.name == "File":
            self.line(f"xe_file_drop(&({code}));")
            return
        if type_.name == "Array":
            element, count = type_.args
            # Arrays are indivisible ownership containers; indexing only borrows an element.
            for index in reversed(range(int(count.name))):
                self.drop_complete(element, f"({code}).items[{index}]")
            return
        if type_.name in self.checker.drop_types:
            self.line(f"{self.method_name(type_.name, 'drop')}(&({code}));")
        variants = self.variants(type_)
        if variants:
            self.line(f"switch (({code}).tag) {{")
            self.indent += 1
            for tag, (variant, types) in enumerate(variants):
                self.line(f"case {tag}:")
                self.indent += 1
                for index, payload_type in enumerate(types):
                    self.drop_complete(payload_type, self.payload_code(code, variant, index))
                self.line("break;")
                self.indent -= 1
            self.line('default: xe_panic("invalid enum tag");')
            self.indent -= 1
            self.line("}")
        else:
            for name, field_type in self.fields(type_):
                self.drop_complete(field_type, self.field_code(code, (name,)))

    def cleanup(self, scopes=None):
        for scope in reversed(self.scopes if scopes is None else scopes):
            ordered = [slot for slot in scope.owned if slot.order]
            if not ordered:
                continue
            # 延后初始化和条件重新初始化改变实际顺序，不能静态反转声明列表。
            best, selected = self.fresh("latest"), self.fresh("selected")
            self.line("while (true) {")
            self.indent += 1
            self.line(f"uint64_t {best} = 0; int {selected} = -1;")
            for index, slot in enumerate(ordered):
                self.line(f"if ({slot.order} > {best}) {{ {best} = {slot.order}; {selected} = {index}; }}")
            self.line(f"if ({selected} < 0) break;")
            self.line(f"switch ({selected}) {{")
            self.indent += 1
            for index, slot in enumerate(ordered):
                self.line(f"case {index}:")
                self.indent += 1
                self.cleanup_slot(slot)
                self.line("break;")
                self.indent -= 1
            self.indent -= 1
            self.line("}")
            self.indent -= 1
            self.line("}")

    def method_name(self, owner, name):
        return identifier("method_" + owner + "_" + name)

    def function_name(self, name):
        return identifier("function_" + name)

    def prototype(self, signature, name, parameter_names=None):
        if signature.generics:
            self.fail(signature.node, "泛型函数实例化尚未接入 C 后端")
        if signature.node["body"] is None:
            self.fail(signature.node, "extern ABI 尚未接入 C 后端")
        if signature.self_type and signature.node["parameters"] and signature.node["parameters"][0]["name"] == "self":
            receiver = signature.parameters[0]
            base = receiver.args[0] if receiver.name == "ptr" else receiver
            if base != signature.self_type:
                self.fail(signature.node, "方法 self 类型必须对应 impl 的目标类型")
        parameters = [self.ctype(t, signature.node) + (" " + parameter_names[i] if parameter_names else "")
                      for i, t in enumerate(signature.parameters)]
        return f"static {self.ctype(signature.result, signature.node)} {name}({', '.join(parameters) or 'void'})"

    def emit_function(self, signature, name, closure_type=None, owning=True):
        self.signature = signature
        self.scopes = [Scope()]
        self.loop_scopes = []
        names = [self.fresh(p["name"]) for p in signature.node["parameters"]]
        self.line(self.prototype(signature, name, names) + " {")
        self.indent += 1
        for parameter, type_, cname in zip(signature.node["parameters"], signature.parameters, names):
            slot = Slot(cname, type_)
            self.scopes[0].names[parameter["name"]] = slot
            self.scopes[0].owned.append(slot)
            for path in self.resource_paths(type_):
                flag = self.fresh("live")
                slot.flags[path] = flag
                self.line(f"bool {flag} = true;")
            self.register_order(slot)
        if closure_type is not None:
            # 捕获名是环境字段的别名，不是复制出来的第二份资源。
            # 拥有调用将字段移动标记联到环境；指针调用不注册字段的 Drop。
            environment = self.scopes[0].names["$environment"]
            code = environment.name if owning else f"*({environment.name})"
            for capture_name, capture_type in self.fields(closure_type):
                flags = {path[1:]: flag for path, flag in environment.flags.items()
                         if path and path[0] == capture_name} if owning else {}
                alias = Slot(self.field_code(code, (capture_name,)), capture_type,
                             flags=flags, borrowed=not owning)
                self.scopes[0].names[capture_name] = alias
        result = self.storage(signature.result, "return", "0" if signature.result == UNIT else None)
        self.block(signature.node["body"], result)
        self.transfer(self.value(result))
        self.cleanup()
        self.line(f"return {result.name};")
        self.indent -= 1
        self.line("}\n")

    def block(self, node, target=None):
        self.line("{")
        self.indent += 1
        self.scopes.append(Scope())
        terminated = False
        for statement in node["statements"]:
            if self.statement(statement):
                terminated = True
                break
        if not terminated and node["tail"]:
            value = self.expression(node["tail"], target.type if target else None)
            if target:
                self.assign(target, value, node["tail"])
            elif value.slot and not self.checker.copyable(value.type):
                self.cleanup_slot(value.slot)
        elif not terminated and target and target.type == UNIT:
            self.line(f"{target.name} = 0;")
        self.cleanup([self.scopes[-1]])
        self.scopes.pop()
        self.indent -= 1
        self.line("}")

    def statement(self, node):
        kind = node["kind"]
        if kind == "Binding":
            type_ = self.checker.binding_types[id(node)]
            value = self.expression(node["value"], type_) if node["value"] else None
            if value and value.type == NEVER:
                return True
            slot = self.storage(type_, node["name"])
            if value:
                self.assign(slot, value, node)
            self.scopes[-1].names[node["name"]] = slot
        elif kind == "Destructure":
            return self.destructure(node)
        elif kind == "Assignment":
            left = node["right"] if node["operator"] == ">>" else node["left"]
            right = node["left"] if node["operator"] == ">>" else node["right"]
            target = self.expression(left)
            address = self.temp(ptr(target.type), f"&({target.code})")
            target = CValue(f"*({address.code})", target.type, target.slot, target.path)
            value = self.expression(right, target.type)
            if value.type == NEVER:
                return True
            # 替换拥有值时先析构旧值，指针原位替换也必须释放旧资源。
            if not self.checker.copyable(target.type):
                if target.slot and target.slot.borrowed:
                    self.drop_complete(target.type, target.code)
                elif target.slot and not target.path:
                    self.cleanup_slot(target.slot)
                elif target.slot:
                    # 拥有聚合的字段可能已移动过：借其实际 flag 清理，不能
                    # 无条件析构旧位，也不能因字段类型不是 String 就漏掉 Drop。
                    prefix = len(target.path)
                    flags = {path[prefix:]: flag for path, flag in target.slot.flags.items()
                             if path[:prefix] == target.path}
                    self.cleanup_slot(Slot(target.code, target.type, flags=flags))
                elif target.type == STRING:
                    self.line(f"xe_string_drop(&({target.code}));")
                else:
                    self.fail(node, "该资源字段的原位替换清理尚未实现")
            self.transfer(value)
            self.line(f"{target.code} = {value.code};")
            if target.slot:
                for path, flag in target.slot.flags.items():
                    if path[:len(target.path)] == target.path:
                        self.line(f"{flag} = true;")
                if not target.path:
                    self.initialized(target.slot)
        elif kind == "Return":
            value = self.expression(node["value"], self.signature.result) if node["value"] else CValue("0", UNIT)
            returned = self.storage(self.signature.result, "early_return")
            self.assign(returned, value, node)
            self.transfer(self.value(returned))
            self.cleanup()
            self.line(f"return {returned.name};")
            return True
        elif kind in {"Break", "Continue"}:
            if not self.loop_scopes:
                self.fail(node, "后端没有对应循环")
            self.cleanup(self.scopes[self.loop_scopes[-1]:])
            self.line(kind.lower() + ";")
            return True
        else:
            value = self.expression(node["expression"])
            if value.type == NEVER:
                return True
            if value.slot and not self.checker.copyable(value.type):
                self.cleanup_slot(value.slot)
        return False

    def destructure(self, node):
        """Snapshot all members before replacing any existing destination.

        The tuple snapshot owns resources until each member has been transferred;
        normal scope cleanup also covers a return during RHS construction.
        Ignored members receive ordinary owned storage so nested resources and
        user Drop implementations are cleaned exactly once.
        """
        if self.type_at(node["value"]) == NEVER:
            self.expression(node["value"])
            return True
        types = [self.checker.destructure_types[id(target)] for target in node["targets"]]
        expected = Type("tuple", tuple(types))
        value = self.expression(node["value"], expected)
        if value.type == NEVER:
            return True
        subject = self.argument(value)
        for index, (target, type_) in enumerate(zip(node["targets"], types)):
            member = CValue(self.field_code(subject.code, (str(index),)), subject.type.args[index],
                            subject.slot, subject.path + (str(index),))
            if target["name"] == "_":
                ignored = self.storage(type_, "ignored")
                self.assign(ignored, member, target)
                self.cleanup_slot(ignored)
            elif node["declare"]:
                slot = self.storage(type_, target["name"])
                self.assign(slot, member, target)
                self.scopes[-1].names[target["name"]] = slot
            else:
                slot = self.lookup(target["name"], target).slot
                if not self.checker.copyable(slot.type):
                    self.cleanup_slot(slot)
                self.assign(slot, member, target)
        return False

    def expression(self, node, expected=None):
        kind = node["kind"]
        type_ = self.type_at(node, expected)
        if kind in {"Tuple", "Array"} and type_ == NEVER:
            # An aggregate that returns during construction has no completed
            # layout. Own each evaluated member until return cleanup runs.
            for element in node["elements"]:
                value = self.expression(element)
                if value.type == NEVER:
                    return value
                self.argument(value)
            self.fail(node, "Never 聚合值没有终止表达式")
        target = getattr(self.checker, "call_targets", {}).get(id(node))
        # for 也记录 next 的具体泛型目标，但 for 本身不是函数值。
        if target is not None and kind in {"Name", "BracketApply", "AssociatedAccess"}:
            return self.temp(type_, self.function_name(target))
        if kind == "Literal":
            category, value = node["literal_kind"], node["value"]
            if category == "STRING":
                return self.temp(STR, literal_string(value))
            if category in {"CHAR", "BYTE"}:
                return self.temp(type_, str(ord(value)))
            if category in {"true", "false"}:
                return self.temp(BOOL, "true" if value else "false")
            if category == "unit":
                return CValue("0", UNIT)
            # INT64_MIN 的正数字面量在 C 中不可表示成有符号 long long。
            code = str(value) if category == "FLOAT" else (f"UINT64_C({value})" if value >= 0 else str(value))
            return self.temp(type_, code)
        if kind == "Name":
            parts = node["path"]["parts"]
            name = "::".join(parts)
            if len(parts) == 1 and any(name in scope.names for scope in self.scopes):
                return self.lookup(name, node)
            if name in self.checker.functions:
                return self.temp(type_, self.function_name(name))
            if name in self.checker.constants:
                # Prelude functions never override a user-defined value.
                # Keep the same lookup order as the semantic checker.
                return self.expression(self.checker.constants[name].node, type_)
            if normalize_io_name(name) == "readline":
                return self.temp(type_, self.readline_function(type_.args[-1]))
            io_native = IO_NATIVE_FUNCTIONS.get(normalize_io_name(name))
            if io_native:
                return self.temp(type_, io_native)
            if env_function(name) is not None:
                return self.temp(type_, self.args_function(type_.args[-1]))
            if name == "Maybe::None":
                return self.temp(type_, f"({self.ctype(type_)}){{.tag = 1}}")
            if len(parts) == 2:
                # Aliases have the canonical enum's type/layout, while the AST
                # retains the spelling used by the programmer.
                for index, (variant, _) in enumerate(self.variants(type_)):
                    if variant == parts[1]:
                        return self.temp(type_, f"({self.ctype(type_)}){{.tag = {index}}}")
            return self.lookup(name, node)
        if kind == "NoneValue":
            return CValue("0", NONE)
        if kind == "AssociatedAccess" and self.variants(type_):
            tag = next(index for index, (name, _) in enumerate(self.variants(type_))
                       if name == node["member"])
            return self.temp(type_, f"({self.ctype(type_)}){{.tag = {tag}}}")
        if kind == "Group":
            return self.expression(node["expression"], expected)
        if kind == "AnonymousFunction":
            return self.anonymous_function(node, type_)
        if kind == "Cast":
            value = self.expression(node["operand"])
            if node["modifiers"]:
                self.fail(node, "checked/wrap 数值转换尚未接入 C 后端")
            return self.temp(type_, f"({self.ctype(type_)})({value.code})")
        if kind == "Borrow":
            value = self.expression(node["operand"])
            return self.temp(type_, f"&({value.code})")
        if kind == "Dereference":
            value = self.expression(node["operand"])
            return CValue(f"*({value.code})", type_)
        if kind == "FieldAccess":
            value = self.expression(node["object"])
            base = value.type.args[0] if value.type.name == "ptr" else value.type
            if base.name in self.checker.types:
                # Inspecting fields, unlike holding an address, needs a complete
                # target layout. Lazy completion preserves recursive pointers.
                self.define_type(base)
            operator = "->" if value.type.name == "ptr" else "."
            return CValue(f"({value.code}){operator}{identifier(node['field'])}", type_,
                          value.slot if operator == "." else None, value.path + (node["field"],))
        if kind in {"StructLiteral", "Tuple"}:
            slot = self.storage(type_)
            fields = dict(self.fields(type_))
            initializers = (node["fields"] if kind == "StructLiteral" else
                            [{"name": str(i), "value": n} for i, n in enumerate(node["elements"])])
            if not initializers:
                self.line(f"{slot.name}.empty = 0;")
            for field in initializers:
                value = self.expression(field["value"], fields[field["name"]])
                if value.type == NEVER:
                    # An initializer may explicitly return from the enclosing
                    # function. Its return already cleaned initialized fields;
                    # there is no payload to assign to the next field.
                    return value
                self.transfer(value)
                self.line(f"{slot.name}.{identifier(field['name'])} = {value.code};")
                for path, flag in slot.flags.items():
                    if path and path[0] == field["name"]:
                        self.line(f"{flag} = true;")
            if () in slot.flags:
                self.line(f"{slot.flags[()]} = true;")
            self.initialized(slot)
            return self.value(slot)
        if kind == "Call":
            return self.call(node)
        if kind == "BracketApply":
            return self.enum_value(node, type_)
        if kind == "Array":
            slot = self.storage(type_)
            for index, element in enumerate(node["elements"]):
                value = self.expression(element, type_.args[0])
                if value.type == NEVER:
                    return value
                self.transfer(value)
                self.line(f"{slot.name}.items[{index}] = {value.code};")
                for path, flag in slot.flags.items():
                    if path and path[0] == f"[{index}]":
                        self.line(f"{flag} = true;")
            for flag in slot.flags.values():
                self.line(f"{flag} = true;")
            self.initialized(slot)
            return self.value(slot)
        if kind == "Range":
            lower = self.argument(self.expression(node["lower"], type_.args[0])) if node["lower"] else CValue("0", type_.args[0])
            upper = self.argument(self.expression(node["upper"], type_.args[0])) if node["upper"] else CValue("0", type_.args[0])
            values = [lower.code, upper.code, "true" if node["lower"] else "false",
                      "true" if node["upper"] else "false", "true" if node["operator"] == "..=" else "false"]
            return self.temp(type_, f"({self.ctype(type_)}){{{', '.join(values)}}}")
        if kind in {"Propagate", "Unwrap"}:
            return self.unwrap(node, type_, propagate=kind == "Propagate")
        if kind == "Pipeline":
            # 先快照输入再执行处理器，资源所有权沿管道转交。
            handler = node["handler"]
            input_type = None
            if handler["kind"] == "HandlerBinding" and handler["parameters"][0]["type"]:
                input_type = self.checker.binding_types[id(handler["parameters"][0])]
            elif handler["kind"] == "FunctionTarget":
                signature_type = self.checker.expression_types.get(id(handler["target"]))
                if signature_type and signature_type.name == "ptr":
                    signature_type = signature_type.args[0]
                if signature_type and signature_type.name in {"fn", "closure"} and len(signature_type.args) > 1:
                    input_type = signature_type.args[0]
            if input_type is None and type_.name in NUMERIC:
                input_type = type_
            value = self.argument(self.expression(node["input"], input_type))
            if value.type == NEVER:
                return value
            return self.handle(node["handler"], [value], type_)
        if kind == "Branch":
            return self.branch(node, type_)
        if kind == "Block":
            slot = self.storage(UNIT if type_ == NEVER else type_)
            self.block(node, slot)
            return CValue("0", NEVER) if type_ == NEVER else self.value(slot)
        if kind == "If":
            condition = self.expression(node["condition"], BOOL)
            slot = self.storage(UNIT if type_ == NEVER else type_)
            self.line(f"if ({condition.code})")
            self.block(node["then"], slot)
            if node["otherwise"]:
                self.line("else")
                if node["otherwise"]["kind"] == "Block":
                    self.block(node["otherwise"], slot)
                else:
                    self.line("{")
                    self.indent += 1
                    other = self.expression(node["otherwise"], slot.type)
                    self.assign(slot, other, node)
                    self.indent -= 1
                    self.line("}")
            elif slot.type == UNIT:
                self.line(f"else {{ {slot.name} = 0; }}")
            return CValue("0", NEVER) if type_ == NEVER else self.value(slot)
        if kind == "Unary":
            # 不先生成 +128 的 i8 临时变量，否则 -128 会被截断。
            if node["operator"] == "-" and node["operand"]["kind"] == "Literal" and node["operand"]["literal_kind"] == "INTEGER":
                number = node["operand"]["value"]
                code = "(-INT64_C(9223372036854775807)-1)" if number == 2**63 else f"(-INT64_C({number}))"
                return self.temp(type_, code)
            value = self.expression(node["operand"], type_)
            if node["operator"] == "not":
                return self.temp(BOOL, f"!({value.code})")
            if node["operator"] == "+":
                return value
            code = f"xe_sub_{type_.name}(0, {value.code})" if type_.name in NUMERIC - {"f32", "f64"} else f"-({value.code})"
            return self.temp(type_, code)
        if kind == "Binary":
            left = self.argument(self.expression(node["left"], type_))
            operator = node["operator"]
            if operator in {"and", "or"}:
                result = self.storage(BOOL, initial=left.code)
                self.line(f"if ({'!' if operator == 'or' else ''}{result.name}) {{")
                self.indent += 1
                self.scopes.append(Scope())
                right = self.expression(node["right"], BOOL)
                self.line(f"{result.name} = {right.code};")
                self.cleanup([self.scopes[-1]])
                self.scopes.pop()
                self.indent -= 1
                self.line("}")
                return self.value(result)
            right = self.expression(node["right"], type_)
            if operator == "%" and type_.name in {"f32", "f64"}:
                self.fail(node, "浮点余数运算尚未接入 C 后端")
            operations = {"+": "add", "-": "sub", "*": "mul", "/": "div", "%": "rem"}
            code = f"xe_{operations[operator]}_{type_.name}({left.code}, {right.code})" if type_.name not in {"f32", "f64"} else f"({left.code}) {operator} ({right.code})"
            return self.temp(type_, code)
        if kind == "ComparisonChain":
            operand_type = next((self.type_at(n) for n in node["operands"]
                                 if self.type_at(n) != I32 or self.checker.expression_types.get(id(n)) != INT_LITERAL), I32)
            first = self.expression(node["operands"][0], operand_type)
            previous = self.storage(first.type, initial=first.code)
            result = self.storage(BOOL, initial="true")
            for operator, operand in zip(node["operators"], node["operands"][1:]):
                self.line(f"if ({result.name}) {{")
                self.indent += 1
                self.scopes.append(Scope())
                next_value = self.expression(operand, first.type)
                code = f"xe_str_compare({previous.name}, {next_value.code}) {operator} 0" if first.type == STR else f"{previous.name} {operator} {next_value.code}"
                self.line(f"{result.name} = ({code});")
                self.line(f"{previous.name} = {next_value.code};")
                self.cleanup([self.scopes[-1]])
                self.scopes.pop()
                self.indent -= 1
                self.line("}")
            return self.value(result)
        if kind in {"For", "While"}:
            return self.loop(node)
        if kind == "Unsafe":
            self.fail(node, "unsafe 裸指针操作尚未接入 C 后端")
        self.fail(node, f"C 后端尚未实现 {kind}")

    def enum_value(self, node, type_):
        obj = node["object"]
        if obj["kind"] not in {"Name", "AssociatedAccess"} or not self.variants(type_):
            value = self.expression(obj)
            base = value.type.args[0] if value.type.name == "ptr" else value.type
            if base.name not in {"Array", "Slice", "SliceMut"}:
                self.fail(node, "切片/动态数组索引尚未接入 C 后端")
            if base.args[0].name in self.checker.types:
                # C pointer arithmetic requires the complete element layout.
                self.define_type(base.args[0])
            index = self.argument(self.expression(node["arguments"][0], Type("usize")))
            array = f"*({value.code})" if value.type.name == "ptr" else value.code
            count = base.args[1].name if base.name == "Array" else f"({array}).len"
            self.line(f"if ({index.code} >= {count}) xe_panic(\"array index out of bounds\");")
            items = "items" if base.name == "Array" else "data"
            return CValue(f"({array}).{items}[{index.code}]", type_)
        name = obj["member"] if obj["kind"] == "AssociatedAccess" else obj["path"]["parts"][-1]
        for tag, (variant, types) in enumerate(self.variants(type_)):
            if variant != name:
                continue
            # 全部载荷先成为独立拥有临时值；后续载荷提前返回时也能正确清理。
            values = []
            for argument, payload_type in zip(node["arguments"], types):
                value = self.expression(argument, payload_type)
                if value.type == NEVER:
                    return value
                values.append(self.argument(value))
            result = self.temp(type_, f"({self.ctype(type_)}){{.tag = {tag}}}")
            for index, value in enumerate(values):
                self.transfer(value)
                self.line(f"{self.payload_code(result.code, name, index)} = {value.code};")
            return result
        self.fail(node, "后端找不到枚举变体")

    def unwrap(self, node, type_, propagate=False, message=None, operand=None):
        """Evaluate once, extract only the active payload, and clean up before propagation.

        Panic deliberately terminates without unwinding; ordinary error propagation uses
        the same scope cleanup as an explicit return. The old result shell is moved only
        after ownership of its active payload has been transferred.
        """
        subject = self.argument(operand or self.expression(node["operand"]))
        self.line(f"if (({subject.code}).tag != 0) {{")
        self.indent += 1
        self.scopes.append(Scope())
        if propagate:
            returned = self.storage(self.signature.result, "propagated_error",
                                    f"({self.ctype(self.signature.result)}){{.tag = 1}}")
            if subject.type.args[1] != NONE:
                self.line(f"{self.payload_code(returned.name, 'No', 0)} = {self.payload_code(subject.code, 'No', 0)};")
            self.transfer(subject)
            self.transfer(self.value(returned))
            self.cleanup()
            self.line(f"return {returned.name};")
        else:
            if message is not None:
                self.line(f"xe_io_write(stderr, {message.code});")
            elif subject.type.args[1] != NONE:
                error = subject.type.args[1]
                # Do not invent Display implementations for arbitrary user error types.
                if error.name in NUMERIC | {"str", "String", "bool", "char", "ConversionError", "io::Error"}:
                    self.display(CValue(self.payload_code(subject.code, 'No', 0), error), "stderr", node)
                else:
                    self.line(f"xe_io_write(stderr, {literal_string(str(error))});")
                self.line("xe_io_newline(stderr);")
            self.line('xe_panic("result extraction failed");')
        self.scopes.pop()
        self.indent -= 1
        self.line("}")
        value = self.argument(CValue(self.payload_code(subject.code, "Yes", 0), type_))
        self.transfer(subject)
        return value

    def handle(self, handler, payloads, type_):
        """分支参数绑定是当前函数里的小作用域，不是一个隐式闭包。

        结果先转交到外层存储，再清理绑定和被忽略的资源，因而可以返回拥有值。
        fn 表达式必须显式写 fn；无捕获函数和具名函数使用相同的调用路径。
        """
        if handler["kind"] == "FunctionTarget":
            target_key = getattr(self.checker, "call_targets", {}).get(id(handler))
            if target_key is not None:
                return self.invoke(self.checker.functions[target_key], self.function_name(target_key),
                                   values=[self.argument(value) for value in payloads])
            function = self.callable_argument(self.expression(handler["target"]), handler)
            return self.invoke_function_value(function, handler,
                                              values=[self.argument(value) for value in payloads])

        result = self.storage(UNIT if type_ == NEVER else type_, "handler_result")
        self.line("{")
        self.indent += 1
        self.scopes.append(Scope())
        parameters = handler["parameters"]
        ignore_all = len(parameters) == 1 and parameters[0]["name"] == "_" and parameters[0]["type"] is None
        for index, payload in enumerate(payloads):
            parameter = None if ignore_all else parameters[index]
            bound_type = self.checker.binding_types[id(parameter)] if parameter else payload.type
            if bound_type == INT_LITERAL:
                bound_type = payload.type
            slot = self.storage(bound_type, parameter["name"] if parameter else "ignored")
            self.assign(slot, payload, handler)
            if parameter and parameter["name"] != "_":
                self.scopes[-1].names[parameter["name"]] = slot
        value = self.expression(handler["body"], result.type)
        self.assign(result, value, handler)
        self.cleanup([self.scopes[-1]])
        self.scopes.pop()
        self.indent -= 1
        self.line("}")
        return CValue("0", NEVER) if type_ == NEVER else self.value(result)

    def branch(self, node, type_):
        result = self.storage(UNIT if type_ == NEVER else type_, "branch_result")
        self.line("{")
        self.indent += 1
        self.scopes.append(Scope())
        input_value = self.expression(node["input"])
        borrowed = "borrow" in node["modifiers"]
        mutable = "mut" in node["modifiers"]
        if borrowed:
            base = input_value.type.args[0] if input_value.type.name == "ptr" else input_value.type
            # 保存地址而非枚举位，借用分支可返回原对象内部的载荷指针。
            address = input_value.code if input_value.type.name == "ptr" else f"&({input_value.code})"
            pointer = self.temp(ptr(base, mutable), address)
            subject = CValue(f"*({pointer.code})", base)
        else:
            subject = self.argument(input_value)
            base = subject.type
        variants = self.variants(base)
        wildcard = False
        for index, arm in enumerate(node["arms"]):
            channel = arm["kind"] == "ChannelArm"
            selector = ({"kind": "VariantSelector", "path": {"parts": ["Maybe", variants[arm["channel"] - 1][0]]}}
                        if channel else arm["selector"])
            kind = selector["kind"]
            if kind == "WildcardSelector":
                condition, payloads, wildcard = None, [subject], True
            elif kind == "VariantSelector":
                name = selector["path"]["parts"][-1]
                tag, (_, types) = next((i, v) for i, v in enumerate(variants) if v[0] == name)
                condition = f"({subject.code}).tag == {tag}"
                payloads = [CValue(self.payload_code(subject.code, name, i), t) for i, t in enumerate(types)]
            elif kind == "LiteralSelector":
                literal = selector["value"]
                value = literal["value"]
                if base == STR:
                    condition = f"xe_str_compare({subject.code}, {literal_string(value)}) == 0"
                else:
                    code = "true" if value is True else "false" if value is False else str(ord(value)) if isinstance(value, str) else str(value)
                    condition = f"({subject.code}) == ({code})"
                payloads = [subject]
            else:
                self.fail(selector, "该组合选择模式尚未接入 C 后端")
            prefix = "if" if index == 0 else "else if"
            self.line((f"{prefix} ({condition}) " if condition else "else " if index else "") + "{")
            self.indent += 1
            self.scopes.append(Scope())
            if borrowed:
                payloads = [CValue(f"&({p.code})", ptr(p.type, mutable))
                            for p in payloads]
            elif kind == "VariantSelector" and any(not self.checker.copyable(p.type) for p in payloads):
                # 所有活动载荷现在各自拥有：旧枚举壳不再负责释放这些位。
                payloads = [self.argument(p) for p in payloads]
                self.transfer(subject)
            value = self.handle(arm["handler"], payloads, result.type)
            self.assign(result, value, arm)
            self.cleanup([self.scopes[-1]])
            self.scopes.pop()
            self.indent -= 1
            self.line("}")
        if not wildcard:
            self.line('else { xe_panic("invalid enum tag or unmatched value"); }')
        self.cleanup([self.scopes[-1]])
        self.scopes.pop()
        self.indent -= 1
        self.line("}")
        return CValue("0", NEVER) if type_ == NEVER else self.value(result)

    def invoke(self, signature, name, nodes=None, values=None):
        if signature.generics:
            self.fail(signature.node, "泛型调用实例化尚未接入 C 后端")
        if values is None:
            values = []
            for argument, parameter in zip(nodes, signature.parameters):
                value = self.expression(argument, parameter)
                if value.type == NEVER:
                    return value
                values.append(self.argument(value))
        # 所有实参先按源码顺序求值，再作 C 调用，且普通参数不隐式借用。
        for value in values:
            self.transfer(value)
        return self.temp(signature.result, f"{name}({', '.join(v.code for v in values)})")

    def anonymous_function(self, node, type_):
        """将 fn 提升为普通文件级函数，并在需要时构造捕获环境。

        No C nested-function extension, heap allocation or hidden lexical access is
        required. Bodies are queued, not emitted in the enclosing function, so its
        scopes and early-return destination remain untouched. Nested anonymous
        functions can append more bodies while this queue is being drained.
        """
        if type_.name == "closure":
            # 捕获按普通字段初始化：Copy 复制，资源移动，@ 保存原对象的地址。
            # 环境是普通聚合返回值，不强制分配堆或复制捕获资源。
            slot = self.storage(type_, "closure")
            for capture, (name, capture_type) in zip(node["captures"], self.fields(type_)):
                source = self.lookup(name, capture)
                if capture["borrow"]:
                    source = self.temp(capture_type, f"&({source.code})")
                self.transfer(source)
                self.line(f"{self.field_code(slot.name, (name,))} = {source.code};")
                for path, flag in slot.flags.items():
                    if path and path[0] == name:
                        self.line(f"{flag} = true;")
            self.line(f"{slot.flags[()]} = true;")
            self.initialized(slot)
            self.closure_function(type_, owning=True)
            return self.value(slot)
        if node["captures"] or type_.name != "fn":
            self.fail(node, "匿名函数缺少已验证的具体函数类型")
        key = id(node)
        if key not in self.anonymous_names:
            name = self.fresh("anonymous_function")
            self.anonymous_names[key] = name
            signature = Signature(node, list(type_.args[:-1]), type_.args[-1])
            self.pending_functions.append((name, signature))
        return self.temp(type_, self.anonymous_names[key])

    def closure_function(self, type_, owning):
        """生成隐藏环境参数；共享/可写调用复用同一非拥有实现。

        调用是否获准由语义层根据闭包能力判定。这里只降低已经获准的调用，
        不能给指针调用制造环境副本，否则它会错误地复制或析构捕获资源。
        """
        key = (type_, owning)
        if key not in self.closure_function_names:
            info = self.checker.closures[type_]
            name = self.fresh("closure_once" if owning else "closure_pointer_call")
            self.closure_function_names[key] = name
            environment_type = type_ if owning else ptr(type_, True)
            node = dict(info.node)
            node["parameters"] = [{"name": "$environment"}, *info.node["parameters"]]
            signature = Signature(node, [environment_type, *type_.args[:-1]], type_.args[-1])
            self.pending_closure_functions.append((name, signature, type_, owning))
        return self.closure_function_names[key]

    def callable_argument(self, value, node):
        """先固定 receiver 地址，再按顺序求值实参；不快照共享环境内容。

        普通 f(...) 与方法相同：read/mut 能力用环境指针，once 才移动环境。
        尤其实参可能修改捕获状态，调用必须读同一环境里的最新状态，不能先
        偷复制一份环境。临时闭包仍由创建它的作用域拥有并负责清理。
        """
        mode = getattr(self.checker, "closure_call_modes", {}).get(id(node))
        if value.type.name == "closure" and mode in {"readonly", "mutable"}:
            receiver_type = ptr(value.type, mode == "mutable")
            return self.temp(receiver_type, f"&({value.code})")
        return self.argument(value)

    def invoke_function_value(self, function, node, nodes=None, values=None):
        pointer = function.type.name == "ptr"
        callable_type = function.type.args[0] if pointer else function.type
        if callable_type.name not in {"fn", "closure"}:
            self.fail(node, "此值没有已验证的函数或闭包调用类型")
        if values is None:
            values = []
            for argument, parameter in zip(nodes, callable_type.args[:-1]):
                value = self.expression(argument, parameter)
                if value.type == NEVER:
                    return value
                values.append(self.argument(value))
        for value in values:
            self.transfer(value)
        if callable_type.name == "closure":
            name = self.closure_function(callable_type, owning=not pointer)
            if not pointer:
                self.transfer(function)
            arguments = [function.code, *(value.code for value in values)]
            return self.temp(callable_type.args[-1], f"{name}({', '.join(arguments)})")
        callee = f"*({function.code})" if pointer else function.code
        return self.temp(callable_type.args[-1],
                         f"({callee})({', '.join(value.code for value in values)})")

    def call(self, node):
        callee = node["callee"]
        target_key = getattr(self.checker, "call_targets", {}).get(id(node))
        if target_key is not None:
            signature = self.checker.functions[target_key]
            receiver_node = callee["object"] if callee["kind"] == "BracketApply" else callee
            if receiver_node["kind"] != "FieldAccess":
                return self.invoke(signature, self.function_name(target_key), nodes=node["arguments"])
            # Generic methods are emitted as ordinary concrete free functions.
            # Receiver conversion remains exactly the ordinary method rule;
            # all arguments are evaluated left-to-right before the C call.
            receiver = self.expression(receiver_node["object"])
            if receiver.type == NEVER:
                return receiver
            target = signature.parameters[0]
            if target.name == "ptr" and receiver.type.name != "ptr":
                receiver = CValue(f"&({receiver.code})", target)
            elif target.name != "ptr" and receiver.type.name == "ptr":
                receiver = CValue(f"*({receiver.code})", target)
            arguments = [self.argument(receiver)]
            for argument, parameter in zip(node["arguments"], signature.parameters[1:]):
                value = self.expression(argument, parameter)
                if value.type == NEVER:
                    return value
                arguments.append(self.argument(value))
            return self.invoke(signature, self.function_name(target_key), values=arguments)
        if callee["kind"] == "AssociatedAccess" and callee["object"]["kind"] == "Name":
            # Substitution of T::try_from creates AssociatedAccess. Standard
            # numeric/String/File associated operations use the same ordinary
            # lowering as an explicitly written u8::try_from in source.
            callee = {"kind": "Name", "path": {"parts":
                      callee["object"]["path"]["parts"] + [callee["member"]]}}
        if callee["kind"] == "FieldAccess":
            receiver = self.expression(callee["object"])
            base = receiver.type.args[0] if receiver.type.name == "ptr" else receiver.type
            name = callee["field"]
            if base.name == "FromFn" and name == "next":
                pointer = receiver.code if receiver.type.name == "ptr" else f"&({receiver.code})"
                return self.from_fn_next(CValue(pointer, ptr(base, True)), node)
            if base.name == "maybe" and name == "expect":
                message = self.argument(self.expression(node["arguments"][0], STR))
                if message.type == NEVER:
                    return message
                return self.unwrap(node, base.args[0], message=message, operand=receiver)
            if base == STR and name == "slice_bytes":
                return self.string_slice(node, receiver)
            if base.name == "Array" and name in {"slice", "slice_mut"}:
                return self.array_slice(node, receiver, base)
            signature = self.checker.methods.get((base.name, name))
            if signature:
                params = signature.node["parameters"]
                if not params or params[0]["name"] != "self":
                    self.fail(node, "关联函数必须通过类型名 :: 调用")
                target = signature.parameters[0]
                if target.name == "ptr" and receiver.type.name != "ptr":
                    receiver = CValue(f"&({receiver.code})", target)
                elif target.name != "ptr" and receiver.type.name == "ptr":
                    receiver = CValue(f"*({receiver.code})", target)
                arguments = [self.argument(receiver)] + [self.argument(self.expression(n, t)) for n, t in
                                          zip(node["arguments"], signature.parameters[1:])]
                return self.invoke(signature, self.method_name(base.name, name), values=arguments)
            arguments = []
            for argument in node["arguments"]:
                value = self.expression(argument)
                if value.type == NEVER:
                    return value
                arguments.append(self.argument(value))
            pointer = receiver.code if receiver.type.name == "ptr" else f"&({receiver.code})"
            if base.name in {"Array", "Slice", "SliceMut"} and name == "len":
                # 数组长度来自类型；切片长度来自描述符。都不读取元素，
                # 更不需要取得元素的所有权或可写指针。
                length = base.args[1].name if base.name == "Array" else f"({pointer})->len"
                return self.temp(Type("usize"), length)
            if base.name == "SliceMut" and name == "copy_from":
                if not self.checker.copyable(base.args[0]):
                    self.fail(node, "copy_from 只能复制 Copy 元素；资源元素不能复制所有权")
                self.line(f"if (({pointer})->len != ({arguments[0].code}).len) xe_panic(\"slice lengths differ\");")
                self.line(f"memmove(({pointer})->data, ({arguments[0].code}).data, ({pointer})->len * sizeof *({pointer})->data);")
                return CValue("0", UNIT)
            if base == STRING:
                if name == "len":
                    return self.temp(Type("usize"), f"({pointer})->len")
                if name == "as_str":
                    return self.temp(STR, f"xe_string_view({pointer})")
                if name == "clone":
                    return self.temp(STRING, f"xe_string_from(xe_string_view({pointer}))")
                if name == "push_str":
                    self.line(f"xe_string_push({pointer}, {arguments[0].code});")
                    return CValue("0", UNIT)
            if base.name == "File":
                if name == "size":
                    return self.temp(Type("usize"), f"xe_file_size({pointer})")
                if name == "read_to_string":
                    result_type = self.type_at(node)
                    result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
                    error = self.fresh("io_error")
                    self.line(f"int {error} = 0;")
                    self.line(f"{self.payload_code(result.code, 'Yes', 0)} = xe_file_read({pointer}, &{error});")
                    self.line(f"({result.code}).tag = {error} ? 1 : 0;")
                    self.line(f"{self.payload_code(result.code, 'No', 0)} = {error};")
                    return result
            if base == STR:
                view = f"*({receiver.code})" if receiver.type.name == "ptr" else receiver.code
                if name == "len":
                    return self.temp(Type("usize"), f"({view}).len")
                if name == "data":
                    return self.temp(ptr(Type("u8")), f"(uint8_t *)({view}).data")
                if name == "byte_at":
                    self.line(f"if ({arguments[0].code} >= ({view}).len) xe_panic(\"string index out of bounds\");")
                    return self.temp(Type("u8"), f"({view}).data[{arguments[0].code}]")
            self.fail(node, f"方法 {base}::{name} 尚未接入 C 后端")
        if callee["kind"] != "Name":
            function = self.callable_argument(self.expression(callee), node)
            return self.invoke_function_value(function, node, nodes=node["arguments"])
        name = "::".join(callee["path"]["parts"])
        if any(name in scope.names for scope in self.scopes):
            function = self.callable_argument(self.expression(callee), node)
            return self.invoke_function_value(function, node, nodes=node["arguments"])
        if name in self.checker.functions:
            return self.invoke(self.checker.functions[name], self.function_name(name), nodes=node["arguments"])
        if "::" in name:
            owner, method = name.rsplit("::", 1)
            if owner in self.checker.aliases:
                owner = self.checker.associated_owner(owner, node).name
                name = owner + "::" + method
            signature = self.checker.methods.get((owner, method))
            if signature:
                return self.invoke(signature, self.method_name(owner, method), nodes=node["arguments"])
        name = normalize_io_name(name)
        if name == FROM_FN:
            callback = self.argument(self.expression(node["arguments"][0]))
            if callback.type == NEVER:
                return callback
            result_type = self.type_at(node)
            self.transfer(callback)
            return self.temp(result_type, f"({self.ctype(result_type)}){{"
                f".{identifier('callback')} = {callback.code}, .{identifier('done')} = false}}")
        if name in {"print", "println", "eprintln"}:
            return self.format_output(name, node)
        if name == "readline":
            return self.readline(node)
        if name in IO_NATIVE_FUNCTIONS:
            return self.temp(self.type_at(node), IO_NATIVE_FUNCTIONS[name] + "()")
        if env_function(name) is not None:
            result_type = self.type_at(node)
            return self.temp(result_type, self.args_function(result_type) + "()")
        if name == "String::from":
            value = self.expression(node["arguments"][0], STR)
            if value.type == NEVER:
                return value
            return self.temp(STRING, f"xe_string_from({value.code})")
        if name == "File::open":
            path = self.argument(self.expression(node["arguments"][0], STR))
            if path.type == NEVER:
                return path
            result_type = self.type_at(node)
            result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
            error = self.fresh("io_error")
            self.line(f"int {error} = 0;")
            self.line(f"{self.payload_code(result.code, 'Yes', 0)} = xe_file_open({path.code}, &{error});")
            self.line(f"({result.code}).tag = {error} ? 1 : 0;")
            self.line(f"{self.payload_code(result.code, 'No', 0)} = {error};")
            return result
        if name.endswith("::try_from") and name.split("::")[0] in NUMERIC:
            return self.try_integer_conversion(node)
        if name == "panic":
            value = self.expression(node["arguments"][0], STR)
            if value.type == NEVER:
                return value
            self.line(f"xe_io_write(stderr, {value.code});")
            self.line('xe_panic("explicit panic");')
            return CValue("0", NEVER)
        self.fail(node, f"调用 {name} 尚未接入 C 后端")

    def from_fn_next(self, pointer, node):
        """回调借用自己的环境运行；第一次 None 以后不再调用用户代码。

        就地展开而非另建 ABI：闭包具体类型、普通函数与函数指针均沿用
        invoke_function_value 的一套调用规则，不增加隐式装箱。
        """
        iterator = pointer.type.args[0]
        result_type = next_result(iterator)
        result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
        done = f"({pointer.code})->{identifier('done')}"
        callback = f"({pointer.code})->{identifier('callback')}"
        callback_t = callback_type(iterator)
        function = (CValue(f"&({callback})", ptr(callback_t, True))
                    if callback_t.name == "closure" else CValue(callback, callback_t))
        self.line(f"if (!({done})) {{")
        self.indent += 1
        # C 的条件块也是临时资源作用域。不能把只在块内声明的
        # produced slot 留在外层清理列表，否则循环退出时会引用不存在的名字。
        self.scopes.append(Scope())
        produced = self.invoke_function_value(function, node, values=[])
        self.assign(result.slot, produced, node)
        self.line(f"if (({result.code}).tag == 1) {done} = true;")
        self.cleanup([self.scopes[-1]])
        self.scopes.pop()
        self.indent -= 1
        self.line("}")
        return result

    def readline(self, node):
        type_ = self.type_at(node)
        return self.temp(type_, self.readline_function(type_) + "()")

    def readline_function(self, type_):
        """Direct calls and fn values share the same ordinary Maybe ABI wrapper."""
        name = identifier("stdlib_io_readline")
        if type_ not in self.io_function_definitions:
            ctype = self.ctype(type_)
            optional = self.payload_code("result", "Yes", 0)
            error = self.payload_code("result", "No", 0)
            text = self.payload_code(optional, "Yes", 0)
            inner = self.ctype(type_.args[0])
            self.io_function_definitions[type_] = (
                f"static {ctype} {name}(void) {{\n"
                "    XeIoReadline raw = xe_io_readline(stdin);\n"
                f"    {ctype} result = {{.tag = 1}};\n"
                f"    if (raw.error) {{ {error} = raw.error; }}\n"
                "    else {\n"
                "        result.tag = 0;\n"
                f"        {optional} = ({inner}){{.tag = raw.has_line ? 0 : 1}};\n"
                f"        if (raw.has_line) {text} = raw.text;\n"
                "    }\n"
                "    return result;\n"
                "}")
        return name

    def args_function(self, type_):
        """参数读取的直接调用与 fn 值共用一个普通 Maybe/Slice ABI 桥接。"""
        name = identifier("stdlib_env_args")
        if type_ not in self.env_function_definitions:
            ctype = self.ctype(type_)
            payload = self.payload_code("result", "Yes", 0)
            error = self.payload_code("result", "No", 0)
            slice_type = self.ctype(type_.args[0])
            self.env_function_definitions[type_] = (
                f"static {ctype} {name}(void) {{\n"
                "    XeEnvArgs raw = xe_env_args();\n"
                f"    {ctype} result = {{.tag = 1}};\n"
                f"    if (raw.error) {{ {error} = raw.error; }}\n"
                "    else {\n"
                "        result.tag = 0;\n"
                f"        {payload} = ({slice_type}){{.data = raw.data, .len = raw.len}};\n"
                "    }\n"
                "    return result;\n"
                "}")
        return name

    def string_slice(self, node, receiver):
        """Ranges are evaluated explicitly; both bounds and UTF-8 boundaries are checked."""
        range_ = node["arguments"][0]
        if range_["kind"] != "Range" or range_["operator"] != "..":
            self.fail(range_, "slice_bytes 当前需要右端不包含的范围表达式")
        view = f"*({receiver.code})" if receiver.type.name == "ptr" else receiver.code
        lower = self.argument(self.expression(range_["lower"], Type("usize"))) if range_["lower"] else CValue("0", Type("usize"))
        upper = self.argument(self.expression(range_["upper"], Type("usize"))) if range_["upper"] else CValue(f"({view}).len", Type("usize"))
        result_type = self.type_at(node)
        result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
        self.line(f"if (xe_str_slice_valid({view}, {lower.code}, {upper.code})) {{")
        self.indent += 1
        self.line(f"({result.code}).tag = 0;")
        self.line(f"{self.payload_code(result.code, 'Yes', 0)} = (XeStr){{({view}).data + {lower.code}, {upper.code} - {lower.code}}};")
        self.indent -= 1
        self.line("}")
        return result

    def array_slice(self, node, receiver, base):
        range_ = node["arguments"][0]
        if range_["kind"] != "Range" or range_["operator"] != "..":
            self.fail(range_, "数组切片当前需要右端不包含的范围表达式")
        lower = self.argument(self.expression(range_["lower"], Type("usize"))) if range_["lower"] else CValue("0", Type("usize"))
        upper = self.argument(self.expression(range_["upper"], Type("usize"))) if range_["upper"] else CValue(base.args[1].name, Type("usize"))
        self.line(f"if ({lower.code} > {upper.code} || {upper.code} > {base.args[1].name}) xe_panic(\"slice range out of bounds\");")
        array = f"*({receiver.code})" if receiver.type.name == "ptr" else receiver.code
        result = self.type_at(node)
        return self.temp(result, f"({self.ctype(result)}){{({array}).items + {lower.code}, {upper.code} - {lower.code}}}")

    def try_integer_conversion(self, node):
        """No cast happens before the signedness-aware range check succeeds."""
        result_type = self.type_at(node)
        target = result_type.args[0]
        value = self.argument(self.expression(node["arguments"][0]))
        if value.type == NEVER:
            return value
        if value.type.name in {"f32", "f64"} or target.name in {"f32", "f64"}:
            self.fail(node, "浮点失败转换尚未确定语言规则")
        name = target.name
        max_ = "SIZE_MAX" if name == "usize" else "INTPTR_MAX" if name == "isize" else f"{'U' if name[0] == 'u' else ''}INT{name[1:]}_MAX"
        signed_source = value.type.name.startswith("i")
        signed_target = name.startswith("i")
        if signed_target:
            min_ = "INTPTR_MIN" if name == "isize" else f"INT{name[1:]}_MIN"
            valid = (f"(intmax_t)({value.code}) >= {min_} && (intmax_t)({value.code}) <= {max_}" if signed_source
                     else f"(uintmax_t)({value.code}) <= (uintmax_t){max_}")
        else:
            valid = (f"({value.code}) >= 0 && " if signed_source else "") + f"(uintmax_t)({value.code}) <= {max_}"
        result = self.temp(result_type, f"({self.ctype(result_type)}){{.tag = 1}}")
        self.line(f"if ({valid}) {{")
        self.indent += 1
        self.line(f"({result.code}).tag = 0;")
        self.line(f"{self.payload_code(result.code, 'Yes', 0)} = ({self.ctype(target)})({value.code});")
        self.indent -= 1
        self.line("}")
        return result

    def display(self, value, stream, node):
        code, type_ = value.code, value.type
        if type_.name == "ptr":
            code, type_ = f"*({code})", type_.args[0]
        if type_ == STR:
            self.line(f"xe_io_write({stream}, {code});")
        elif type_ == STRING:
            self.line(f"xe_io_write({stream}, xe_string_view(&({code})));")
        elif type_ == BOOL:
            self.line(f"xe_io_print_bool({stream}, {code});")
        elif type_.name == "char":
            self.line(f"xe_io_print_char({stream}, {code});")
        elif type_.name == "ConversionError":
            self.line(f"xe_io_write({stream}, {literal_string('integer conversion out of range')});")
        elif type_.name == "io::Error":
            self.line(f"xe_io_print_error({stream}, {code});")
        elif type_.name in NUMERIC | {"$integer"}:
            function = "float" if type_.name in {"f32", "f64"} else "u64" if type_.name.startswith("u") else "i64"
            self.line(f"xe_io_print_{function}({stream}, {code});")
        else:
            self.fail(node, f"{type_} 的格式化尚未接入 C 后端")

    def format_output(self, name, node):
        template = node["arguments"][0]
        if template["kind"] != "Literal":
            self.fail(node, "格式字符串必须是字面量")
        values = []
        for argument in node["arguments"][1:]:
            value = self.expression(argument)
            if value.type == NEVER:
                return value
            values.append(self.argument(value))
        stream = "stderr" if name == "eprintln" else "stdout"
        index = 0
        for text, field, spec, conversion in string.Formatter().parse(template["value"]):
            if text:
                self.line(f"xe_io_write({stream}, {literal_string(text)});")
            if field is None:
                continue
            if field != "" or conversion or spec not in {"", "p"}:
                self.fail(template, "首版格式化只支持 {} 与 {:p}")
            value = values[index]
            index += 1
            code, type_ = value.code, value.type
            if spec == "p":
                if type_.name != "ptr":
                    self.fail(node, "{:p} 需要指针")
                self.line(f"xe_io_print_pointer({stream}, (const void *)({code}));")
                continue
            self.display(value, stream, node)
        if name != "print":
            self.line(f"xe_io_newline({stream});")
        # 格式化遵守按值传参。拥有 String 的打印完成后即析构，而非默认借用。
        for value in values:
            if not self.checker.copyable(value.type) and value.type.name != "ptr":
                if value.type.name == "io::Error":
                    self.transfer(value)
                    continue
                if value.type != STRING:
                    self.fail(node, "拥有值的格式化清理尚未实现")
                self.transfer(value)
                self.line(f"xe_string_drop(&({value.code}));")
        return CValue("0", UNIT)

    def loop(self, node):
        if node["kind"] == "While":
            self.line("while (true) {")
            self.indent += 1
            self.scopes.append(Scope())
            condition = self.expression(node["condition"], BOOL)
            self.line(f"if (!({condition.code})) {{")
            self.indent += 1
            self.cleanup([self.scopes[-1]])
            self.line("break;")
            self.indent -= 1
            self.line("}")
            self.loop_scopes.append(len(self.scopes)-1)
            self.block(node["body"])
            self.cleanup([self.scopes[-1]])
            self.loop_scopes.pop()
            self.scopes.pop()
        else:
            source = node["source"]
            source_type = self.type_at(source)
            if source_type == NEVER:
                return self.expression(source)
            base = source_type.args[0] if source_type.name == "ptr" else source_type
            if base.name in {"Array", "Slice", "SliceMut"}:
                return self.array_loop(node, base)
            if id(node) in self.checker.for_iterators:
                return self.iterator_loop(node, base)
            if base.name != "Range":
                self.fail(node, "for 后端当前支持整数范围、数组和切片")
            type_ = self.checker.binding_types[id(node)]
            interval = self.argument(self.expression(source))
            self.line(f"if (!({interval.code}).has_lower || !({interval.code}).has_upper) xe_panic(\"iteration needs both range endpoints\");")
            lower, upper = f"({interval.code}).lower", f"({interval.code}).upper"
            closed = f"({interval.code}).closed"
            name = self.fresh(node["name"])
            finished = self.fresh("range_finished")
            self.line(f"bool {finished} = false;")
            # The final closed endpoint must not be incremented (it can be TYPE_MAX).
            # A for increment also executes on continue, unlike a body-end guard.
            condition = f"!{finished} && ({closed} ? {name} <= {upper} : {name} < {upper})"
            increment = f"{closed} && {name} == {upper} ? ({finished} = true, {name}) : xe_add_{type_.name}({name}, 1)"
            self.line(f"for ({self.ctype(type_)} {name} = {lower}; {condition}; {name} = {increment}) {{")
            self.indent += 1
            scope = Scope()
            scope.names[node["name"]] = Slot(name, type_)
            self.scopes.append(scope)
            self.loop_scopes.append(len(self.scopes)-1)
            self.block(node["body"])
            self.cleanup([self.scopes[-1]])
            self.loop_scopes.pop()
            self.scopes.pop()
        self.indent -= 1
        self.line("}")
        return CValue("0", UNIT)

    def iterator_loop(self, node, base):
        """拥有源只移动一次，循环元素各自拥有；任何退出路径按作用域 Drop。

        外层 scope 保存整个迭代器，内层 scope 保存本轮 next 结果和元素。
        continue/break 只清理本轮；return 会清理两层，避免遗漏环境资源。
        """
        self.line("{")
        self.indent += 1
        self.scopes.append(Scope())
        source = self.argument(self.expression(node["source"]))
        pointer = source if source.type.name == "ptr" else CValue(f"&({source.code})", ptr(base, True))
        self.line("while (true) {")
        self.indent += 1
        self.scopes.append(Scope())
        self.loop_scopes.append(len(self.scopes)-1)
        signature = self.checker.for_iterators[id(node)]
        if signature is None:
            produced = self.from_fn_next(pointer, node)
        else:
            target = self.checker.call_targets.get(id(node))
            name = self.function_name(target) if target else self.method_name(base.name, "next")
            produced = self.invoke(signature, name, values=[pointer])
        self.line(f"if (({produced.code}).tag == 1) {{")
        self.indent += 1
        self.cleanup([self.scopes[-1]])
        self.line("break;")
        self.indent -= 1
        self.line("}")
        element_type = self.checker.binding_types[id(node)]
        slot = self.storage(element_type, node["name"])
        payload = CValue(self.payload_code(produced.code, "Yes", 0), produced.type.args[0])
        self.assign(slot, payload, node)
        self.transfer(produced)
        self.scopes[-1].names[node["name"]] = slot
        self.block(node["body"])
        self.cleanup([self.scopes[-1]])
        self.loop_scopes.pop()
        self.scopes.pop()
        self.indent -= 1
        self.line("}")
        self.cleanup([self.scopes[-1]])
        self.scopes.pop()
        self.indent -= 1
        self.line("}")
        return CValue("0", UNIT)

    def array_loop(self, node, base):
        array = self.expression(node["source"])
        address = array.code if array.type.name == "ptr" else f"&({array.code})"
        pointer = self.temp(ptr(base), address)
        index, name = self.fresh("array_index"), self.fresh(node["name"])
        count = base.args[1].name if base.name == "Array" else f"({pointer.code})->len"
        items = "items" if base.name == "Array" else "data"
        self.line(f"for (size_t {index} = 0; {index} < {count}; ++{index}) {{")
        self.indent += 1
        scope = Scope()
        element_type = self.checker.binding_types[id(node)]
        self.line(f"{self.ctype(element_type)} {name} = &(({pointer.code})->{items}[{index}]);")
        scope.names[node["name"]] = Slot(name, element_type)
        self.scopes.append(scope)
        self.loop_scopes.append(len(self.scopes)-1)
        self.block(node["body"])
        self.cleanup([self.scopes[-1]])
        self.loop_scopes.pop()
        self.scopes.pop()
        self.indent -= 1
        self.line("}")
        return CValue("0", UNIT)

    def generate(self):
        for item in self.tree["items"]:
            if item["kind"] == "Constant":
                value = item["value"]
                if value["kind"] != "Literal" and not (value["kind"] == "Unary" and value["operand"]["kind"] == "Literal"):
                    self.fail(item, "首版后端的 const 只支持字面量及其一元正负号，不重复执行常量初始化函数")
        for name, declaration in self.checker.types.items():
            if not declaration.get("generics") and declaration["kind"] in {"Struct", "Enum"}:
                self.define_type(name)
        # Templates are checked by the front end but only concrete instances
        # reach C. Even an unused generic template must not block an executable.
        signatures = [(self.function_name(n), s) for n, s in self.checker.functions.items() if not s.generics]
        signatures += [(self.method_name(owner, n), s) for (owner, n), s in self.checker.methods.items()
                       if not s.generics]
        prototypes = [self.prototype(s, n) + ";" for n, s in signatures]
        main = self.checker.functions.get("main")
        if not main or main.parameters or main.generics or main.result not in {UNIT, I32}:
            self.fail(self.tree, "可执行入口必须为 fn main() 或 fn main() -> i32")
        for name, signature in signatures:
            self.emit_function(signature, name)
        pending_index = closure_index = 0
        # 闭包体可以再构造闭包或无捕获函数，两种队列都要排空。
        while (pending_index < len(self.pending_functions)
               or closure_index < len(self.pending_closure_functions)):
            if pending_index < len(self.pending_functions):
                name, signature = self.pending_functions[pending_index]
                pending_index += 1
                context = (None, True)
            else:
                name, signature, type_, owning = self.pending_closure_functions[closure_index]
                closure_index += 1
                context = (type_, owning)
            prototypes.append(self.prototype(signature, name) + ";")
            self.emit_function(signature, name, *context)
        # 原生入口保留操作系统拆好的参数，Xe main 仍是用户熟悉的无参函数。
        # Xe main 自己先完成 Drop（包括提前 return），之后才释放参数描述符表，
        # 因此 Drop 内仍能读取参数视图。最后刷新两个输出流并保留退出码。
        # C main/exit 默认刷新输出，但不会把刷新失败报告为失败退出。
        self.line("int main(int argc, char **argv) {")
        self.indent += 1
        self.line("xe_env_init(argc, argv);")
        if main.result == UNIT:
            self.line(f"(void){self.function_name('main')}();")
            self.line("int exit_code = 0;")
        else:
            self.line(f"int exit_code = (int){self.function_name('main')}();")
        self.line("xe_env_cleanup();")
        self.line("xe_io_flush(stdout);")
        self.line("xe_io_flush(stderr);")
        self.line("return exit_code;")
        self.indent -= 1
        self.line("}")
        compiler_root = Path(__file__).resolve().parents[1]
        runtime = (compiler_root / "runtime/xe_runtime.h").read_text(encoding="utf-8")
        io_library = (compiler_root.parent / "stdlib/io/xe_io.h").read_text(encoding="utf-8")
        env_library = (compiler_root.parent / "stdlib/env/xe_env.h").read_text(encoding="utf-8")
        # emit-C remains self-contained even when compiled from another folder.
        runtime = runtime.replace('#include "../../stdlib/io/xe_io.h"', io_library)
        runtime = runtime.replace('#include "../../stdlib/env/xe_env.h"', env_library)
        # Opaque pointer targets remain forward-only. Do not force completion
        # merely because a type name was needed in a pointer declaration.
        forwards = [f"typedef struct {cname} {cname};" for cname in self.user_layout_names.values()]
        for name in self.definitions:
            layout = self.layout_types[name] if name in self.layout_types else Type(name)
            if layout.name in self.checker.types:
                continue
            cname = self.ctype(layout)
            forwards.append(f"typedef struct {cname} {cname};")
        return ("/* Generated by Xe bootstrap C backend. Do not edit. */\n" + runtime + "\n" +
                "\n".join(forwards) + "\n" + "\n".join(self.function_typedefs.values()) + "\n" +
                "\n\n".join(self.definitions.values()) + "\n\n" +
                "\n\n".join(self.io_function_definitions.values()) + "\n\n" +
                "\n\n".join(self.env_function_definitions.values()) + "\n\n" +
                "\n".join(prototypes) + "\n\n" + "\n".join(self.lines) + "\n")


def lower_to_c(text, filename="<input>", check_borrows=True, warnings=None):
    source = Source(text, filename)
    tree = parse_source(text, filename)
    checker = AnnotatedChecker(source, tree, check_borrows)
    diagnostics = checker.check()
    if diagnostics:
        raise diagnostics[0]
    if warnings is not None:
        warnings.extend(checker.warnings)
    return CBackend(checker).generate()
