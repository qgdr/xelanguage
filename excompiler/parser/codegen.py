import os
import subprocess
from llvmlite import ir, binding as llvm
from llvmlite.binding import Target
from parser.node import (
    # stage01
    # ModuleNode,
    FunctionNode,
    IntegerNode,
    FloatNode,
    BooleanNode,
    VariableNode,
    TypeNode,
    UnaryExpressionNode,
    BinaryExpressionNode,
    ReturnStatementNode,
    VariableDeclarationNode,
    VarEqualNode,
    CallExpressionNode,
    # stage02
    PointerTypeNode,
    NamedVarPointerNode,
    PtrDerefEqualNode,
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
        for function in prog.body:
            if not isinstance(function, FunctionNode):
                raise TypeError(f"Expected FunctionNode, got {type(function)}")
            function.codegen(self.module)

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
    builder.store(self.value.codegen(), variable)
    setattr(variable, "alloca", True)  # 标记为分配的变量
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


setattr(PointerTypeNode, "codegen", PointerType_codegen)


def NamedVarPointer_codegen(self: NamedVarPointerNode):
    global builder, symbol_table_stack_codegen
    # 处理指向变量的指针
    var_ptr = symbol_table_stack_codegen.get(self.name)
    if not var_ptr.alloca:
        raise ValueError(f"Variable '{self.name}' is not allocated.")
    return var_ptr


setattr(NamedVarPointerNode, "codegen", NamedVarPointer_codegen)


def PtrDerefEqual_codegen(self: PtrDerefEqualNode):
    global builder, symbol_table_stack_codegen
    # 处理指针解引用赋值
    var_ptr = self.variable.codegen()
    value = self.value.codegen()
    builder.store(value, var_ptr)


setattr(PtrDerefEqualNode, "codegen", PtrDerefEqual_codegen)


# def PtrDerefMove_codegen(self):
#     if isinstance(self.value, IntegerNode):
#         variable = builder.module.get_global(self.name.name)
#         variable.initializer = ir.Constant(ir.IntType(32), int(self.value.value, 0))
#     else:
#         raise NotImplementedError(f"Unsupported assignment type: {self.value.type}")


# setattr(PtrDerefMoveNode, "codegen", PtrDerefMove_codegen)
