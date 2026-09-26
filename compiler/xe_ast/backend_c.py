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
from .semantic import Checker
from .source import Source
from .typesys import Type, UNIT, NEVER, BOOL, STR, STRING, INT_LITERAL, I32, NUMERIC, ptr


class AnnotatedChecker(Checker):
    """记录侧表，不改变冻结的 AST JSON。id(node) 在本次编译期间稳定。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expression_types, self.binding_types = {}, {}

    def infer(self, node, expected=None, lift=False):
        value = super().infer(node, expected, lift)
        self.expression_types[id(node)] = value.type
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
            return expected if expected and expected.name in NUMERIC else I32
        return type_

    def ctype(self, type_, at=None):
        at = at or self.tree
        primitives = {f"{s}{b}": f"{'int' if s == 'i' else 'uint'}{b}_t"
                      for s in ("i", "u") for b in (8, 16, 32, 64)}
        primitives.update({"bool": "bool", "char": "uint32_t", "str": "XeStr",
                           "String": "XeString", "Unit": "XeUnit", "Never": "XeUnit",
                           "usize": "size_t", "isize": "intptr_t", "f32": "float", "f64": "double"})
        if type_.name in primitives:
            return primitives[type_.name]
        if type_.name == "ptr":
            return self.ctype(type_.args[0], at) + " *"
        if type_.name == "RawPtr":
            return self.ctype(type_.args[0], at) + " *"
        if type_.name in self.checker.types and not type_.args:
            node = self.checker.types[type_.name]
            if node["kind"] not in {"Struct", "Enum"} or node.get("generics"):
                self.fail(at, "C 后端目前只支持非泛型结构体和枚举布局")
            return identifier("type_" + type_.name)
        self.fail(at, f"C 后端尚不支持类型 {type_}")

    def fields(self, type_):
        node = self.checker.types.get(type_.name, {})
        return [(f["name"], self.checker.type_of(f["type"], set(), type_)) for f in node.get("fields", [])]

    def define_type(self, name):
        if name in self.definitions:
            return
        node = self.checker.types[name]
        if node["kind"] not in {"Struct", "Enum"}:
            self.fail(node, "通用 Trait 布局尚不支持")
        if node.get("generics"):
            self.fail(node, "泛型数据布局尚未接入 C 后端")
        if name in self.defining:
            self.fail(node, "类型包含按值递归字段，必须通过指针或拥有容器间接连接")
        self.defining.add(name)
        fields = self.fields(Type(name))
        payloads = [(v["name"], [self.checker.type_of(t, set()) for t in v["payload"]])
                    for v in node.get("variants", [])]
        for type_ in [t for _, t in fields] + [t for _, ts in payloads for t in ts]:
            self.ctype(type_, node)
            if type_.name in self.checker.types:
                self.define_type(type_.name)
        cname = self.ctype(Type(name))
        members = [f"    {self.ctype(t)} {identifier(f)};" for f, t in fields]
        if node["kind"] == "Enum":
            members = ["    int tag;"]
            for variant, types in payloads:
                for index, type_ in enumerate(types):
                    members.append(f"    {self.ctype(type_)} {identifier(variant+'_'+str(index))};")
        if not members:
            members = ["    unsigned char empty; /* C 不允许空结构体 */"]
        self.definitions[name] = "struct " + cname + " {\n" + "\n".join(members) + "\n};"
        self.defining.remove(name)

    def resource_paths(self, type_, path=()):
        if self.checker.copyable(type_) or type_.name in {"ptr", "RawPtr"}:
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
            code = f"({code}).{identifier(name)}"
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
        if value.type != slot.type:
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
            elif type_.name in self.checker.drop_types:
                self.line(f"if ({flag}) {self.method_name(type_.name, 'drop')}(&({code}));")
            elif self.checker.types.get(type_.name, {}).get("kind") == "Enum":
                declaration = self.checker.types[type_.name]
                if any(not self.checker.copyable(self.checker.type_of(t, set()))
                       for v in declaration["variants"] for t in v["payload"]):
                    self.fail(declaration, "拥有资源载荷的枚举清理尚未实现")
            self.line(f"{flag} = false;")
        if slot.order:
            self.line(f"{slot.order} = 0;")

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

    def emit_function(self, signature, name):
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
            slot = self.storage(type_, node["name"])
            if value:
                self.assign(slot, value, node)
            self.scopes[-1].names[node["name"]] = slot
        elif kind == "Assignment":
            left = node["right"] if node["operator"] == ">>" else node["left"]
            right = node["left"] if node["operator"] == ">>" else node["right"]
            target = self.expression(left)
            address = self.temp(ptr(target.type), f"&({target.code})")
            target = CValue(f"*({address.code})", target.type, target.slot, target.path)
            value = self.expression(right, target.type)
            # 替换拥有值时先析构旧值，指针原位替换也必须释放旧资源。
            if not self.checker.copyable(target.type):
                if target.slot and not target.path:
                    self.cleanup_slot(target.slot)
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
            if value.slot and not self.checker.copyable(value.type):
                self.cleanup_slot(value.slot)
        return False

    def expression(self, node, expected=None):
        kind = node["kind"]
        type_ = self.type_at(node, expected)
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
            if len(parts) == 2 and parts[0] in self.checker.types:
                definition = self.checker.types[parts[0]]
                for index, variant in enumerate(definition.get("variants", [])):
                    if variant["name"] == parts[1]:
                        return self.temp(type_, f"({self.ctype(type_)}){{.tag = {index}}}")
            if name in self.checker.constants:
                return self.expression(self.checker.constants[name].node, type_)
            return self.lookup(name, node)
        if kind == "Group":
            return self.expression(node["expression"], expected)
        if kind == "Borrow":
            value = self.expression(node["operand"])
            return self.temp(type_, f"&({value.code})")
        if kind == "Dereference":
            value = self.expression(node["operand"])
            return CValue(f"*({value.code})", type_)
        if kind == "FieldAccess":
            value = self.expression(node["object"])
            operator = "->" if value.type.name == "ptr" else "."
            return CValue(f"({value.code}){operator}{identifier(node['field'])}", type_,
                          value.slot if operator == "." else None, value.path + (node["field"],))
        if kind == "StructLiteral":
            slot = self.storage(type_)
            fields = dict(self.fields(type_))
            if not node["fields"]:
                self.line(f"{slot.name}.empty = 0;")
            for field in node["fields"]:
                value = self.expression(field["value"], fields[field["name"]])
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

    def invoke(self, signature, name, nodes=None, values=None):
        if signature.generics:
            self.fail(signature.node, "泛型调用实例化尚未接入 C 后端")
        values = values if values is not None else [
            self.argument(self.expression(n, t)) for n, t in zip(nodes, signature.parameters)]
        # 所有实参先按源码顺序求值，再作 C 调用，且普通参数不隐式借用。
        for value in values:
            self.transfer(value)
        return self.temp(signature.result, f"{name}({', '.join(v.code for v in values)})")

    def call(self, node):
        callee = node["callee"]
        if callee["kind"] == "FieldAccess":
            receiver = self.expression(callee["object"])
            base = receiver.type.args[0] if receiver.type.name == "ptr" else receiver.type
            name = callee["field"]
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
            arguments = [self.argument(self.expression(n)) for n in node["arguments"]]
            pointer = receiver.code if receiver.type.name == "ptr" else f"&({receiver.code})"
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
            if base == STR:
                view = f"*({receiver.code})" if receiver.type.name == "ptr" else receiver.code
                if name == "len":
                    return self.temp(Type("usize"), f"({view}).len")
                if name == "data":
                    return self.temp(Type("RawPtr", (Type("u8"),)), f"(uint8_t *)({view}).data")
                if name == "byte_at":
                    self.line(f"if ({arguments[0].code} >= ({view}).len) xe_panic(\"string index out of bounds\");")
                    return self.temp(Type("u8"), f"({view}).data[{arguments[0].code}]")
            self.fail(node, f"方法 {base}::{name} 尚未接入 C 后端")
        if callee["kind"] != "Name":
            self.fail(node, "函数值或闭包调用尚未接入 C 后端")
        name = "::".join(callee["path"]["parts"])
        if name in self.checker.functions:
            return self.invoke(self.checker.functions[name], self.function_name(name), nodes=node["arguments"])
        if "::" in name:
            owner, method = name.rsplit("::", 1)
            signature = self.checker.methods.get((owner, method))
            if signature:
                return self.invoke(signature, self.method_name(owner, method), nodes=node["arguments"])
        if name in {"print", "println", "eprintln"}:
            return self.format_output(name, node)
        if name == "String::from":
            value = self.expression(node["arguments"][0], STR)
            return self.temp(STRING, f"xe_string_from({value.code})")
        if name == "panic":
            value = self.expression(node["arguments"][0], STR)
            self.line(f"xe_write(stderr, {value.code});")
            self.line('xe_panic("explicit panic");')
            return CValue("0", NEVER)
        self.fail(node, f"调用 {name} 尚未接入 C 后端")

    def format_output(self, name, node):
        template = node["arguments"][0]
        if template["kind"] != "Literal":
            self.fail(node, "格式字符串必须是字面量")
        values = [self.argument(self.expression(n)) for n in node["arguments"][1:]]
        stream = "stderr" if name == "eprintln" else "stdout"
        index = 0
        for text, field, spec, conversion in string.Formatter().parse(template["value"]):
            if text:
                self.line(f"xe_write({stream}, {literal_string(text)});")
            if field is None:
                continue
            if field != "" or conversion or spec not in {"", "p"}:
                self.fail(template, "首版格式化只支持 {} 与 {:p}")
            value = values[index]
            index += 1
            code, type_ = value.code, value.type
            if spec == "p":
                if type_.name not in {"ptr", "RawPtr"}:
                    self.fail(node, "{:p} 需要指针")
                self.line(f"xe_print_pointer({stream}, (const void *)({code}));")
                continue
            if type_.name == "ptr":
                code, type_ = f"*({code})", type_.args[0]
            if type_ == STR:
                self.line(f"xe_write({stream}, {code});")
            elif type_ == STRING:
                self.line(f"xe_write({stream}, xe_string_view(&({code})));")
            elif type_ == BOOL:
                self.line(f"xe_print_bool({stream}, {code});")
            elif type_.name == "char":
                self.line(f"xe_print_char({stream}, {code});")
            elif type_.name in NUMERIC | {"$integer"}:
                function = "float" if type_.name in {"f32", "f64"} else "u64" if type_.name.startswith("u") else "i64"
                self.line(f"xe_print_{function}({stream}, {code});")
            else:
                self.fail(node, f"{type_} 的格式化尚未接入 C 后端")
        if name != "print":
            self.line(f"fputc('\\n', {stream});")
        # 格式化遵守按值传参。拥有 String 的打印完成后即析构，而非默认借用。
        for value in values:
            if not self.checker.copyable(value.type) and value.type.name != "ptr":
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
            if source["kind"] != "Range" or source["lower"] is None or source["upper"] is None:
                self.fail(node, "首版 for 后端只支持完整的整数范围")
            type_ = self.checker.binding_types[id(node)]
            lower = self.expression(source["lower"], type_)
            upper = self.expression(source["upper"], type_)
            if source["operator"] != "..":
                self.fail(node, "闭区间循环的溢出边界尚未实现")
            name = self.fresh(node["name"])
            self.line(f"for ({self.ctype(type_)} {name} = {lower.code}; {name} < {upper.code}; {name} = xe_add_{type_.name}({name}, 1)) {{")
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

    def generate(self):
        for item in self.tree["items"]:
            if item["kind"] == "Constant":
                value = item["value"]
                if value["kind"] != "Literal" and not (value["kind"] == "Unary" and value["operand"]["kind"] == "Literal"):
                    self.fail(item, "首版后端的 const 只支持字面量及其一元正负号，不重复执行常量初始化函数")
        for name in self.checker.types:
            self.define_type(name)
        signatures = [(self.function_name(n), s) for n, s in self.checker.functions.items()]
        signatures += [(self.method_name(owner, n), s) for (owner, n), s in self.checker.methods.items()]
        prototypes = [self.prototype(s, n) + ";" for n, s in signatures]
        main = self.checker.functions.get("main")
        if not main or main.parameters or main.generics or main.result not in {UNIT, I32}:
            self.fail(self.tree, "可执行入口必须为 fn main() 或 fn main() -> i32")
        for name, signature in signatures:
            self.emit_function(signature, name)
        self.line(f"int main(void) {{ {'(void)' if main.result == UNIT else 'return (int)'}{self.function_name('main')}();"
                  + (" return 0; }" if main.result == UNIT else " }"))
        runtime = (Path(__file__).resolve().parents[1] / "runtime/xe_runtime.h").read_text(encoding="utf-8")
        forwards = [f"typedef struct {self.ctype(Type(name))} {self.ctype(Type(name))};" for name in self.definitions]
        return ("/* Generated by Xe bootstrap C backend. Do not edit. */\n" + runtime + "\n" +
                "\n".join(forwards) + "\n" + "\n\n".join(self.definitions.values()) + "\n\n" +
                "\n".join(prototypes) + "\n\n" + "\n".join(self.lines) + "\n")


def lower_to_c(text, filename="<input>", check_borrows=False):
    source = Source(text, filename)
    tree = parse_source(text, filename)
    checker = AnnotatedChecker(source, tree, check_borrows)
    diagnostics = checker.check()
    if diagnostics:
        raise diagnostics[0]
    return CBackend(checker).generate()
