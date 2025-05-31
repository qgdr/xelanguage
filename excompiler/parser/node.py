# import json
from llvmlite import ir


# AST节点基类
class ASTNode:
    def __init__(self, type):
        self.type = type  # 节点类型
        self.is_leaf = False  # 是否为叶子节点
        # self.body = []


## 基本数据类型
class BasicTypeNode(ASTNode):
    def __init__(self, name):
        super().__init__("BasicType")
        self.name = name  # 类型名称
        self.is_leaf = True  # 是叶子节点

    def to_dict(self):
        return {"type": "BasicType", "name": self.name}


### 指针类型节点
class PointerTypeNode(ASTNode):
    def __init__(self, base):
        super().__init__("PointerType")
        self.base = base  # 基础类型

    def to_dict(self):
        return {"type": "PointerType", "base": self.base.to_dict()}


## 基本数据
class IdentifierNode(ASTNode):
    def __init__(self, name):
        super().__init__("Identifier")  # 节点类型为Identifier
        self.name = name  # 标识符名称
        self.is_leaf = True  # 是叶子节点

    def to_dict(self):
        return {"type": "Identifier", "name": self.name}


class IntegerNode(ASTNode):
    def __init__(self, value):
        super().__init__("Integer")  # 节点类型为Integer
        self.value = value  # 整数值
        self.is_leaf = True  # 是叶子节点

    def to_dict(self):
        return {"type": "Integer", "value": self.value}


class FloatNode(ASTNode):
    def __init__(self, value):
        super().__init__("Float")  # 节点类型为Float
        self.value = value  # 浮点数值
        self.is_leaf = True  # 是叶子节点

    def to_dict(self):
        return {"type": "Float", "value": self.value}


### var@
class NamedVarPointerNode(ASTNode):
    def __init__(self, base_var):
        super().__init__("NamedVarPointer")
        self.base_var = base_var  # 基础类型

    def to_dict(self):
        return {
            "type": "NamedVarPointer",
            "base_var": try_to_dict(self.base_var),
        }

### ptr#
# class NamedPointerDerefNode(ASTNode):
    

# 起点
class ProgramNode(ASTNode):
    def __init__(self, function):
        super().__init__("Program")
        self.body = [function]
        # self.is_leaf = False  # 是否为叶子节点

    def to_dict(self):
        Prog = {
            "type": "Program",
            "body": [
                node.to_dict() if node is not None else NotImplementedError()
                for node in self.body
            ],
        }
        return Prog


class FunctionNode(ASTNode):
    def __init__(self, name, params, return_type, block):
        super().__init__("Function")
        self.name = name  # 函数名
        if return_type is None:
            assert name == 'main', "funcation must have return type this stage, except \"main\" function"
            return_type = BasicTypeNode("i32")  # 默认返回类型为i32
            
        self.return_type = return_type  # 返回类型
        self.params = params  # 参数列表
        self.body = block.body  # 函数体

    def to_dict(self):
        Function = {
            "type": "Function",
            "name": self.name,
            "params": [try_to_dict(param) for param in self.params],
            "body": [try_to_dict(stat) for stat in self.body],
        }
        return Function



class VarTypePairNode(ASTNode):
    def __init__(self, name, var_type):
        super().__init__("VarTypePair")
        self.name = name  # 参数名
        self.var_type = var_type  # 参数类型

    def to_dict(self):
        return {
            "type": "VarTypePair",
            "name": self.name,
            "var_type": self.var_type.to_dict(),
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


## expression


class BlockNode(ASTNode):
    def __init__(self, statements):
        super().__init__("Block")
        self.body = statements  # 语句列表

    def to_dict(self):
        Block = {"type": "block", "body": [node.to_dict() for node in self.body]}
        return Block


## statement


class ReturnStatementNode(ASTNode):
    def __init__(self, expression):
        super().__init__("ReturnStatement")
        self.value = expression  # 返回表达式

    def to_dict(self):
        return {"type": "ReturnStatement", "value": self.value.to_dict()}



class VariableDeclarationNode(ASTNode):
    def __init__(self, name, var_type, equal_or_move, value=None):
        super().__init__("VariableDeclaration")
        self.variable = VarTypePairNode(name, var_type)  # 变量名和类型
        self.equal_or_move = equal_or_move
        self.value = value  # 可选的初始值

    def to_dict(self):
        return {
            "type": "VariableDeclaration",
            "variable": try_to_dict(self.variable),
            "equal_or_move": self.equal_or_move,
            "value": try_to_dict(self.value),
        }


class VarEqualNode(ASTNode):
    def __init__(self, name, value):
        super().__init__("VarEqual")
        self.name = name  # 变量名
        self.value = value  # 赋值表达式

    def to_dict(self):
        return {
            "type": "VarEqual",
            "name": self.name,
            "value": try_to_dict(self.value),
        }


class PtrDerefMoveNode(ASTNode):
    def __init__(self, name, value):
        super().__init__("PtrDerefMove")
        self.name = name  # 变量名
        self.value = value  # 赋值表达式

    def to_dict(self):
        return {
            "type": "PtrDerefMove",
            "name": self.name.to_dict(),
            "value": try_to_dict(self.value),
        }


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
    except AttributeError:
        return "\033[31mError: Node does not have to_dict method\033[0m"


# class ExpressionNode(ASTNode):
#     def __init__(self, type, left, right):
#         super().__init__(type)  # 节点类型
#         self.left = left  # 左表达式
#         self.right = right  # 右表达式

#     def to_dict(self):
#         return {}
