"""命令行入口；AST 描述一个文件，检查/构建加载可达模块。

输出使用同目录临时文件 + 原子替换，避免中断留下半份 JSON。
失败时不创建 AST，也不覆盖上一次成功产物。源码错误退出码 1，I/O 错误退出码 2。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .ast import document
from .build import BuildError
from .parser import parse_source
from .source import Diagnostic, Source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Xe 编译器：AST、模块检查、C 输出、构建与运行",
        epilog="--run 时，-- 后的参数原样传给 Xe 程序，例如：main.xe --run -- check")
    parser.add_argument("source", type=Path, help="UTF-8 .xe 入口文件；检查/构建递归加载模块")
    parser.add_argument("-o", "--output", help="AST/C/程序输出路径；- 仅用于文本标准输出")
    parser.add_argument("--diagnostic-format", choices=("text", "json"), default="text")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="语义检查，不写 AST 文件")
    actions.add_argument("--emit-c", action="store_true", help="语义检查并输出可读 C")
    actions.add_argument("--build", action="store_true", help="生成 C 并编译可执行程序")
    actions.add_argument("--run", action="store_true", help="构建并运行程序")
    parser.add_argument("--cc", default="cc", help="系统 C 编译器路径（默认 cc）")
    parser.add_argument("--link-input", type=Path, action="append", default=[],
                        help="外部 C 源码、对象或库文件；构建/运行时可重复")
    parser.add_argument("--check-safety", "--check-borrows", dest="check_borrows", action="store_true",
                        help="兼容选项；始终检查类型、写权限和所有权，指针风险只警告")
    # 分界符之前只解析编译器选项，之后只交给目标程序。不能用 shell 字符串
    # 拼接：空参数、参数内的空格、--help 和第二个 -- 都必须原样保留。
    arguments = list(sys.argv[1:] if argv is None else argv)
    program_arguments = []
    has_separator = "--" in arguments
    if has_separator:
        separator = arguments.index("--")
        program_arguments = arguments[separator + 1:]
        arguments = arguments[:separator]
    args = parser.parse_args(arguments)
    if has_separator and not args.run:
        parser.error("-- 后的程序参数只能与 --run 一起使用")
    if args.link_input and not (args.build or args.run):
        parser.error("--link-input 只能用于 --build 或 --run")
    if args.check_borrows and not (args.check or args.emit_c or args.build or args.run):
        parser.error("--check-safety 必须用于检查或后端动作；AST 阶段不检查可变性或借用")
    # 暂停开放不检查模式；旧旗标保留以免已有构建命令失效。
    args.check_borrows = True
    if args.check and args.output is not None:
        parser.error("--check 不写 AST，请移除 -o；诊断 JSON 使用 --diagnostic-format json")
    backend = args.emit_c or args.build or args.run
    if (args.build or args.run) and args.output == "-":
        parser.error("可执行文件输出不能是 -")
    output = Path(args.output) if args.output and args.output != "-" else None
    if args.output is None:
        output = (Path("target/c") / (args.source.name + ".c") if args.emit_c else
                  Path("target/debug") / args.source.stem if args.build or args.run else
                  Path("target/ast") / (args.source.name + ".ast.json"))
    source_text = ""
    warnings = []
    def report_warnings():
        if not warnings:
            return
        if args.diagnostic_format == "json":
            print(json.dumps({"diagnostics": [w.to_dict() for w in warnings]}, ensure_ascii=False),
                  file=sys.stderr)
        else:
            for warning in warnings:
                print(warning.render(), file=sys.stderr)
    try:
        if output is not None and output.resolve() == args.source.resolve():
            print("输出路径不能覆盖输入源码。", file=sys.stderr)
            return 2
        if backend:
            from .backend_c import lower_to_c
            from .build import build_executable, emit_c
            if args.emit_c:
                if args.output == "-":
                    source_text = args.source.read_bytes().decode("utf-8")
                    sys.stdout.write(lower_to_c(source_text, str(args.source), args.check_borrows, warnings=warnings))
                else:
                    assert output is not None  # -o - 已由上一分支处理。
                    emit_c(args.source, output, args.check_borrows, warnings=warnings)
                    print(f"C 已输出：{output}")
            else:
                assert output is not None  # 构建/运行总有默认或显式程序路径。
                build_executable(args.source, output, args.check_borrows, args.cc, warnings=warnings,
                                 link_inputs=args.link_input)
                if args.run:
                    report_warnings()
                    return subprocess.call([str(output.resolve()), *program_arguments])
                print(f"可执行程序已输出：{output}")
            report_warnings()
            return 0
        # bytes.decode 不把 CRLF 改为 LF，保证 AST offset 与文件原文一致。
        source_text = args.source.read_bytes().decode("utf-8")
        tree = parse_source(source_text, str(args.source))
        if args.check:
            from .modules import load_program
            from .semantic import Checker
            source_info, tree = load_program(args.source, source_text)
            checker = Checker(source_info, tree, args.check_borrows)
            diagnostics = checker.check()
            warnings = checker.warnings
            if args.diagnostic_format == "json":
                print(json.dumps({"diagnostics": [d.to_dict() for d in [*diagnostics, *warnings]],
                                  "inferred_types": list(checker.inferred_types.values())}, ensure_ascii=False),
                      file=sys.stderr if diagnostics or warnings else sys.stdout)
            elif diagnostics:
                for diagnostic in diagnostics:
                    print(diagnostic.render(), file=sys.stderr)
            else:
                print(f"语义检查通过：{args.source}")
            if args.diagnostic_format != "json":
                report_warnings()
            return 1 if diagnostics else 0
        payload = document(Source(source_text, str(args.source)), tree)
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output == "-":
            sys.stdout.write(rendered)
        else:
            assert output is not None
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8",
                        newline="\n", dir=output.parent, prefix=".xe-ast-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(rendered)
                os.replace(temporary, output)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            print(f"AST 已输出：{output}")
        return 0
    except Diagnostic as diagnostic:
        if args.diagnostic_format == "json":
            print(json.dumps({"diagnostics": [diagnostic.to_dict()]}, ensure_ascii=False),
                  file=sys.stderr)
        else:
            print(diagnostic.render(), file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as error:
        print(f"无法读取源码或写入编译产物：{error}", file=sys.stderr)
        return 2
    except BuildError as error:
        if args.diagnostic_format == "json":
            print(json.dumps({"diagnostics": [{"code": "XE-BUILD-0001", "message": str(error)}]},
                             ensure_ascii=False), file=sys.stderr)
        else:
            print(f"构建失败：{error}", file=sys.stderr)
        return 2
    except RecursionError:
        # 长二元链可能解析成功，但 JSON 嵌套超出宿主限制，也应是友好的诊断。
        diagnostic = Diagnostic(Source(source_text, str(args.source)), 0, 0,
                                "AST 嵌套过深，请拆分长表达式", "XE-AST-0001")
        if args.diagnostic_format == "json":
            print(json.dumps({"diagnostics": [diagnostic.to_dict()]}, ensure_ascii=False),
                  file=sys.stderr)
        else:
            print(diagnostic.render(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
