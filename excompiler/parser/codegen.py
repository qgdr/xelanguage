import os
import subprocess
from llvmlite import ir, binding as llvm
from llvmlite.binding import Target
from parser.node import (
    BooleanNode,
    ModuleNode,
    FunctionNode,
    PtrDerefMoveNode,
    VariableNode,
    BinaryExpressionNode,
    IntegerNode,
    FloatNode,
    ReturnStatementNode,
    VariableDeclarationNode,
    VarEqualNode,
)


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
        # 现阶段只有一个函数 main
        self.module.name = module_name
        mainAST = prog.body[0]

        symbol_table_stack_codegen.push()  # Push a new symbol table for the main function

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

    def pop(self):
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack.pop()

    def current(self):
        if not self.stack:
            raise IndexError("Symbol table stack is empty.")
        return self.stack[-1]

    def add(self, name, ir_local_var):
        if name in self.current():
            raise ValueError(f"Symbol '{name}' already exists in the current symbol table.")
        # 在当前符号表中添加变量
        self.current().add(name, ir_local_var)

    def get(self, name):
        for table in reversed(self.stack):
            if name in table:
                return table.get(name)
        raise KeyError(f"Symbol '{name}' not found in any symbol table.")


symbol_table_stack_codegen = SymbolTableStack_codegen()


IRType = {
    "i32": ir.IntType(32),
    "f32": ir.FloatType(),
    "bool": ir.IntType(1),
    "ptr": ir.PointerType(),  # 假设指针类型为指向i32的指针
}


## codegen for AST nodes
# def Function_codegen(self, builder: ir.IRBuilder):
def Variable_codegen(self, builder: ir.IRBuilder):
    # 处理变量节点
    var_ptr = symbol_table_stack_codegen.get(self.name)
    return builder.load(var_ptr, typ=var_ptr.type.pointee)


setattr(VariableNode, "codegen", Variable_codegen)


# 暂时只支持整数的二元表达式
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


def Integer_codegen(self, builder: ir.IRBuilder):
    # 处理整数节点
    return ir.Constant(ir.IntType(32), int(self.value, 0))


setattr(IntegerNode, "codegen", Integer_codegen)


def Float_codegen(self, builder: ir.IRBuilder):
    # 处理浮点数节点
    return ir.Constant(ir.FloatType(), float(self.value))


setattr(FloatNode, "codegen", Float_codegen)


def Boolean_codegen(self, builder: ir.IRBuilder):
    # 处理布尔值节点
    return ir.Constant(ir.IntType(1), 1 if self.value == "true" else 0)


setattr(BooleanNode, "codegen", Boolean_codegen)


def ReturnStatement_codegen(self, builder: ir.IRBuilder):
    builder.ret(self.value.codegen(builder))


setattr(ReturnStatementNode, "codegen", ReturnStatement_codegen)


def VariableDeclaration_codegen(self: VariableDeclarationNode, builder: ir.IRBuilder):
    if self.value is None:
        raise NotImplementedError("Variable declaration without initial value")
    match self.variable.var_type:
        case "i32":
            var_type = ir.IntType(32)
        case "f32":
            var_type = ir.FloatType()
        case "bool":
            var_type = ir.IntType(1)
        case _:
            raise NotImplementedError(
                f"Unsupported variable type: {self.variable.var_type}"
            )
    variable = builder.alloca(var_type, name=self.variable.name)
    builder.store(self.value.codegen(builder), variable)
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
