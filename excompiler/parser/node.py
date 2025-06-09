# import json
from typing import List, override
from llvmlite import ir
from .symtable import *


# builder: ir.IRBuilder  # 全局IR构建器


# class IdentifierNode(ASTNode):
#     def __init__(self, name: str):
#         self.name = name  # 标识符名称

#     def to_dict(self):
#         # return {"ClassName": "Identifier", "name": self.name}
#         raise NotImplementedError(
#             "IdentifierNode does not support to_dict method. Use PHVariable instead."
#         )


## frame


## 没有 codegen
class ModuleNode(ASTNode):
    def __init__(self, module_items: List[ASTNode]):
        self.body = module_items  # 模块中的项目列表

    def to_dict(self):
        Module = {
            "ClassName": self.__class__.__name__,
            "body": [try_to_dict(node) for node in self.body],
        }
        return Module


class BlockNode(ExpressionNode):
    def __init__(self, statements: List[ASTNode]):
        self.body = statements  # 语句列表

    def to_dict(self):
        Block = {
            "ClassName": self.__class__.__name__,
            "body": [try_to_dict(node) for node in self.body],
        }
        return Block


## statement , has codegen method
class FunctionDefNode(StatementNode):
    def __init__(
        self,
        name: str,
        return_type: TypeNode,
        args: List[VarTypePairNode],
        block: BlockNode,
    ):
        self.name = name  # 函数名
        if not return_type:
            raise ValueError("There should be a function return type")
        self.return_type = return_type  # 返回类型
        self.args = args  # 参数列表
        self.body = block.body  # 函数体
        arg_names: List[str] = []
        arg_types: List[TypeNode] = []
        for pair in args:
            if pair.name in arg_names:
                raise ValueError(f"Duplicate argument name: {pair.name}")
            arg_names.append(pair.name)
            arg_types.append(pair.var_type)
        self.arg_names = arg_names
        self.arg_types = arg_types

    def to_dict(self):
        Function = {
            "ClassName": self.__class__.__name__,
            "name": self.name,
            "return_type": try_to_dict(self.return_type),
            "args": [try_to_dict(arg) for arg in self.args],
            "body": [try_to_dict(stat) for stat in self.body],
        }
        return Function

    def codegen(self, module: ir.Module):
        global symbol_table_stack
        assert symbol_table_stack.current().is_global, ValueError(
            "Function definition should be in global scope"
        )
        ir_function_symbol = IRFunctionSymbol(self.name, self.return_type, self.arg_names, self.arg_types)
        symbol_table_stack.current().add(self.name, ir_function_symbol)
        # 创建函数类型
        func_type = ir_function_symbol.get_ir_type()
        function = ir.Function(module, func_type, name=self.name)
        entry_block = function.append_basic_block("entry")
        builder = ir.IRBuilder(entry_block)

        symbol_table_stack.push()  # Push a new symbol table for the function
        # 将参数添加到符号表
        for i, name in enumerate(self.arg_names):
            ir_local_var = IRVariableSymbol(
                name, self.arg_types[i], function.args[i], is_alloca=False
            )
            symbol_table_stack.add(name, ir_local_var)  # 添加到符号表
        # 处理函数体
        for stat in self.body:
            stat.codegen(builder)

        # 检查块是否已终止
        assert isinstance(builder.block, ir.Block), ValueError(
            "Builder block is not an instance of ir.Block."
        )
        if not builder.block.is_terminated:
            builder.ret(ir.Constant(ir.IntType(32), 0))

        symbol_table_stack.pop()  # Pop the symbol table for the function


class StructTypeDefNode(StatementNode):
    def __init__(self, name: str, var_type_pairs: List[VarTypePairNode]):
        self.name = name
        self.var_type_pairs = var_type_pairs  # 结构体成员列表
        field_names: List[str] = []
        field_types: List[TypeNode] = []
        for pair in var_type_pairs:
            if pair.name in field_names:
                raise ValueError(f"Duplicate field name: {pair.name}")
            field_names.append(pair.name)
            field_types.append(pair.var_type)
        self.field_names = field_names
        self.field_types = field_types

    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "name": self.name,
            "var_type_pairs": [try_to_dict(pair) for pair in self.var_type_pairs],
        }

    def codegen(self, module: ir.Module):
        global symbol_table_stack
        assert symbol_table_stack.current().is_global, ValueError(
            "Struct definition should be in global scope"
        )
        ir_struct_type_symbol = IRStructTypeSymbol(
            self.name, self.field_names, self.field_types
        )
        symbol_table_stack.current().add(self.name, ir_struct_type_symbol)
        ctx = module.context
        struct_type = ctx.get_identified_type(self.name)
        struct_type.set_body(*[field_type.get_ir_type() for field_type in self.field_types])
        return struct_type


