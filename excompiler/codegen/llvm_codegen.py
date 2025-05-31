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
        llvm.set_option("", "--opaque-pointers=1")  # 启用 opaque pointers
        llvm.initialize_native_asmprinter()

        # auto detect target
        self.target = Target.from_default_triple()
        self.module = ir.Module()

        self.builder = None

    def generate(self, prog):
        # 生成LLVM IR逻辑
        # 现阶段只有一个函数 main
        assert prog.type == "Program"
        mainAST = prog.body[0]
        assert mainAST.type == "Function"
        assert mainAST.name == "main"

        symbol_table_stack_codegen.push()  # Push a new symbol table for the main function

        main_type = ir.FunctionType(ir.IntType(32), [])
        main_func = ir.Function(self.module, main_type, name="main")
        entry_block = main_func.append_basic_block("entry")
        self.builder = ir.IRBuilder(entry_block)

        # 返回值处理
        for statAST in mainAST.body:
            statAST.codegen(self.builder)


        # 检测函数是否返回
        if not self.builder.block.terminator:
            self.builder.ret(ir.Constant(ir.IntType(32), 0))

        symbol_table_stack_codegen.pop()  # Pop the symbol table for the main function
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
        subprocess.run([linker, ir_file_path, "-o", output_file])


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


symbol_table_stack_codegen = SymbolTableStack_codegen()


## codegen for AST nodes
# def Function_codegen(self, builder: ir.IRBuilder):


def ReturnStatement_codegen(self, builder: ir.IRBuilder):
    if self.value.type == "Integer":
        builder.ret(ir.Constant(ir.IntType(32), int(self.value.value)))
    elif self.value.type == "Identifier":
        builder.ret(builder.load(symbol_table_stack_codegen.get(self.value.name)))
    else:
        raise NotImplementedError(f"Unsupported return type: {self.value.type}")


setattr(ReturnStatementNode, "codegen", ReturnStatement_codegen)


def VariableDeclaration_codegen(self, builder):
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
                    ir.IntType(32)(int(self.value.value, 0)),
                    variable,
                )

            case "f32":
                assert isinstance(self.value, FloatNode), (
                    f"Expected FloatNode for f32, got {self.value.type}"
                )
                variable = builder.alloca(ir.FloatType(), name=self.name)
                builder.store(
                    ir.FloatType()(float(self.value.value)),
                    variable,
                )
            case "bool":
                assert self.value.name in ["true", "false"], (
                    f"Expected 'true' or 'false' for bool, got {self.value.name}"
                )
                variable = builder.alloca(ir.IntType(1), name=self.name)
                builder.store(
                    ir.IntType(1)(1 if self.value.name == "true" else 0),
                    variable,
                )
            case _:
                raise NotImplementedError(f"Unsupported type: {self.variable.var_type.name}")

    elif isinstance(self.var_type, PointerTypeNode):
        if isinstance(self.value, NamedVarPointerNode):
            assert self.var_type.base.name in ["i32", "f32"], (
                f"Unsupported pointer base type: {self.variable.var_type.base.name}"
            )
            # variable = ir.GlobalVariable(
            #     builder.module, ir.PointerType(), self.name
            # )
            variable = builder.alloca(ir.PointerType(), name=self.name)
            variable.initializer = symbol_table_stack_codegen.get(
                self.value.base_var.name
            ).get_reference()

    if variable is None:
        raise NotImplementedError("Variable was not initialized in declaration.")
    symbol_table_stack_codegen.add(self.variable.name, variable)


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
