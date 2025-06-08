import os
import subprocess
from llvmlite import ir, binding as llvm
from llvmlite.binding import Target
from parser.node import *

# from parser.node import (
#     # stage01
#     # ModuleNode,
#     FunctionNode,
#     IntegerNode,
#     FloatNode,
#     BooleanNode,
#     VariableNode,
#     TypeNode,
#     UnaryExpressionNode,
#     BinaryExpressionNode,
#     ReturnStatementNode,
#     VariableDeclarationNode,
#     VarEqualNode,
#     CallExpressionNode,
#     # stage02
#     PointerTypeNode,
#     NamedVarPointerNode,
#     PtrDerefEqualNode,
# )


class LLVMCodeGen:
    # 替换初始化方式

    def __init__(self):
        llvm.initialize()
        llvm.initialize_native_target()
        llvm.initialize_native_asmprinter()

        # auto detect target
        self.target = Target.from_default_triple()
        self.module = ir.Module()
        self.module.triple = self.target.triple

        # self.builder = None

    def generate(self, prog, module_name):
        # 生成LLVM IR逻辑
        self.module.name = module_name
        for module_item in prog.body:
            module_item.codegen(self.module)
        return str(self.module)

    def compile_to_executable(self, ir_file_path, output_file):
        # 生成目标文件
        # llvm_module = llvm.parse_assembly(str(self.module))
        # target_machine = self.target.create_target_machine()

        # 生成对象文件
        # obj_data = target_machine.emit_object(llvm_module)
        # obj_path = output_file.with_suffix(".o")
        # with open(obj_path, "wb") as f:
        #     f.write(obj_data)

        # 链接为可执行文件
        linker = "clang" if os.name != "nt" else "clang-cl"
        if subprocess.run([linker, ir_file_path, "-o", output_file]).returncode != 0:
            print("Failed to generate executable")
            exit(1)


# IRType = {
#     "i32": ir.IntType(32),
#     "f32": ir.FloatType(),
#     "bool": ir.IntType(1),
#     "ptr": ir.PointerType(),  # 假设指针类型为指向i32的指针
# }


def ReturnStatement_codegen(self: ReturnStatementNode):
    global builder
    builder.ret(self.value.codegen())


setattr(ReturnStatementNode, "codegen", ReturnStatement_codegen)


def VariableDeclaration_codegen(self: VariableDeclarationNode):
    global builder, symbol_table_stack
    if self.value is None:
        raise NotImplementedError("Variable declaration without initial value")
    var_type = self.variable.var_type.codegen()
    variable = builder.alloca(var_type, name=self.variable.name)
    if isinstance(self.variable.var_type, TypeNode):
        builder.store(self.value.codegen(), variable)
        setattr(variable, "alloca", True)  # 标记为分配的变量

    elif isinstance(self.variable.var_type, PointerTypeNode):
        builder.store(self.value.codegen(), variable)
        setattr(variable, "alloca", True)  # 标记为分配的变量

    elif isinstance(self.variable.var_type, ArrayTypeNode):
        assert isinstance(self.value, ArrayNode), "Value must be an array"
        assert int(self.variable.var_type.size.value) >= len(self.value.elements), (
            "Array size too small"
        )
        for i, element in enumerate(self.value.elements):
            builder.store(
                element.codegen(),
                builder.gep(
                    variable,
                    [ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), i)],
                    inbounds=True,
                ),
            )
        setattr(variable, "alloca", False)  # 标记为分配的变量

        # variable = builder.bitcast(variable, ir.PointerType(var_type))
    elif isinstance(self.variable.var_type, StructTypeNode):
        symbol_table = self.value.codegen()
        if not isinstance(symbol_table, SymbolTable):
            raise TypeError("Value must be a SymbolTable_codegen instance for struct")
        for i, key in enumerate(symbol_table.symbols):
            builder.store(
                symbol_table.symbols[key],
                builder.gep(
                    variable,
                    [ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), i)],
                    inbounds=True,
                ),
            )
    else:
        raise NotImplementedError(f"Unsupported type: {type(self.variable.var_type)}")

    symbol_table_stack.add(self.variable.name, variable)


setattr(VariableDeclarationNode, "codegen", VariableDeclaration_codegen)


