from ast import Dict
from typing import override
from llvmlite import ir
from .types import *


# 占位符
class PlaceHolder(ASTNode):
    def __init__(self, name: str):
        self.name = name

    @override
    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "name": self.name,
        }


class PHVariable(PlaceHolder, ExpressionNode):
    def __init__(self, name: str):
        super().__init__(name)


class PHFunction(PlaceHolder):
    def __init__(self, name: str):
        super().__init__(name)


class PHType(PlaceHolder, TypeNode):
    def __init__(self, name: str):
        super().__init__(name)


class PHStructType(PHType):
    def __init__(self, name: str):
        super().__init__(name)


class IRSymbol:
    pass


class IRVariableSymbol(IRSymbol):
    def __init__(
        self, name: str, var_type: TypeNode, ir_value: ir.Value, is_alloca: bool = True
    ):
        self.name = name
        self.var_type = var_type
        self.ir_value = ir_value
        self.is_alloca = is_alloca
        self.is_global = False

    def ir_type(self) -> ir.Type:
        return self.var_type.get_ir_type()


class IRFunctionSymbol(IRSymbol):
    def __init__(
        self,
        name: str,
        function_node
    ):
        self.name = name
        self.function_node = function_node


class IRTypeSymbol(IRSymbol):
    def __init__(self, name: str):
        self.name = name
        self.var_type = var_type



# 符号栈
class SymbolTable:
    def __init__(self):
        self.symbols: Dict[str, IRVariableSymbol] = {}

    def add(self, name: str, ir_local_var: IRVariableSymbol):
        if name in self.symbols:
            raise ValueError(f"Symbol '{name}' already exists.")
        self.symbols[name] = ir_local_var

    def get(self, name: str) -> IRVariableSymbol:
        if name not in self.symbols:
            raise KeyError(f"Symbol '{name}' not found.")
        return self.symbols.get(name)

    def assign(self, name: str, ir_local_var: IRVariableSymbol):
        if name not in self.symbols:
            raise KeyError(f"Symbol '{name}' not found.")
        self.symbols[name] = ir_local_var

    def __contains__(self, name: str) -> bool:
        return name in self.symbols

    def __repr__(self) -> str:
        return f"SymbolTable({self.symbols})"


class SymbolTableStack:
    def __init__(self):
        self.stack = []

    def push(self):
        self.stack.append(SymbolTable())

    def pop(self) -> SymbolTable:
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack.pop()

    def current(self) -> SymbolTable:
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack[-1]

    def add(self, name: str, ir_local_var: IRVariableSymbol):
        # 在当前符号表中添加变量
        self.current().add(name, ir_local_var)

    def get(self, name) -> IRVariableSymbol:
        for table in reversed(self.stack):
            if name in table:
                return table.get(name)
        raise KeyError(f"Symbol '{name}' not found in any symbol table.")

    def assign(self, name: str, ir_local_var: IRVariableSymbol):
        # 在当前符号表中查找变量并赋值
        for table in reversed(self.stack):
            if name in table:
                table.assign(name, ir_local_var)
        raise KeyError(f"Symbol '{name}' not found in any symbol table.")
