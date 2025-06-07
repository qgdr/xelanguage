import os
import subprocess
from llvmlite import ir, binding as llvm
from llvmlite.binding import Target
from parser.node import *


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

        self.builder = None

    def generate(self, prog):
        # 生成LLVM IR逻辑
        # 现阶段只有一个函数 main
        assert prog.type == "Program"
        mainAST = prog.body[0]
        assert mainAST.type == "Function"
        assert mainAST.name == "main"

        symbol_table_stack.push()  # Push a new symbol table for the main function

        main_type = ir.FunctionType(ir.IntType(32), [])
        main_func = ir.Function(self.module, main_type, name="main")
        entry_block = main_func.append_basic_block("entry")
        self.builder = ir.IRBuilder(entry_block)

        # 返回值处理
        for statAST in mainAST.body:
            statAST.codegen(self.builder)

        # 检查块是否已终止
        if not self.builder.block.is_terminated:
            self.builder.ret(ir.Constant(ir.IntType(32), 0))

        symbol_table_stack.pop()  # Pop the symbol table for the main function
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
class SymbolTable:
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


class SymbolTableStack:
    def __init__(self):
        self.stack = []

    def push(self):
        self.stack.append(SymbolTable())

    def pop(self):
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack.pop()

    def current(self):
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack[-1]

    def add(self, name, ir_local_var):
        self.current().add(name, ir_local_var)

    def get(self, name):
        for table in reversed(self.stack):
            if name in table:
                return table.get(name)
        raise KeyError(f"Symbol '{name}' not found in any symbol table.")


symbol_table_stack = SymbolTableStack()


IRType = {
    "i32": ir.IntType(32),
    "f32": ir.FloatType(),
    "bool": ir.IntType(1),
    "ptr": ir.PointerType(),  # 假设指针类型为指向i32的指针
}



## codegen for AST nodes
# def Function_codegen(self, builder: ir.IRBuilder):
def Identifier_codegen(self, builder: ir.IRBuilder):
    # 处理标识符节点
    var_ptr = symbol_table_stack.get(self.name)
    return builder.load(var_ptr, typ=var_ptr.type.pointee)
setattr(IdentifierNode, "codegen", Identifier_codegen)


def BinaryExpression_codegen(self, builder: ir.IRBuilder):
    left = self.left.codegen(builder)
    right = self.right.codegen(builder)

    if self.operator == "+":
        return builder.add(left, right)
    elif self.operator == "-":
        return builder.sub(left, right)
    elif self.operator == "*":
        return builder.mul(left, right)
    elif self.operator == "/":
        return builder.sdiv(left, right)
    else:
        raise NotImplementedError(f"Unsupported operator: {self.op}")
setattr(BinaryExpressionNode, "codegen", BinaryExpression_codegen)



def ReturnStatement_codegen(self, builder: ir.IRBuilder):
    if self.value.type == "Integer":
        builder.ret(ir.Constant(ir.IntType(32), int(self.value.value)))
    # elif self.value.type == "Identifier":
    #     value_ptr = symbol_table_stack_codegen.get(self.value.name)
    #     builder.ret(builder.load(value_ptr, typ=value_ptr.type.pointee))
    elif self.value.type == "BinaryExpression":
        # 处理二元表达式
        builder.ret(self.value.codegen(builder))
    else:
        raise NotImplementedError(f"Unsupported return type: {self.value.type}")


setattr(ReturnStatementNode, "codegen", ReturnStatement_codegen)


def VariableDeclaration_codegen(self, builder: ir.IRBuilder):
    if self.value is None:
        raise NotImplementedError("Variable declaration without initial value")

    variable = None  # Ensure variable is always defined

    if not (
        isinstance(self.variable.var_type, BasicTypeNode)
        or isinstance(self.variable.var_type, PointerTypeNode)
    ):
        raise NotImplementedError(
            f"Unsupported variable declaration type: {self.variable.var_type.name}"
        )
    if isinstance(self.variable.var_type, BasicTypeNode):
        match self.variable.var_type.name:
            case "i32":
                assert isinstance(self.value, IntegerNode), (
                    f"Expected IntegerNode for i32, got {self.value.type}"
                )
                variable = builder.alloca(ir.IntType(32), name=self.variable.name)
                builder.store(
                    ir.Constant(ir.IntType(32), int(self.value.value, 0)),
                    variable,
                )

            case "f32":
                assert isinstance(self.value, FloatNode), (
                    f"Expected FloatNode for f32, got {self.value.type}"
                )
                variable = builder.alloca(ir.FloatType(), name=self.variable.name)
                builder.store(
                    ir.FloatType()(float(self.value.value)),
                    variable,
                )
            case "bool":
                assert self.value.name in ["true", "false"], (
                    f"Expected 'true' or 'false' for bool, got {self.value.name}"
                )
                variable = builder.alloca(ir.IntType(1), name=self.variable.name)
                builder.store(
                    ir.IntType(1)(1 if self.value.name == "true" else 0),
                    variable,
                )
            case _:
                raise NotImplementedError(
                    f"Unsupported type: {self.variable.var_type.name}"
                )

    elif isinstance(self.variable.var_type, PointerTypeNode):
        if isinstance(self.value, NamedVarPointerNode):
            assert self.variable.var_type.base.name in ["i32", "f32"], (
                f"Unsupported pointer base type: {self.variable.var_type.base.name}"
            )
            # 修复指针类型生成逻辑
            value_ptr = symbol_table_stack.get(self.value.base_var.name)
            assert ir.IntType(32).as_pointer() == value_ptr.type, (
                f"Expected pointer to i32, got {value_ptr.type}"
            )
            variable = builder.alloca(
                ir.PointerType(ir.IntType(32)), name=self.variable.name
            )
            builder.store(value_ptr, variable)

    if variable is None:
        raise NotImplementedError("Variable was not initialized in declaration.")
    symbol_table_stack.add(self.variable.name, variable)


setattr(VariableDeclarationNode, "codegen", VariableDeclaration_codegen)


def VarEqual_codegen(self, builder):
    if isinstance(self.value, IntegerNode):
        variable = builder.module.get_global(self.name)
        variable.initializer = ir.Constant(ir.IntType(32), int(self.value.value, 0))
    else:
        raise NotImplementedError(f"Unsupported assignment type: {self.value.type}")


setattr(VarEqualNode, "codegen", VarEqual_codegen)


def PtrDerefMove_codegen(self, builder):
    if isinstance(self.value, IntegerNode):
        variable = builder.module.get_global(self.name.name)
        variable.initializer = ir.Constant(ir.IntType(32), int(self.value.value, 0))
    else:
        raise NotImplementedError(f"Unsupported assignment type: {self.value.type}")


setattr(PtrDerefMoveNode, "codegen", PtrDerefMove_codegen)
