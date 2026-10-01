"""全局变量的静态初值契约，不执行 Xe 函数，也不依赖 C 后端。

全局对象必须在 main 前已经有初值，因此不能把普通函数调用偷偷降低成
启动函数。允许的形状是可直接表达的静态数据与静态地址；资源初始化及
退出时 Drop 要另行设计。普通表达式的类型/范围仍复用 Checker。
"""
from typing import TYPE_CHECKING, cast

from .typesys import INT_LITERAL, NUMERIC, Type

if TYPE_CHECKING:
    from .semantic import Checker


class GlobalChecker:
    def global_initializer_error(self, node, message):
        self = cast("Checker", self)
        self.fail(node, message, "XE-GLOBAL-0002",
                  "使用字面量、Copy 聚合值、只读常量或全局地址；需要计算时在 main 中赋值")

    def static_initializer(self, node):
        """先拒绝动态形状，再让通用 infer 检查类型；check 与 build 边界一致。"""
        self = cast("Checker", self)
        kind = node["kind"]
        if kind == "Literal":
            return
        if kind == "Group":
            self.static_initializer(node["expression"])
            return
        if kind == "Unary" and node["operator"] in {"+", "-"}:
            self.static_initializer(node["operand"])
            if self.static_number(node) is None:
                self.global_initializer_error(node, "全局初值的一元正负号需要静态数值")
            return
        if kind in {"Array", "Tuple"}:
            for element in node["elements"]:
                self.static_initializer(element)
            return
        if kind == "StructLiteral":
            for field in node["fields"]:
                self.static_initializer(field["value"])
            return
        if kind == "Name":
            name = "::".join(node["path"]["parts"])
            if name in self.constants:
                self.static_initializer(self.constants[name].node)
                return
            if name in self.functions:
                # infer 会验证是否是具名、具体签名及合法可见性。
                return
            self.global_initializer_error(node, "全局初值不能读取另一可变全局变量的运行时值")
        if kind == "Borrow":
            self.static_global_place(node["operand"])
            return
        self.global_initializer_error(node, "全局初值必须是静态数据，不能包含函数调用或运行时运算")

    def static_global_place(self, node):
        """仅静态对象本身/字段/固定数组元素有可在程序启动前确定的地址。"""
        self = cast("Checker", self)
        kind = node["kind"]
        if kind == "Group":
            self.static_global_place(node["expression"])
            return
        if kind == "Name" and "::".join(node["path"]["parts"]) in self.globals:
            return
        if kind == "FieldAccess":
            self.static_global_place(node["object"])
            if self.infer(node["object"]).type.name == "ptr":
                self.global_initializer_error(node, "静态地址不能通过全局指针间接读取另一个对象")
            return
        if kind == "BracketApply":
            self.static_global_place(node["object"])
            if self.infer(node["object"]).type.name != "Array" or len(node["arguments"]) != 1:
                self.global_initializer_error(node, "静态元素地址只支持全局 Array 的固定下标")
            index = node["arguments"][0]
            self.static_initializer(index)
            if type(self.static_number(index)) is not int:
                self.global_initializer_error(index, "静态元素地址需要整数常量下标")
            return
        self.global_initializer_error(node, "静态初值只能取全局可写对象及其字段、数组元素的地址")

    def static_number(self, node):
        """只折叠已有静态标量和正负号，不在这里实现通用常量解释器。"""
        self = cast("Checker", self)
        kind = node["kind"]
        if kind == "Literal":
            return node["value"] if node["literal_kind"] in {"INTEGER", "FLOAT", "BYTE", "CHAR"} else None
        if kind == "Group":
            return self.static_number(node["expression"])
        if kind == "Name":
            constant = self.constants.get("::".join(node["path"]["parts"]))
            return self.static_number(constant.node) if constant is not None else None
        if kind == "Unary" and node["operator"] in {"+", "-"}:
            value = self.static_number(node["operand"])
            if isinstance(value, (int, float)):
                return -value if node["operator"] == "-" else value
        return None

    def check_static_numbers(self, node, expected):
        """静态符号折叠也须检查最终边界，防止 -MIN 被 C 静默截断。"""
        self = cast("Checker", self)
        from .semantic import Value
        number = self.static_number(node)
        if expected.name in NUMERIC and type(number) is int:
            self.convert(Value(INT_LITERAL, node, literal=number), expected)
            return
        if node["kind"] == "Group":
            self.check_static_numbers(node["expression"], expected)
        elif node["kind"] == "Array":
            for element in node["elements"]:
                self.check_static_numbers(element, expected.args[0])
        elif node["kind"] == "Tuple":
            for element, type_ in zip(node["elements"], expected.args):
                self.check_static_numbers(element, type_)
        elif node["kind"] == "StructLiteral":
            declaration = self.types[expected.name]
            field_types = {field["name"]: self.type_of(field["type"]) for field in declaration["fields"]}
            for field in node["fields"]:
                self.check_static_numbers(field["value"], field_types[field["name"]])
