import abc
from ast import Dict
from typing import override
from llvmlite import ir
from .types import *


# 符号
## IRSymbol 只负责从module里获取，创建由 StatementNode 负责
class IRSymbol:
    pass


class IRVariableSymbol(IRSymbol, ExpressionNode):
    def __init__(
        self,
        name: str,
        value_type: TypeNode,
        ir_value: ir.Value | ir.AllocaInstr,
        is_alloca: bool = True,
        is_global: bool = False,
    ):
        self.name = name
        self.value_type = value_type
        self.ir_value = ir_value
        self.is_alloca = is_alloca
        self.is_global = is_global

    @override
    def get_value_type(self) -> TypeNode:
        return self.value_type

    def get_ir_ptr(self, builder) -> ir.AllocaInstr:
        assert self.is_alloca, "not alloca"
        if self.is_global:
            return builder.module.get_global(self.name)
        else:
            return self.ir_value

    @override
    def get_ir_value(self, builder: ir.IRBuilder) -> ir.Value:
        if self.is_alloca:
            return builder.load(self.get_ir_ptr(builder))
        else:
            return self.ir_value


class IRFunctionSymbol(IRSymbol):
    def __init__(
        self,
        name: str,
        return_type: TypeNode,
        arg_names: List[str],
        arg_types: List[TypeNode],
    ):
        self.name = name
        self.return_type = return_type
        self.arg_names = arg_names
        self.arg_types = arg_types

    def arg_idx(self, arg_name: str) -> int:
        return self.arg_names.index(arg_name)

    def get_ir_type(self):
        return ir.FunctionType(
            self.return_type.get_ir_type(),
            [arg_type.get_ir_type() for arg_type in self.arg_types],
        )

    def get_ir_function(self, builder: ir.IRBuilder):
        ir_func = builder.module.get_global(self.name)
        assert isinstance(ir_func, ir.Function)
        return ir_func


class VarTypePairNode(ASTNode):
    def __init__(self, name: str, var_type: TypeNode):
        self.name = name  # 参数名
        self.var_type = var_type  # 参数类型

    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "name": self.name,
            "var_type": try_to_dict(self.var_type),
        }


### 不直接实例化
class IRIdentifiedTypeSymbol(IRSymbol, TypeNode):
    def __init__(self, name: str):
        self.name = name

    @override
    def get_ir_type(self, module: ir.Module):
        identified_types = module.get_identified_types()
        if self.name in identified_types:
            return identified_types[self.name]
        else:
            raise ValueError(f"Type {self.name} not found")


class IRStructTypeSymbol(IRIdentifiedTypeSymbol):
    def __init__(self, name: str, field_names: List[str], field_types: List[TypeNode]):
        self.name = name
        self.field_names = field_names
        self.field_types = field_types

    def field_idx(self, field_name: str) -> int:
        return self.field_names.index(field_name)

    def get_field_type(self, field_name: str) -> TypeNode:
        return self.field_types[self.field_idx(field_name)]

    def add_method(self, function: IRFunctionSymbol):
        raise NotImplementedError("not implemented")

    @override
    def store(
        self,
        builder: ir.IRBuilder,
        ir_var_ptr: ir.Value | ir.AllocaInstr,
        value: "ExpressionNode",
    ):
        assert value.__class__.__name__ == "StructLiteralNode", (
            "Value must be a struct literal"
        )
        # raise NotImplementedError("not implemented")
        struct_type = value.get_value_type()
        assert isinstance(struct_type, IRStructTypeSymbol)
        struct_ir_value = value.get_ir_value(builder)
        builder.store(struct_ir_value, ir_var_ptr)


class EnumTypeNode(IRIdentifiedTypeSymbol):
    pass