def VarEqual_codegen(self: VarEqualNode):
    global builder, symbol_table_stack
    variable = symbol_table_stack.get(self.variable.name)
    builder.store(self.value.codegen(), variable)


setattr(VarEqualNode, "codegen", VarEqual_codegen)


def CallExpression_codegen(self: CallExpressionNode):
    global builder
    # 处理函数调用表达式
    function = builder.module.get_global(self.function_name)
    if not isinstance(function, ir.Function):
        raise ValueError(f"Function '{self.function_name}' not found in module.")
    args = [arg.codegen() for arg in self.args]
    call = builder.call(function, args)
    return call


setattr(CallExpressionNode, "codegen", CallExpression_codegen)


# stage02


def NamedVarPointer_codegen(self: NamedVarPointerNode):
    global builder, symbol_table_stack
    # 处理指向变量的指针
    var_ptr = symbol_table_stack.get(self.name)
    if not var_ptr.alloca:
        raise ValueError(f"Variable '{self.name}' is not allocated.")
    # print(var_ptr.type.pointee)
    return var_ptr


setattr(NamedVarPointerNode, "codegen", NamedVarPointer_codegen)


def PtrDerefEqual_codegen(self: PtrDerefEqualNode):
    global builder, symbol_table_stack
    # 处理指针解引用赋值
    var_ptr = self.variable.codegen()
    value = self.value.codegen()
    builder.store(value, var_ptr)


setattr(PtrDerefEqualNode, "codegen", PtrDerefEqual_codegen)


def PtrDeref_codegen(self: PtrDerefNode):
    global builder, symbol_table_stack
    # 处理指针解引用
    var_ptr = self.variable.codegen()
    # print(var_ptr.type)
    return builder.load(var_ptr)


setattr(PtrDerefNode, "codegen", PtrDeref_codegen)


# stage04


def String_codegen(self: StringNode):
    global builder
    # 处理字符串节点
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


setattr(StringNode, "codegen", String_codegen)

import hashlib


def get_hash_name(s: str) -> str:
    hash_val = hashlib.md5(s.encode()).hexdigest()  # 生成128位哈希
    return f"str_{hash_val[:8]}"  # 取前8位作为短名称[6](@ref)


def ArrayItem_codegen(self: ArrayItemNode):
    global builder, symbol_table_stack
    # 处理数组项
    array_ptr = self.array.codegen()
    index = self.index.codegen()
    # print(array_ptr, index)
    return builder.load(builder.gep(array_ptr, [ir.Constant(ir.IntType(32), 0), index]))


setattr(ArrayItemNode, "codegen", ArrayItem_codegen)


## struct
def StructType_codegen(self: StructTypeNode):
    global builder
    return builder.module.get_identified_types()[self.type_name]


setattr(StructTypeNode, "codegen", StructType_codegen)


def StructTypeDef_codegen(self: StructTypeDefNode, module: ir.Module):
    global builder, symbol_table_stack
    # 处理结构体类型定义
    ctx = ir.context.global_context
    struct_type = ctx.get_identified_type(self.name)
    struct_type.set_body(
        *[var_type_pair.var_type.codegen() for var_type_pair in self.struct_fields]
    )


setattr(StructTypeDefNode, "codegen", StructTypeDef_codegen)


def StructLiteral_codegen(self: StructLiteralNode):
    global builder, symbol_table_stack
    symbol_table_stack.push()
    for stat in self.body:
        if not isinstance(stat, VarEqualNode):
            raise NotImplementedError(f"Unsupported statement: {type(stat)}")
        symbol_table_stack.add(stat.variable.name, stat.value.codegen())
    return symbol_table_stack.pop()


setattr(StructLiteralNode, "codegen", StructLiteral_codegen)

# def PtrDerefMove_codegen(self):
#     if isinstance(self.value, IntegerNode):
#         variable = builder.module.get_global(self.name.name)
#         variable.initializer = ir.Constant(ir.IntType(32), int(self.value.value, 0))
#     else:
#         raise NotImplementedError(f"Unsupported assignment type: {self.value.type}")


# setattr(PtrDerefMoveNode, "codegen", PtrDerefMove_codegen)
