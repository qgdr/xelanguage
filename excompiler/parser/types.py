from llvmlite import ir
from typing import List  # 导入List类型注解
from abc import abstractmethod


module: ir.Module


class ASTNode:
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


class TypeNode(ASTNode):
    pass


class ExpressionNode(ASTNode):
    value_type: TypeNode
    pass


class IntegerNode(ExpressionNode):
    def __init__(self, value: str | int):
        self.value = value  # 整数值

    def to_dict(self):
        return {"ClassName": "Integer", "value": self.value}

    def codegen(self):
        return ir.Constant(ir.IntType(32), int(self.value))


class AtomicTypeNode(ASTNode):
    pass


class IntTypeNode(AtomicTypeNode):
    def __init__(self, bits: int):
        self.bits = bits

    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "type": f"i{self.bits}"}

    def codegen(self):
        return ir.IntType(self.bits)


class FloatTypeNode(AtomicTypeNode):
    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "type": "float"}

    def codegen(self):
        return ir.FloatType(self.bits)


### 指针类型节点
class PointerTypeNode(TypeNode):
    def __init__(self, base: TypeNode):
        self.base = base  # 基础类型

    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "base": try_to_dict(self.base)}

    def codegen(self):
        return self.base.codegen().as_pointer()


class ArrayTypeNode(TypeNode):
    def __init__(self, base: ASTNode, size: IntegerNode):
        self.base = base  # 基础类型
        self.size = size  # 数组大小

    def to_dict(self):
        return {
            "ClassName": self.__class__.__name__,
            "base": try_to_dict(self.base),
            "size": try_to_dict(self.size),
        }

    def codegen(self):
        array_type = ir.ArrayType(self.base.codegen(), int(self.size.value))
        return array_type


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


class StructTypeNode(TypeNode):
    def __init__(
        self, module: ir.Module, name: str, var_type_pairs: List[VarTypePairNode]
    ):
        self.module = module
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

    def field_idx(self, field_name: str) -> int:
        return self.field_names.index(field_name)

    def codegen(self):
        identified_types = self.module.get_identified_types()
        if self.name in identified_types:
            return identified_types[self.name]
        else:
            ctx = self.module.context
            struct_type = ctx.get_identified_type(self.name)
            struct_type.set_body(
                *[field_type.codegen() for field_type in self.field_types]
            )


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
