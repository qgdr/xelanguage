"""全局变量的静态初值契约，不执行 Xe 函数，也不依赖 C 后端。

全局对象必须在 main 前已经有初值，因此不能把普通函数调用偷偷降低成
启动函数。允许的形状是可直接表达的静态数据与静态地址；资源初始化及
退出时 Drop 要另行设计。普通表达式的类型/范围仍复用 Checker。
"""
from typing import TYPE_CHECKING, NoReturn, cast

from .static_values import StaticValueError, scalar_static_value
from .typesys import INT_LITERAL, NUMERIC, Type

if TYPE_CHECKING:
    from .semantic import Checker


class GlobalChecker:
    def global_initializer_error(self, node, message) -> NoReturn:
        self = cast("Checker", self)
        self.fail(node, message, "XE-GLOBAL-0002",
                  "使用字面量、Copy 聚合值、只读常量或全局地址；需要计算时在 main 中赋值")

    def static_initializer(self, node, seen=frozenset()):
        """先拒绝动态形状，再让通用 infer 检查类型；check 与 build 边界一致。"""
        self = cast("Checker", self)
        kind = node["kind"]
        if kind == "Literal":
            return
        if kind == "Group":
            self.static_initializer(node["expression"], seen)
            return
        if kind == "Unary" and node["operator"] in {"+", "-", "bitnot", "not"}:
            self.static_initializer(node["operand"], seen)
            return
        if kind == "Binary":
            self.static_initializer(node["left"], seen)
            self.static_initializer(node["right"], seen)
            return
        if kind in {"Array", "Tuple"}:
            for element in node["elements"]:
                self.static_initializer(element, seen)
            return
        if kind == "StructLiteral":
            for field in node["fields"]:
                self.static_initializer(field["value"], seen)
            return
        if kind == "Name":
            name = "::".join(node["path"]["parts"])
            if name in self.constants:
                if name in seen:
                    self.global_initializer_error(node, f"模块只读初值循环引用 {name}")
                self.static_initializer(self.constants[name].node, seen | {name})
                return
            if name in self.functions:
                # infer 会验证是否是具名、具体签名及合法可见性。
                return
            self.global_initializer_error(node, "全局初始值不能读取另一可变全局变量的运行时值")
        if kind == "Borrow":
            self.static_global_place(node["operand"])
            return
        self.global_initializer_error(node, "全局初始值必须是静态数据，不能包含函数调用或运行时运算")

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
            number = self.static_number(index, Type("usize"))
            if type(number) is not int:
                self.global_initializer_error(index, "静态元素地址需要整数常量下标")
            array = self.infer(node["object"]).type
            if not 0 <= number < int(array.args[1].name):
                self.global_initializer_error(index, "静态数组元素地址的下标越界")
            return
        self.global_initializer_error(node, "静态初值只能取全局对象及其字段、数组元素的地址")

    def static_number(self, node, expected=None):
        """折叠已验证的静态标量；不执行用户函数或读取可变对象。"""
        self = cast("Checker", self)
        try:
            return scalar_static_value(node, self.constants, expected)
        except StaticValueError as error:
            self.global_initializer_error(node, str(error))

    def check_static_numbers(self, node, expected: Type):
        """静态符号折叠也须检查最终边界，防止 -MIN 被 C 静默截断。"""
        self = cast("Checker", self)
        from .semantic import Value
        number = self.static_number(node, expected)
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
            from .typesys import substitute
            substitutions = {"$" + parameter["name"]: type_ for parameter, type_ in
                             zip(declaration.get("generics", []), expected.args)}
            field_types = {field["name"]: substitute(
                self.type_of(field["type"], self.generic_set(declaration)), substitutions)
                for field in declaration["fields"]}
            for field in node["fields"]:
                self.check_static_numbers(field["value"], field_types[field["name"]])
