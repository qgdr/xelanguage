# import json
from typing import List
from llvmlite import ir
from .symtable import *


symbol_table_stack_codegen = SymbolTableStack_codegen()
builder: ir.IRBuilder  # 全局IR构建器




# stage01


class ModuleNode(ASTNode):
    def __init__(self, module_items: List[ASTNode]):
        super().__init__("Module")
        self.body = module_items  # 模块中的项目列表

    def to_dict(self):
        Module = {
            "ClassName": "Module",
            "body": [try_to_dict(node) for node in self.body],
        }
        return Module



class VariableNode(ASTNode):
    def __init__(self, name: str):
        super().__init__("Variable")  # 节点类型为Variable
        self.name = name  # 变量名称

    def to_dict(self):
        return {"ClassName": "Variable", "name": self.name}


class IdentifierNode(ASTNode):
    def __init__(self, name: str):
        super().__init__("Identifier")  # 节点类型为Identifier
        self.name = name  # 标识符名称

    def to_dict(self):
        # return {"ClassName": "Identifier", "name": self.name}
        raise NotImplementedError(
            "IdentifierNode does not support to_dict method. Use VariableNode instead."
        )





class FloatNode(ASTNode):
    def __init__(self, value):
        super().__init__("Float")  # 节点类型为Float
        self.value = value  # 浮点数值

    def to_dict(self):
        return {"ClassName": "Float", "value": self.value}


class BooleanNode(ASTNode):
    def __init__(self, value):
        super().__init__("Boolean")  # 节点类型为Boolean
        self.value = value  # 布尔值

    def to_dict(self):
        return {"ClassName": "Boolean", "value": self.value}





class BlockNode(ASTNode):
    def __init__(self, statements: List[ASTNode]):
        super().__init__("Block")
        self.body = statements  # 语句列表

    def to_dict(self):
        Block = {
            "ClassName": "block",
            "body": [try_to_dict(node) for node in self.body],
        }
        return Block


class FunctionNode(ASTNode):
    def __init__(
        self,
        name: str,
        return_type: TypeNode,
        args: List[VarTypePairNode],
        block: BlockNode,
    ):
        super().__init__("Function")
        self.name = name  # 函数名
        if not return_type:
            raise ValueError("There should be a function return type")
        self.return_type = return_type  # 返回类型
        self.args = args  # 参数列表
        self.body = block.body  # 函数体

    def to_dict(self):
        Function = {
            "ClassName": "Function",
            "name": self.name,
            "args": [try_to_dict(arg) for arg in self.args],
            "body": [try_to_dict(stat) for stat in self.body],
        }
        return Function


class BinaryExpressionNode(ASTNode):
    def __init__(self, left, right, operator):
        super().__init__("BinaryExpression")
        self.left = left  # 左操作数
        self.right = right  # 右操作数
        self.operator = operator  # 操作符

    def to_dict(self):
        return {
            "ClassName": "BinaryExpression",
            "operator": self.operator,
            "left": try_to_dict(self.left),
            "right": try_to_dict(self.right),
        }


class UnaryExpressionNode(ASTNode):
    def __init__(self, operator: str, primary: ASTNode):
        super().__init__("UnaryExpression")
        self.operator = operator  # 操作符
        self.value = primary  # 表达式

    def to_dict(self):
        return {
            "ClassName": "UnaryExpression",
            "operator": self.operator,
            "value": try_to_dict(self.value),
        }


class ReturnStatementNode(ASTNode):
    def __init__(self, expression):
        super().__init__("ReturnStatement")
        self.value = expression  # 返回表达式

    def to_dict(self):
        return {"ClassName": "ReturnStatement", "value": self.value.to_dict()}


class VariableDeclarationNode(ASTNode):
    def __init__(self, var_type_pair: VarTypePairNode, equal_or_move: str, value):
        super().__init__("VariableDeclaration")
        self.variable = var_type_pair  # 变量名和类型
        self.equal_or_move = equal_or_move
        self.value = value  # 可选的初始值

    def to_dict(self):
        return {
            "ClassName": "VariableDeclaration",
            "variable": try_to_dict(self.variable),
            "equal_or_move": self.equal_or_move,
            "value": try_to_dict(self.value),
        }