class ReturnStatementNode(StatementNode):
    def __init__(self, expression: ExpressionNode):
        self.value = expression  # 返回表达式

    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "value": self.value.to_dict()}

    def codegen(self, builder: ir.IRBuilder):
        value = self.value.get_ir_value(builder)
        builder.ret(value)


class VariableDeclarationNode(StatementNode):
    def __init__(
        self, var_type_pair: VarTypePairNode, equal_or_move: str, value: ExpressionNode
    ):
        self.variable = var_type_pair  # 变量名和类型
        self.equal_or_move = equal_or_move
        self.value = value  # 可选的初始值
        if self.value is None:
            raise NotImplementedError("Variable declaration without initial value")

    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "variable": try_to_dict(self.variable),
            "equal_or_move": self.equal_or_move,
            "value": try_to_dict(self.value),
        }

    def codegen(self, bm: ir.IRBuilder | ir.Module):
        global symbol_table_stack
        if isinstance(bm, ir.Module):
            assert symbol_table_stack.current().is_global
            # 全局变量
            ir_var = ir.GlobalVariable(
                module,
                self.variable.var_type.get_ir_type(),
                name=self.variable.name,
            )
            ir_var.initializer = self.value.get_ir_value()
        else:
            # 局部变量
            assert not symbol_table_stack.current().is_global
            ir_var = bm.alloca(
                self.variable.var_type.get_ir_type(bm.module), name=self.variable.name
            )
            self.variable.var_type.store(bm, ir_var, self.value)

        ir_var_symbol = IRVariableSymbol(
            self.variable.name,
            self.variable.var_type,
            ir_var,
            is_alloca=True,
            is_global=False,
        )

        symbol_table_stack.current().add(self.variable.name, ir_var_symbol)


class VarEqualNode(StatementNode):
    def __init__(self, variable: PHVariable, value: ExpressionNode):
        self.variable = variable  # 变量名
        self.value = value  # 赋值表达式

    def to_dict(self):
        return {
            "ClassName": "VarEqual",
            "variable": try_to_dict(self.variable),
            "value": try_to_dict(self.value),
        }


class PtrDerefEqualNode(StatementNode):
    def __init__(self, variable: PHVariable, value: ExpressionNode):
        self.variable = variable  # 变量名
        self.value = value  # 赋值表达式

    def to_dict(self):
        return {
            "ClassName": "PtrDerefEqual",
            "variable": try_to_dict(self.variable),
            "value": try_to_dict(self.value),
        }


## expression


class BinaryExpressionNode(ExpressionNode):
    def __init__(self, left: ExpressionNode, right: ExpressionNode, operator: str):
        self.left = left  # 左操作数
        self.right = right  # 右操作数
        self.operator = operator  # 操作符

    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "operator": self.operator,
            "left": try_to_dict(self.left),
            "right": try_to_dict(self.right),
        }

    def get_ir_value(self, builder: ir.IRBuilder):
        assert self.left.get_value_type() == self.right.get_value_type(), ValueError(
            "Type mismatch in binary expression"
        )
        left = self.left.get_ir_value()
        right = self.right.get_ir_value()

        if self.operator == "+":
            return builder.add(left, right)
        elif self.operator == "-":
            return builder.sub(left, right)
        elif self.operator == "*":
            return builder.mul(left, right)
        elif self.operator == "/":
            return builder.sdiv(left, right)
        else:
            raise NotImplementedError(f"Unsupported operator: {self.operator}")

    @override
    def get_value_type(self) -> TypeNode:
        assert self.left.get_value_type() == self.right.get_value_type(), ValueError(
            "Type mismatch in binary expression"
        )
        return self.left.get_value_type()


class UnaryExpressionNode(ExpressionNode):
    def __init__(self, operator: str, primary: ExpressionNode):
        self.operator = operator  # 操作符
        self.value = primary  # 表达式

    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "operator": self.operator,
            "value": try_to_dict(self.value),
        }

    def get_ir_value(self, builder: ir.IRBuilder):
        value = self.value.get_ir_value()
        if self.operator == "-":
            return builder.neg(value)
        elif self.operator == "+":
            return value
        elif self.operator == "not":
            return builder.not_(value)
        else:
            raise NotImplementedError(f"Unsupported operator: {self.operator}")

    @override
    def get_value_type(self) -> TypeNode:
        return self.value.get_value_type()