# 符号栈
class SymbolTable:
    def __init__(self, is_global: bool = False):
        self.is_global = is_global
        self.symbols: Dict[str, IRSymbol] = {}

    def add(self, name: str, ir_local_var: IRSymbol):
        if name in self.symbols:
            raise ValueError(f"Symbol '{name}' already exists.")
        self.symbols[name] = ir_local_var

    def get(self, name: str) -> IRSymbol:
        if name not in self.symbols:
            raise KeyError(f"Symbol '{name}' not found.")
        return self.symbols.get(name)

    def assign(self, name: str, ir_local_var: IRSymbol):
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

    def push(self, is_global=False):
        self.stack.append(SymbolTable(is_global))

    def pop(self) -> SymbolTable:
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack.pop()

    def current(self) -> SymbolTable:
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack[-1]

    def add(self, name: str, ir_local_var: IRSymbol):
        # 在当前符号表中添加变量
        self.current().add(name, ir_local_var)

    def get(self, name) -> IRSymbol:
        for table in reversed(self.stack):
            if name in table:
                return table.get(name)
        raise KeyError(f"Symbol '{name}' not found in any symbol table.")

    def assign(self, name: str, ir_local_var: IRSymbol):
        # 在当前符号表中查找变量并赋值
        for table in reversed(self.stack):
            if name in table:
                table.assign(name, ir_local_var)
        raise KeyError(f"Symbol '{name}' not found in any symbol table.")


symbol_table_stack = SymbolTableStack()


# 占位符
class PlaceHolder(ASTNode):
    ## parser
    def __init__(self, name: str):
        self.name = name

    @override
    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "name": self.name,
        }

    ## codegen
    @abstractmethod
    def get_symbol(self) -> IRSymbol:
        raise NotImplementedError(
            f"Subclasses {self.__class__.__name__} should implement this method"
        )


class PHVariable(PlaceHolder, IRVariableSymbol):
    def __init__(self, name: str):
        super().__init__(name)

    @override
    def get_symbol(self) -> IRVariableSymbol:
        ir_var_symbol = symbol_table_stack.get(self.name)
        assert isinstance(ir_var_symbol, IRVariableSymbol)
        return ir_var_symbol

    @override
    def get_value_type(self) -> TypeNode:
        return self.get_symbol().get_value_type()

    @override
    def get_ir_value(self, builder: ir.IRBuilder) -> ir.Value:
        return self.get_symbol().get_ir_value(builder)

    @override
    def get_ir_ptr(self, builder: ir.IRBuilder) -> ir.AllocaInstr:
        return self.get_symbol().get_ir_ptr(builder)


class PHFunction(PlaceHolder):
    def __init__(self, name: str):
        super().__init__(name)

    @override
    def get_symbol(self) -> IRFunctionSymbol:
        ir_function_symbol = symbol_table_stack.get(self.name)
        assert isinstance(ir_function_symbol, IRFunctionSymbol)
        return ir_function_symbol

    def get_ir_function(self, builder: ir.IRBuilder) -> ir.Function:
        return self.get_symbol().get_ir_function(builder)

    def get_return_type(self) -> TypeNode:
        return self.get_symbol().return_type

    def get_ir_return_type(self) -> ir.Type:
        return self.get_return_type().get_ir_type()


class PHIdentifiedType(PlaceHolder, IRIdentifiedTypeSymbol):
    def __init__(self, name: str):
        super().__init__(name)

    @override
    def get_symbol(self) -> IRIdentifiedTypeSymbol:
        ir_struct_type_symbol = symbol_table_stack.get(self.name)
        return ir_struct_type_symbol

    @override
    def store(
        self,
        builder: ir.IRBuilder,
        ir_var_ptr: ir.Value | ir.AllocaInstr,
        value: "ExpressionNode",
    ):
        self.get_symbol().store(builder, ir_var_ptr, value)


class PHStructType(PHIdentifiedType):
    def __init__(self, name: str):
        super().__init__(name)

    @override
    def get_symbol(self) -> IRStructTypeSymbol:
        ir_struct_type_symbol = symbol_table_stack.get(self.name)
        assert isinstance(ir_struct_type_symbol, IRStructTypeSymbol)
        return ir_struct_type_symbol

    @override
    def store(
        self,
        builder: ir.IRBuilder,
        ir_var_ptr: ir.Value | ir.AllocaInstr,
        value: "ExpressionNode",
    ):
        raise TypeError("StructTypeNode cannot be stored")
