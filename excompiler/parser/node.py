# import json
from abc import abstractmethod
from typing import List


class ASTNode:
    def __init__(self, node_type):
        self.node_type = node_type  # 节点类型

    @abstractmethod
    def to_dict(
        self,
    ):
        """
        将节点转换为字典格式
        :return: 字典表示的节点
        """
        raise NotImplementedError("Subclasses should implement this method")

    @abstractmethod
    def codegen(self, *args):
        """
        生成LLVM IR代码
        :param builder: LLVM IR构建器
        """
        raise NotImplementedError("Subclasses should implement this method")


# stage01


class ModuleNode(ASTNode):
    def __init__(self, functions):
        super().__init__("Module")
        self.body = functions  # 函数列表

    def to_dict(self):
        Module = {
            "NodeClass": "Module",
            "body": [try_to_dict(node) for node in self.body],
        }
        return Module


class TypeNode(ASTNode):
    def __init__(self, type_name: str):
        super().__init__("Type")
        self.type_name = type_name  # 类型名称

    def to_dict(self):
        return {"NodeClass": "Type", "type_name": self.type_name}


class VariableNode(ASTNode):
    def __init__(self, name: str):
        super().__init__("Variable")  # 节点类型为Variable
        self.name = name  # 变量名称

    def to_dict(self):
        return {"NodeClass": "Variable", "name": self.name}


class IdentifierNode(ASTNode):
    def __init__(self, name: str):
        super().__init__("Identifier")  # 节点类型为Identifier
        self.name = name  # 标识符名称

    def to_dict(self):
        # return {"NodeClass": "Identifier", "name": self.name}
        raise NotImplementedError(
            "IdentifierNode does not support to_dict method. Use VariableNode instead."
        )


class IntegerNode(ASTNode):
    def __init__(self, value: str):
        super().__init__("Integer")  # 节点类型为Integer
        self.value = value  # 整数值

    def to_dict(self):
        return {"NodeClass": "Integer", "value": self.value}


class FloatNode(ASTNode):
    def __init__(self, value):
        super().__init__("Float")  # 节点类型为Float
        self.value = value  # 浮点数值

    def to_dict(self):
        return {"NodeClass": "Float", "value": self.value}


class BooleanNode(ASTNode):
    def __init__(self, value):
        super().__init__("Boolean")  # 节点类型为Boolean
        self.value = value  # 布尔值

    def to_dict(self):
        return {"NodeClass": "Boolean", "value": self.value}


class VarTypePairNode(ASTNode):
    def __init__(self, name: str, var_type: TypeNode):
        super().__init__("VarTypePair")
        self.name = name  # 参数名
        self.var_type = var_type  # 参数类型

    def to_dict(self):
        return {
            "NodeClass": "VarTypePair",
            "name": self.name,
            "var_type": try_to_dict(self.var_type),
        }


class BlockNode(ASTNode):
    def __init__(self, statements: List[ASTNode]):
        super().__init__("Block")
        self.body = statements  # 语句列表

    def to_dict(self):
        Block = {
            "NodeClass": "block",
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
            "NodeClass": "Function",
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
            "NodeClass": "BinaryExpression",
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
            "NodeClass": "UnaryExpression",
            "operator": self.operator,
            "value": try_to_dict(self.value),
        }


class ReturnStatementNode(ASTNode):
    def __init__(self, expression):
        super().__init__("ReturnStatement")
        self.value = expression  # 返回表达式

    def to_dict(self):
        return {"NodeClass": "ReturnStatement", "value": self.value.to_dict()}


class VariableDeclarationNode(ASTNode):
    def __init__(self, var_type_pair: VarTypePairNode, equal_or_move: str, value):
        super().__init__("VariableDeclaration")
        self.variable = var_type_pair  # 变量名和类型
        self.equal_or_move = equal_or_move
        self.value = value  # 可选的初始值

    def to_dict(self):
        return {
            "NodeClass": "VariableDeclaration",
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
            "NodeClass": "VarEqual",
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
            "NodeClass": "CallExpression",
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
            "NodeClass": "NamedVarPointer",
            "name": self.name,
        }


### 指针类型节点
class PointerTypeNode(ASTNode):
    def __init__(self, base: TypeNode):
        super().__init__("PointerType")
        self.base = base  # 基础类型

    def to_dict(self):
        return {"NodeClass": "PointerType", "base": self.base.to_dict()}


class PtrDerefEqualNode(ASTNode):
    def __init__(self, variable: VariableNode, value: ASTNode):
        super().__init__("PtrDerefEqual")
        self.variable = variable  # 变量名
        self.value = value  # 赋值表达式

    def to_dict(self):
        return {
            "NodeClass": "PtrDerefEqual",
            "variable": try_to_dict(self.variable),
            "value": try_to_dict(self.value),
        }


class PtrDerefNode(ASTNode):
    def __init__(self, variable: VariableNode):
        super().__init__("PtrDeref")
        self.variable = variable  # 变量名

    def to_dict(self):
        return {
            "NodeClass": "PtrDeref",
            "variable": try_to_dict(self.variable),
        }


# stage04


class StringNode(ASTNode):
    def __init__(self, value):
        super().__init__("String")
        self.value = proccess_str_literal(value)  # 字符串值

    def to_dict(self):
        return {"NodeClass": "String", "value": self.value}


def proccess_str_literal(raw_content):
    return (
        raw_content.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\\\", "\\")
    )


class ArrayTypeNode(ASTNode):
    def __init__(self, base: ASTNode, size: IntegerNode):
        super().__init__("ArrayType")
        self.base = base  # 基础类型
        self.size = size  # 数组大小

    def to_dict(self):
        return {
            "NodeClass": "ArrayType",
            "base": try_to_dict(self.base),
            "size": try_to_dict(self.size),
        }


class ArrayNode(ASTNode):
    def __init__(self, elements: List[ASTNode]):
        super().__init__("Array")
        self.elements = elements  # 数组元素列表

    def to_dict(self):
        return {
            "NodeClass": "Array",
            "elements": [try_to_dict(element) for element in self.elements],
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


def try_to_dict(node):
    """尝试将节点转换为字典格式"""
    try:
        return node.to_dict()
    except AttributeError as e:
        return f"\033[31mError: Node {type(node).__name__} to_dict error {node.name}\033[0m"


# class ExpressionNode(ASTNode):
#     def __init__(self, type, left, right):
#         super().__init__(type)  # 节点类型
#         self.left = left  # 左表达式
#         self.right = right  # 右表达式

#     def to_dict(self):
#         return {}
