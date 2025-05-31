import argparse
import json
import sys
from pathlib import Path
from parser.lexer import Lexer


__version__ = "0.1.0"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compiler for XeLanguage")
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("input_file", type=Path, help="Source code file")
    # 词法分析器
    parser.add_argument("--lex", action="store_true", help="tokenize")
    # 语法分析器
    parser.add_argument("--parser", action="store_true", help="parser")
    # 中间代码生成
    parser.add_argument("--ir", action="store_true", help="generate IR")
    # 可执行文件生成
    parser.add_argument("-o", "--output", type=Path, help="Output executable path")

    args = parser.parse_args()

    try:
        file_path = args.input_file
        src = file_path.read_text()
        # print(src)
    except FileNotFoundError:
        sys.exit(f"Error: Input file {args.input_file} not found")
    except Exception as e:
        sys.exit(f"Compilation error: {str(e)}")

    if args.lex:
        tokens = Lexer(src).tokenize()
        for token in tokens:
            # print(token, "(", token.lineno, " ,", token.column, ")")
            print(token.value, end=" ")
        print("\n")
        exit(0)

    from parser.parser import Parser

    program = Parser(src).parse()
    if program is None:
        print("Parse error!")
        exit(1)
    if args.parser:
        # AST转储
        try:
            ast_file_path = file_path.with_suffix(".ast.json")
            with open(ast_file_path, "w") as f:
                json.dump(program.to_dict(), f, indent=4)
            print(json.dumps(program.to_dict(), indent=4))
        except Exception as e:
            print(f"Error writing AST to file: {e}")
            print(program.to_dict())
        exit(0)

    # 生成可执行文件
    from codegen.llvm_codegen import LLVMCodeGen

    codegen = LLVMCodeGen()
    # 中间指令 IR
    ir_code = codegen.generate(program)
    ir_file_path = file_path.with_suffix(".ll")
    ir_file_path.write_text(ir_code)
    if args.ir:
        print(f"IR generated at: {ir_file_path}")
        exit(0)

    

    exe_path = file_path.with_suffix(".out")
    if args.output:
        exe_path = args.output
    print(f"Compiling {file_path} to {exe_path}")

    codegen.compile_to_executable(ir_file_path, exe_path)
    print(f"\nExecutable generated at: {exe_path}")

    # print("Failed!")