"""模块级 Copy 存储与 C 静态初始化。

全局拥有进程生存期，不进入任何函数的作用域/Drop 队列。初始化器仅
使用 C 的静态值和地址，不能借用普通表达式降低来偷偷执行初始化函数。
"""


def literal_string(text, *, compound=True):
    data = text.encode("utf-8")
    # 三位八进制转义不会像 \\x 那样吞掉后续十六进制字符。
    encoded = "".join(f"\\{byte:03o}" for byte in data)
    prefix = "(XeStr)" if compound else ""
    return f'{prefix}{{(const unsigned char *)"{encoded}", {len(data)}}}'


def numeric_static_value(node, constants):
    """折叠静态一元正负号，不在窄整数临时变量上执行取负。"""
    kind = node["kind"]
    if kind == "Literal" and node["literal_kind"] in {"INTEGER", "FLOAT"}:
        return node["value"]
    if kind == "Group":
        return numeric_static_value(node["expression"], constants)
    if kind == "Unary" and node["operator"] in {"+", "-"}:
        value = numeric_static_value(node["operand"], constants)
        if value is not None:
            return -value if node["operator"] == "-" else value
    if kind == "Name":
        name = "::".join(node["path"]["parts"])
        if name in constants:
            return numeric_static_value(constants[name].node, constants)
    return None


def integer_static_literal(number):
    if number == -(2**63):
        return "(-INT64_C(9223372036854775807)-1)"
    return f"(-INT64_C({-number}))" if number < 0 else f"UINT64_C({number})"


class GlobalBackend:
    def global_declarations(self):
        """Tentative declarations let an address initializer name a later global."""
        return [f"static {self.ctype(slot.type)} {slot.name};"
                for slot in self.global_slots.values()]

    def global_definitions(self):
        definitions = []
        for name, binding in getattr(self.checker, "globals", {}).items():
            slot = self.global_slots[name]
            initial = self.global_initializer(binding.node["value"], slot.type)
            definitions.append(f"static {self.ctype(slot.type)} {slot.name} = {initial};")
        return definitions

    def global_initializer(self, node, type_):
        """Render a brace initializer for aggregates, a constant expression otherwise."""
        kind = node["kind"]
        if kind == "Group":
            return self.global_initializer(node["expression"], type_)
        if kind == "Literal":
            category, value = node["literal_kind"], node["value"]
            if category == "STRING":
                return literal_string(value, compound=False)
            if category in {"CHAR", "BYTE"}:
                return str(ord(value))
            if category in {"true", "false"}:
                return "true" if value else "false"
            if category == "unit":
                return "0"
            return str(value) if category == "FLOAT" else integer_static_literal(value)
        if kind == "Unary" and node["operator"] in {"+", "-"}:
            value = numeric_static_value(node, self.checker.constants)
            if value is not None:
                return integer_static_literal(value) if isinstance(value, int) else str(value)
            self.fail(node, "全局一元正负号需要静态数值")
        target = getattr(self.checker, "call_targets", {}).get(id(node))
        if target is not None and kind in {"Name", "BracketApply", "AssociatedAccess"}:
            return self.function_name(target)
        if kind == "Name":
            name = "::".join(node["path"]["parts"])
            if name in self.checker.constants:
                return self.global_initializer(self.checker.constants[name].node, type_)
            if name in self.checker.functions:
                return self.function_name(name)
            self.fail(node, "全局初始值只能引用模块只读绑定或函数；可变全局只能取静态地址")
        if kind == "Borrow":
            code, _ = self.global_place(node["operand"])
            return f"&({code})"
        if kind == "Array":
            values = [self.global_initializer(element, type_.args[0])
                      for element in node["elements"]]
            return "{.items = {" + (", ".join(values) or "0") + "}}"
        if kind in {"StructLiteral", "Tuple"}:
            fields = dict(self.fields(type_))
            initializers = (node["fields"] if kind == "StructLiteral" else
                            [{"name": str(i), "value": element}
                             for i, element in enumerate(node["elements"])])
            values = [f".{self.global_field_name(field['name'])} = " +
                      self.global_initializer(field["value"], fields[field["name"]])
                      for field in initializers]
            return "{" + (", ".join(values) or "0") + "}"
        self.fail(node, f"全局初始值不支持运行时表达式 {kind}")

    def global_place(self, node):
        """A static address may name a global, an embedded field or a fixed array element."""
        kind = node["kind"]
        if kind == "Group":
            return self.global_place(node["expression"])
        if kind == "Name":
            name = "::".join(node["path"]["parts"])
            if name in self.global_slots:
                slot = self.global_slots[name]
                return slot.name, slot.type
        if kind == "FieldAccess":
            code, type_ = self.global_place(node["object"])
            fields = dict(self.fields(type_))
            if type_.name == "ptr" or node["field"] not in fields:
                self.fail(node, "全局静态地址只支持按值字段，不支持读取指针后间接取地址")
            return f"({code}).{self.global_field_name(node['field'])}", fields[node["field"]]
        if kind == "BracketApply":
            code, type_ = self.global_place(node["object"])
            if type_.name != "Array" or len(node["arguments"]) != 1:
                self.fail(node, "全局静态下标地址需要固定长度数组")
            index = numeric_static_value(node["arguments"][0], self.checker.constants)
            if not isinstance(index, int) or not 0 <= index < int(type_.args[1].name):
                self.fail(node, "全局静态下标地址需要范围内的固定整数下标")
            return f"({code}).items[{index}]", type_.args[0]
        self.fail(node, "全局地址初始值必须指向全局存储或它的字段、固定数组元素")