def proccess_str_literal(raw_content):
    return (
        raw_content.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\\\", "\\")
    )


import hashlib


def get_hash_name(s: str) -> str:
    hash_val = hashlib.md5(s.encode()).hexdigest()  # 生成128位哈希
    return f"str_{hash_val[:8]}"  # 取前8位作为短名称[6](@ref)


class StringNode(ExpressionNode):
    def __init__(self, value: str):
        self.value_type = PointerTypeNode(StrTypeNode())
        self.value = proccess_str_literal(value)  # 字符串值

    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "value": self.value}

    def get_ir_value(self, builder: ir.IRBuilder):
        name = get_hash_name(self.value)
        if name in builder.module.globals:
            return builder.module.get_global(name)
        else:
            str_bytes = self.value.encode() + b"\0"
            str_arr = ir.Constant(
                ir.ArrayType(ir.IntType(8), len(str_bytes)), bytearray(str_bytes)
            )
            str_var = ir.GlobalVariable(
                builder.module, ir.ArrayType(ir.IntType(8), len(str_bytes)), name=name
            )
            str_var.initializer = str_arr  # type: ignore
            str_var.linkage = "internal"  # 限制作用域，避免外部可见性
            str_var = builder.bitcast(str_var, ir.PointerType(ir.IntType(8)))
            return str_var


class CallExpressionNode(ExpressionNode):
    def __init__(self, function: PHFunction, args: List[ExpressionNode]):
        self.function = function  # 函数名
        self.args = args  # 参数列表

    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "function": try_to_dict(self.function),
            "args": [try_to_dict(arg) for arg in self.args],
        }

    def get_ir_value(self, builder: ir.IRBuilder):
        function = builder.module.get_global(self.function_name)
        if not isinstance(function, ir.Function):
            raise ValueError(f"Function '{self.function_name}' not found in module.")
        args = [arg.codegen() for arg in self.args]
        call = builder.call(function, args)
        return call

    def get_value_type(self, builder: ir.IRBuilder) -> TypeNode:
        global symbol_table_stack
        func_node = symbol_table_stack.get(self.function_name)
        if not isinstance(func_node, FunctionNode):
            raise ValueError(f"Function '{self.function_name}' not found in module.")
        return func_node.return_type


### var@
class NamedVarPointerNode(ExpressionNode):
    def __init__(self, variable: PHVariable):
        self.variable = variable  # 变量名

    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "variable": try_to_dict(
                self.variable,
            ),
        }


class PtrDerefNode(ExpressionNode):
    def __init__(self, variable: PHVariable):
        self.variable = variable  # 变量名

    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "variable": try_to_dict(self.variable),
        }


class ArrayNode(ExpressionNode):
    def __init__(self, elements: List[ASTNode]):
        self.elements = elements  # 数组元素列表

    def to_dict(self):
        return {
            "ClassName": "Array",
            "elements": [try_to_dict(element) for element in self.elements],
        }


class ArrayItemNode(ExpressionNode):
    def __init__(self, array: PHVariable, index: ExpressionNode):
        self.array = array  # 数组名
        self.index = index  # 索引

    def to_dict(self):
        return {
            "ClassName": "ArrayItem",
            "array": try_to_dict(self.array),
            "index": try_to_dict(self.index),
        }


class StructLiteralNode(ExpressionNode):
    def __init__(self, struct_type: TypeNode, body: List[ASTNode]):
        self.struct_type = struct_type  # 结构体类型
        self.body = body  # 结构体成员列表

    def to_dict(self):
        return {
            "ClassName": "StructLiteral",
            "struct_type": try_to_dict(self.struct_type),
            "body": [try_to_dict(stat) for stat in self.body],
        }


class ObjectFieldNode(ExpressionNode):
    def __init__(self, variable: PHVariable, field: str):
        self.object = variable  # 对象
        self.field = field  # 字段名

    def to_dict(self):
        return {
            "ClassName": "ObjectField",
            "object": try_to_dict(self.object),
            "field": self.field,
        }


# stage04


# class VarRefTypePairNode(ASTNode):
#     def __init__(self, name, var_type):
#         super().__init__("VarRefTypePair")
#         self.name = name  # 参数名
#         self.var_type = var_type  # 参数类型

#     def to_dict(self):
#         return {
#             "type": "VarRefTypePair",
#             "name": self.name,
#             "var_type": self.var_type.to_dict(),
#         }
#
#
#
#
#
#
#
#
#
#
#
#
#
#
