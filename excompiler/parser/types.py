from llvmlite import ir
from typing import List  # 导入List类型注解
from abc import abstractmethod


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


class TypeNode(ASTNode):
    def get_ir_type(self) -> ir.Type:
        raise NotImplementedError(
            f"Subclasses {self.__class__.__name__} should implement this method"
        )


class ExpressionNode(ASTNode):
    value_type: TypeNode

    @abstractmethod
    def get_value_type(self, builder: ir.IRBuilder) -> TypeNode:
        return self.value_type

    @abstractmethod
    def get_ir_value(self) -> ir.Value:
        raise NotImplementedError(
            f"Subclasses {self.__class__.__name__} should implement this method"
        )

    def get_ir_type(self, builder: ir.IRBuilder) -> ir.Type:
        return self.get_value_type(builder).get_ir_type()


class StatementNode(ASTNode):
    @abstractmethod
    def codegen(self, *args):
        raise NotImplementedError(
            f"Subclasses {self.__class__.__name__} should implement this method"
        )


class IntegerNode(ExpressionNode):
    def __init__(self, value: str | int):
        self.value_type = IntTypeNode(32)
        self.value = value  # 整数值

    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "value": self.value}

    def get_ir_value(self):
        return ir.Constant(ir.IntType(32), int(self.value))


class AtomicTypeNode(TypeNode):
    pass


class IntTypeNode(AtomicTypeNode):
    def __init__(self, bits: int):
        self.bits = bits

    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "type": f"i{self.bits}"}

    def get_ir_type(self):
        return ir.IntType(self.bits)


class FloatTypeNode(AtomicTypeNode):
    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "type": "float"}

    def get_ir_type(self):
        return ir.FloatType(self.bits)


class BoolTypeNode(AtomicTypeNode):
    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "type": "bool"}

    def get_ir_type(self):
        return ir.IntType(1)


class StrTypeNode(AtomicTypeNode):
    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "type": "str"}

    def get_ir_type(self):
        return ir.IntType(8).as_pointer()


### 指针类型节点
class PointerTypeNode(TypeNode):
    def __init__(self, base: TypeNode):
        self.base = base  # 基础类型

    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "base": try_to_dict(self.base)}

    def get_ir_type(self):
        if isinstance(self.base, StrTypeNode):
            return ir.PointerType(ir.IntType(8))
        else:
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


class IdentifiedTypeNode(TypeNode):
    def __init__(self, name: str):
        self.name = name

    def get_ir_type(self, module: ir.Module):
        identified_types = module.get_identified_types()
        if self.name in identified_types:
            return identified_types[self.name]
        else:
            raise ValueError(f"Type {self.name} not found")


class StructTypeNode(IdentifiedTypeNode):
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

    def field_idx(self, field_name: str) -> int:
        return self.field_names.index(field_name)

    def register_to_module(self, module: ir.Module):
        ctx = module.context
        struct_type = ctx.get_identified_type(self.name)
        struct_type.set_body(*[field_type.codegen() for field_type in self.field_types])
        return struct_type


class EnumTypeNode(IdentifiedTypeNode):
    pass


class NoneTypeNode(TypeNode):
    def to_dict(self):
        return {"ClassName": self.__class__.__name__}


class AnyTypeNode(TypeNode):
    def to_dict(self):
        return {"ClassName": self.__class__.__name__}


## expression


class FloatNode(ExpressionNode):
    def __init__(self, value):
        self.value_type = FloatTypeNode()
        self.value = value  # 浮点数值

    def to_dict(self):
        return {"ClassName": self.__class__.__name__, "value": self.value}

    def get_ir_value(self):
        return ir.Constant(ir.FloatType(), float(self.value))


class BooleanNode(ExpressionNode):
    def __init__(self, value):
        self.value_type = BoolTypeNode()
        self.value = value  # 布尔值

    def to_dict(self):
        return {"ClassName": "Boolean", "value": self.value}

    def get_ir_value(self):
        match self.value:
            case "true":
                return ir.Constant(ir.IntType(1), 1)
            case "false":
                return ir.Constant(ir.IntType(1), 0)
            case _:
                raise ValueError(f"Invalid boolean value: {self.value}")


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
        return f"Error: Node {type(node).__name__} to_dict error {node.name}\n" + str(e)
