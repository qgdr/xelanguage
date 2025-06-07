from ast import Dict
from llvmlite import ir
from .types import *


class IRLocalVar:
    def __init__(
        self, name: str, ir_value: ir.Value, var_type: TypeNode, is_alloca: bool = True
    ):
        self.name = name
        self.ir_value = ir_value
        self.var_type = var_type
        self.is_alloca = is_alloca

    def __repr__(self):
        return f"IRLocalVar(name={self.name}, ir_value={self.ir_value}, var_type={self.var_type}, is_alloca={self.is_alloca})"

    def ir_type(self) -> ir.Type:
        return self.var_type.get_ir_type()


# 符号栈
class SymbolTable:
    def __init__(self):
        self.symbols : Dict[str, IRLocalVar] =  {}

    def add(self, name: str, ir_local_var: IRLocalVar):
        if name in self.symbols:
            raise ValueError(f"Symbol '{name}' already exists.")
        self.symbols[name] = ir_local_var

    def get(self, name: str) -> IRLocalVar:
        if name not in self.symbols:
            raise KeyError(f"Symbol '{name}' not found.")
        return self.symbols.get(name)

    def assign(self, name: str, ir_local_var: IRLocalVar):
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

    def add(self, name: str, ir_local_var: IRLocalVar):
        # 在当前符号表中添加变量
        self.current().add(name, ir_local_var)

    def get(self, name) -> IRLocalVar:
        for table in reversed(self.stack):
            if name in table:
                return table.get(name)
        raise KeyError(f"Symbol '{name}' not found in any symbol table.")

    def assign(self, name: str, ir_local_var: IRLocalVar):
        # 在当前符号表中查找变量并赋值
        for table in reversed(self.stack):
            if name in table:
                table.assign(name, ir_local_var)
        raise KeyError(f"Symbol '{name}' not found in any symbol table.")