class VarEqualNode(ASTNode):
    def __init__(self, variable: VariableNode, value):
        super().__init__("VarEqual")
        self.variable = variable  # 变量名
        self.value = value  # 赋值表达式

    def to_dict(self):
        return {
            "ClassName": "VarEqual",
            "variable": try_to_dict(self.variable),
            "value": try_to_dict(self.value),
        }


class CallExpressionNode(ASTNode):
    def __init__(self, function_name: str, args: List[ASTNode]):
        super().__init__("CallExpression")
        self.function_name = function_name  # 函数名
        self.args = args  # 参数列表

    def to_dict(self):
        return {
            "ClassName": "CallExpression",
            "function_name": self.function_name,
            "args": [try_to_dict(arg) for arg in self.args],
        }


# stage02


### var@
class NamedVarPointerNode(ASTNode):
    def __init__(self, name: str):
        super().__init__("NamedVarPointer")
        self.name = name  # 变量名

    def to_dict(self):
        return {
            "ClassName": "NamedVarPointer",
            "name": self.name,
        }




class PtrDerefEqualNode(ASTNode):
    def __init__(self, variable: VariableNode, value: ASTNode):
        super().__init__("PtrDerefEqual")
        self.variable = variable  # 变量名
        self.value = value  # 赋值表达式

    def to_dict(self):
        return {
            "ClassName": "PtrDerefEqual",
            "variable": try_to_dict(self.variable),
            "value": try_to_dict(self.value),
        }


class PtrDerefNode(ASTNode):
    def __init__(self, variable: VariableNode):
        super().__init__("PtrDeref")
        self.variable = variable  # 变量名

    def to_dict(self):
        return {
            "ClassName": "PtrDeref",
            "variable": try_to_dict(self.variable),
        }


# stage04


class StringNode(ASTNode):
    def __init__(self, value):
        super().__init__("String")
        self.value = proccess_str_literal(value)  # 字符串值

    def to_dict(self):
        return {"ClassName": "String", "value": self.value}


def proccess_str_literal(raw_content):
    return (
        raw_content.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\\\", "\\")
    )




class ArrayNode(ASTNode):
    def __init__(self, elements: List[ASTNode]):
        super().__init__("Array")
        self.elements = elements  # 数组元素列表

    def to_dict(self):
        return {
            "ClassName": "Array",
            "elements": [try_to_dict(element) for element in self.elements],
        }


class ArrayItemNode(ASTNode):
    def __init__(self, array: VariableNode, index: ASTNode):
        super().__init__("ArrayItem")
        self.array = array  # 数组名
        self.index = index  # 索引

    def to_dict(self):
        return {
            "ClassName": "ArrayItem",
            "array": try_to_dict(self.array),
            "index": try_to_dict(self.index),
        }


class StructTypeNode(ASTNode):
    def __init__(self, type_name: str):
        super().__init__("StructType")
        self.type_name = type_name  # 类型名称

    def to_dict(self):
        return {"ClassName": "StructType", "type_name": self.type_name}





class StructLiteralNode(ASTNode):
    def __init__(self, struct_type: TypeNode, body: List[ASTNode]):
        super().__init__("StructLiteral")
        self.struct_type = struct_type  # 结构体类型
        self.body = body  # 结构体成员列表

    def to_dict(self):
        return {
            "ClassName": "StructLiteral",
            "struct_type": try_to_dict(self.struct_type),
            "body": [try_to_dict(stat) for stat in self.body],
        }


class ObjectFieldNode(ASTNode):
    def __init__(self, variable: VariableNode, field: str):
        super().__init__("ObjectField")
        self.object = variable  # 对象
        self.field = field  # 字段名

    def to_dict(self):
        return {
            "ClassName": "ObjectField",
            "object": try_to_dict(self.object),
            "field": self.field,
        }


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





# class ExpressionNode(ASTNode):
#     def __init__(self, type, left, right):
#         super().__init__(type)  # 节点类型
#         self.left = left  # 左表达式
#         self.right = right  # 右表达式

#     def to_dict(self):
#         return {}
