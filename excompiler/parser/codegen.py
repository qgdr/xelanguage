import os
import subprocess
from symtable import SymbolTable
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
        global symbol_table_stack_codegen
        self.module.name = module_name
        symbol_table_stack_codegen.push()
        for module_item in prog.body:
            # if not isinstance(function, FunctionNode):
            #     raise TypeError(f"Expected FunctionNode, got {type(function)}")
            module_item.codegen(self.module)
        symbol_table_stack_codegen.pop()
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


# 符号栈
class SymbolTable_codegen:
    def __init__(self):
        self.symbols = {}

    def add(self, name, ir_local_var):
        if name in self.symbols:
            raise ValueError(f"Symbol '{name}' already exists.")
        self.symbols[name] = ir_local_var

    def get(self, name):
        if name not in self.symbols:
            raise KeyError(f"Symbol '{name}' not found.")
        return self.symbols.get(name)

    def __contains__(self, name):
        return name in self.symbols

    def __repr__(self):
        return f"SymbolTable({self.symbols})"


class SymbolTableStack_codegen:
    def __init__(self):
        self.stack = []

    def push(self):
        self.stack.append(SymbolTable_codegen())

    def pop(self) -> SymbolTable_codegen:
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack.pop()

    def current(self) -> SymbolTable_codegen:
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack[-1]

    def add(self, name: str, ir_local_var):
        if name in self.current():
            raise ValueError(
                f"Symbol '{name}' already exists in the current symbol table."
            )
        # 在当前符号表中添加变量
        self.current().add(name, ir_local_var)

    def get(self, name):
        for table in reversed(self.stack):
            if name in table:
                return table.get(name)
        raise KeyError(f"Symbol '{name}' not found in any symbol table.")


symbol_table_stack_codegen = SymbolTableStack_codegen()
builder: ir.IRBuilder = None  # type: ignore # 全局IR构建器

# IRType = {
#     "i32": ir.IntType(32),
#     "f32": ir.FloatType(),
#     "bool": ir.IntType(1),
#     "ptr": ir.PointerType(),  # 假设指针类型为指向i32的指针
# }


## codegen for AST nodes
def Variable_codegen(self: VariableNode):
    global builder
    # 处理变量节点
    var = symbol_table_stack_codegen.get(self.name)
    if not var.alloca:
        return var
    else:
        return builder.load(var)


setattr(VariableNode, "codegen", Variable_codegen)


# 暂时只支持整数的二元表达式
def BinaryExpression_codegen(self: BinaryExpressionNode):
    global builder
    left = self.left.codegen()
    right = self.right.codegen()

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


setattr(BinaryExpressionNode, "codegen", BinaryExpression_codegen)


def UnaryExpression_codegen(self):
    global builder
    # 处理一元表达式
    value = self.value.codegen()
    if self.operator == "-":
        return builder.neg(value)
    elif self.operator == "+":
        return value
    elif self.operator == "not":
        return builder.not_(value)
    else:
        raise NotImplementedError(f"Unsupported operator: {self.operator}")


setattr(UnaryExpressionNode, "codegen", UnaryExpression_codegen)


def Integer_codegen(self: IntegerNode):
    # 处理整数节点
    return ir.Constant(ir.IntType(32), int(self.value, 0))


setattr(IntegerNode, "codegen", Integer_codegen)


def Float_codegen(self: FloatNode):
    # 处理浮点数节点
    return ir.Constant(ir.FloatType(), float(self.value))


setattr(FloatNode, "codegen", Float_codegen)


def Boolean_codegen(self: BooleanNode):
    # 处理布尔值节点
    return ir.Constant(ir.IntType(1), 1 if self.value == "true" else 0)


setattr(BooleanNode, "codegen", Boolean_codegen)


def ReturnStatement_codegen(self: ReturnStatementNode):
    global builder
    builder.ret(self.value.codegen())


setattr(ReturnStatementNode, "codegen", ReturnStatement_codegen)


def VariableDeclaration_codegen(self: VariableDeclarationNode):
    global builder, symbol_table_stack_codegen
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
        if not isinstance(symbol_table, SymbolTable_codegen):
            raise TypeError(
                "Value must be a SymbolTable_codegen instance for struct"
            )
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

    symbol_table_stack_codegen.add(self.variable.name, variable)


setattr(VariableDeclarationNode, "codegen", VariableDeclaration_codegen)


def VarEqual_codegen(self: VarEqualNode):
    global builder, symbol_table_stack_codegen
    variable = symbol_table_stack_codegen.get(self.variable.name)
    builder.store(self.value.codegen(), variable)


setattr(VarEqualNode, "codegen", VarEqual_codegen)


