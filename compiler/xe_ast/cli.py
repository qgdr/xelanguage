"""单文件命令行入口；AST 输出只在解析成功后写入，--check 不写 AST。

输出使用同目录临时文件 + 原子替换，避免中断留下半份 JSON。
失败时不创建 AST，也不覆盖上一次成功产物。源码错误退出码 1，I/O 错误退出码 2。
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from .ast import document
from .parser import parse_source
from .source import Diagnostic, Source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Xe 单文件前端：输出 AST，或检查类型与所有权")
    parser.add_argument("source", type=Path, help="UTF-8 .xe 源文件")
    parser.add_argument("-o", "--output", help="JSON 文件；- 表示标准输出")
    parser.add_argument("--diagnostic-format", choices=("text", "json"), default="text")
    parser.add_argument("--check", action="store_true", help="语义检查，不写 AST 文件")
    parser.add_argument("--check-borrows", action="store_true", help="语义检查时启用基础借用/生命周期检查")
    args = parser.parse_args(argv)
    if args.check_borrows and not args.check:
        parser.error("--check-borrows 必须与 --check 一起使用；AST 阶段不检查借用")
    if args.check and args.output is not None:
        parser.error("--check 不写 AST，请移除 -o；诊断 JSON 使用 --diagnostic-format json")
    output = Path(args.output) if args.output and args.output != "-" else None
    if args.output is None:
        output = Path("target/ast") / (args.source.name + ".ast.json")
    try:
        if output is not None and output.resolve() == args.source.resolve():
            print("输出路径不能覆盖输入源码。", file=sys.stderr)
            return 2
        # bytes.decode 不把 CRLF 改为 LF，保证 AST offset 与文件原文一致。
        source_text = args.source.read_bytes().decode("utf-8")
        tree = parse_source(source_text, str(args.source))
        if args.check:
            from .semantic import Checker
            diagnostics = Checker(Source(source_text, str(args.source)), tree, args.check_borrows).check()
            if args.diagnostic_format == "json":
                print(json.dumps({"diagnostics": [d.to_dict() for d in diagnostics]}, ensure_ascii=False),
                      file=sys.stderr if diagnostics else sys.stdout)
            elif diagnostics:
                for diagnostic in diagnostics:
                    print(diagnostic.render(), file=sys.stderr)
            else:
                print(f"语义检查通过：{args.source}")
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
        print(f"无法读取源码或写入 AST：{error}", file=sys.stderr)
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
