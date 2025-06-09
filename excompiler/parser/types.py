from llvmlite import ir
from typing import override, List
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
    @abstractmethod
    def get_ir_type(self, *args) -> ir.Type:
        raise NotImplementedError(
            f"Subclasses {self.__class__.__name__} should implement this method"
        )

    @abstractmethod
    def store(
        self,
        builder: ir.IRBuilder,
        ir_var_ptr: ir.Value | ir.AllocaInstr,
        value: "ExpressionNode",
    ):
        raise NotImplementedError(
            f"Subclasses {self.__class__.__name__} should implement this method"
        )


class ExpressionNode(ASTNode):
    value_type: TypeNode

    def get_value_type(self) -> TypeNode:
        return self.value_type

    def get_ir_type(self) -> ir.Type:
        return self.get_value_type().get_ir_type()

    @abstractmethod
    def get_ir_value(self, builder: ir.IRBuilder) -> ir.Value:
        raise NotImplementedError(
            f"Subclasses {self.__class__.__name__} should implement this method"
        )


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


## built-in types
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
        raise NotImplementedError(
            f"Subclasses {self.__class__.__name__} should implement this method"
        )


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
            return self.base.get_ir_type().as_pointer()

    def store(
        self,
        builder: ir.IRBuilder,
        ir_var_ptr: ir.Value | ir.AllocaInstr,
        value: ExpressionNode,
    ):
        builder.store(value.get_ir_value(builder), ir_var_ptr)


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

    def get_ir_type(self):
        array_type = ir.ArrayType(self.base.codegen(), int(self.size.value))
        return array_type

    def store(
        self,
        builder: ir.IRBuilder,
        ir_var_ptr: ir.Value | ir.AllocaInstr,
        value: ExpressionNode,
    ):
        assert (value.__class__.__name__ == 'ArrayNode'), "Value must be an array"
        assert int(self.size.value) >= len(value.elements), "Array size too small"
        for i, element in enumerate(value.elements):
            builder.store(
                element.codegen(),
                builder.gep(
                    ir_var_ptr,
                    [ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), i)],
                    inbounds=True,
                ),
            )


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