def Type_codegen(self: TypeNode):
    match self.type_name:
        case "i32":
            return ir.IntType(32)
        case "f32":
            return ir.FloatType()
        case "bool":
            return ir.IntType(1)
        case "str":
            return ir.IntType(8)
        case _:
            raise NotImplementedError(f"Unsupported type: {self.type_name}")


setattr(TypeNode, "codegen", Type_codegen)


def Function_codegen(self: FunctionNode, module: ir.Module):
    global builder, symbol_table_stack_codegen
    # 处理函数节点
    func_type = ir.FunctionType(
        self.return_type.codegen(),
        [var_type_pair.var_type.codegen() for var_type_pair in self.args],
    )
    function = ir.Function(module, func_type, name=self.name)
    entry_block = function.append_basic_block("entry")
    builder = ir.IRBuilder(entry_block)

    symbol_table_stack_codegen.push()  # Push a new symbol table for the function
    # 将参数添加到符号表
    for i, arg in enumerate(function.args):
        setattr(arg, "alloca", False)  # 标记为分配的参数
        symbol_table_stack_codegen.add(self.args[i].name, arg)  # 添加到符号表
    # 处理函数体
    for stat in self.body:
        stat.codegen()

    # 检查块是否已终止
    if not isinstance(builder.block, ir.Block):
        raise ValueError("Builder block is not an instance of ir.Block.")
    if not builder.block.is_terminated:
        builder.ret(ir.Constant(ir.IntType(32), 0))

    symbol_table_stack_codegen.pop()  # Pop the symbol table for the function


setattr(FunctionNode, "codegen", Function_codegen)


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


def PointerType_codegen(self: PointerTypeNode):
    # 处理指针类型
    return self.base.codegen().as_pointer()
    # return ir.PointerType()


setattr(PointerTypeNode, "codegen", PointerType_codegen)


def NamedVarPointer_codegen(self: NamedVarPointerNode):
    global builder, symbol_table_stack_codegen
    # 处理指向变量的指针
    var_ptr = symbol_table_stack_codegen.get(self.name)
    if not var_ptr.alloca:
        raise ValueError(f"Variable '{self.name}' is not allocated.")
    # print(var_ptr.type.pointee)
    return var_ptr


setattr(NamedVarPointerNode, "codegen", NamedVarPointer_codegen)


def PtrDerefEqual_codegen(self: PtrDerefEqualNode):
    global builder, symbol_table_stack_codegen
    # 处理指针解引用赋值
    var_ptr = self.variable.codegen()
    value = self.value.codegen()
    builder.store(value, var_ptr)


setattr(PtrDerefEqualNode, "codegen", PtrDerefEqual_codegen)


def PtrDeref_codegen(self: PtrDerefNode):
    global builder, symbol_table_stack_codegen
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


def ArrayType_codegen(self: ArrayTypeNode):
    global builder
    # 处理数组类型
    array_type = ir.ArrayType(self.base.codegen(), int(self.size.value))
    return array_type
    # return self.base.codegen().as_pointer()


setattr(ArrayTypeNode, "codegen", ArrayType_codegen)


def ArrayItem_codegen(self: ArrayItemNode):
    global builder, symbol_table_stack_codegen
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
    global builder, symbol_table_stack_codegen
    # 处理结构体类型定义
    # struct_type = ir.IdentifiedStructType(module.context, name=self.name)
    ctx = ir.context.global_context
    struct_type = ctx.get_identified_type(self.name)
    struct_type.set_body(
        *[var_type_pair.var_type.codegen() for var_type_pair in self.struct_fields]
    )
    # symbol_table_stack_codegen.add(self.name, struct_type)
    # return struct_type


setattr(StructTypeDefNode, "codegen", StructTypeDef_codegen)


def StructLiteral_codegen(self: StructLiteralNode):
    global builder, symbol_table_stack_codegen
    symbol_table_stack_codegen.push()
    for stat in self.body:
        if not isinstance(stat, VarEqualNode):
            raise NotImplementedError(f"Unsupported statement: {type(stat)}")
        symbol_table_stack_codegen.add(stat.variable.name, stat.value.codegen())
    return symbol_table_stack_codegen.pop()


setattr(StructLiteralNode, "codegen", StructLiteral_codegen)

# def PtrDerefMove_codegen(self):
#     if isinstance(self.value, IntegerNode):
#         variable = builder.module.get_global(self.name.name)
#         variable.initializer = ir.Constant(ir.IntType(32), int(self.value.value, 0))
#     else:
#         raise NotImplementedError(f"Unsupported assignment type: {self.value.type}")


# setattr(PtrDerefMoveNode, "codegen", PtrDerefMove_codegen)
