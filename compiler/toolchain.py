"""统一 xe 命令，仍使用原版 Python 前端和 C 后端。

本层只管理项目、编译产物与开发工作流，不另造语言规则。旧 compiler/main.py
入口保持兼容。运行程序不用 shell，-- 后参数不会再次拆词或解释。
"""
import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .documentation import collect, markdown
from .driver import ROOT, VERSION, build, cc_identity
from .formatting import format_source, source_files
from .project import find_manifest, project_at, select_source
from .xe_ast.ast import SYNTAX_VERSION
from .xe_ast.build import BuildError, atomic_text, protect_source
from .xe_ast.cli import main as legacy_main
from .xe_ast.lexer import Lexer
from .xe_ast.modules import load_program
from .xe_ast.semantic import Checker
from .xe_ast.source import Diagnostic


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("超时需要有限的正数秒")
    return number


def make_parser():
    parser = argparse.ArgumentParser(prog="xe", description="Xe stage0 工具链（Python + C 后端）")
    parser.add_argument("--version", action="version", version=f"xe {VERSION} ({SYNTAX_VERSION}, stage0)")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(name, help, *, source=True, many=False, binary=True):
        command = commands.add_parser(name, help=help)
        if source:
            command.add_argument("source", type=Path, nargs="*" if many else "?", help=".xe 文件或项目目录")
        command.add_argument("--manifest-path", type=Path, help="xe.toml 或项目目录")
        if binary:
            command.add_argument("--bin", help="选择 src/bin/name.xe 中的 name")
        command.add_argument("--message-format", "--diagnostic-format", dest="message_format",
                             choices=("text", "json"), default="text", help="编译诊断输出格式")
        return command

    def compilation(command):
        command.add_argument("--cc", default=os.environ.get("CC", "cc"), help="C 编译器（单个可执行文件）")
        command.add_argument("--release", action="store_true", help="C 后端使用 -O2，输出到 target/release")
        command.add_argument("--sanitize", action="store_true", help="启用 ASan/UBSan，保留默认泄漏检查")
        command.add_argument("--rebuild", action="store_true", help="跳过可执行文件缓存")
        command.add_argument("--cflag", action="append", default=[], help="附加单个 C 选项，例如 --cflag=-Wall")
        command.add_argument("--link-input", type=Path, action="append", default=[],
                             help="追加外部 C 源码、对象或库文件；可重复，路径相对当前目录")
        command.add_argument("--build-timeout", type=positive_seconds, default=30, help="C 编译超时秒数")

    for name, help in (("ast", "输出单文件 AST JSON"), ("check", "检查可达模块"),
                       ("emit-c", "检查并输出 C"), ("build", "构建可执行程序"),
                       ("run", "构建并运行；-- 后原样传递参数")):
        command = common(name, help)
        if name != "check":
            command.add_argument("-o", "--output", help="输出路径；ast/emit-c 可使用 -")
        if name in {"build", "run"}:
            compilation(command)
        if name == "run":
            command.add_argument("--timeout", type=positive_seconds, help="目标程序运行超时秒数；默认不限制")

    tests = common("test", "编译运行显式 Xe 测试文件，或 --compiler 验收编译器", many=True, binary=False)
    compilation(tests)
    tests.set_defaults(cc=None)
    tests.add_argument("--compiler", action="store_true", help="运行现有 Python 编译器回归")
    tests.add_argument("--pattern", default="test*.py", help="--compiler 使用的 unittest 文件模式")
    tests.add_argument("--timeout", type=positive_seconds, help="Xe 测试默认每个 5 秒；编译器回归默认不限")

    formatter = common("fmt", "保守整理缩进；--check 只核对", many=True, binary=False)
    formatter.add_argument("--check", action="store_true", help="不改文件；需要格式整理时退出 1")
    common("lint", "语义检查及隐藏 var 别名的迁移提示")
    docs = common("doc", "从 ///、//! 和公开声明生成 API 清单")
    docs.add_argument("-o", "--output", help="输出路径；- 输出到标准输出")
    docs.add_argument("--format", choices=("markdown", "json"), default="markdown")
    docs.add_argument("--include-private", action="store_true", help="同时列出私有声明")
    clean = common("clean", "只清理本工具记录且未手动修改的 target 产物", binary=False)
    clean.add_argument("--dry-run", action="store_true", help="展示将删除的文件，不执行删除")
    doctor = common("doctor", "核对解释器、项目与 C 编译器", source=False, binary=False)
    doctor.add_argument("--cc", default=os.environ.get("CC", "cc"))
    return parser


def report_diagnostics(diagnostics, format):
    if not diagnostics:
        return
    if format == "json":
        print(json.dumps({"diagnostics": [diagnostic.to_dict() for diagnostic in diagnostics]},
                         ensure_ascii=False), file=sys.stderr)
    else:
        for diagnostic in diagnostics:
            print(diagnostic.render(), file=sys.stderr)


def progress(data, args):
    if args.message_format == "json":
        print(json.dumps(data, ensure_ascii=False))
    elif data.get("message"):
        print(data["message"])


def selected(args, library=False):
    return select_source(args.source, manifest=args.manifest_path, binary=getattr(args, "bin", None),
                         library=library)


def check_source(source, format):
    source_info, tree = load_program(source)
    checker = Checker(source_info, tree)
    errors = checker.check()
    diagnostics = [*errors, *checker.warnings]
    if format == "json":
        print(json.dumps({"diagnostics": [diagnostic.to_dict() for diagnostic in diagnostics],
                          "inferred_types": list(checker.inferred_types.values())}, ensure_ascii=False),
              file=sys.stderr if diagnostics else sys.stdout)
    else:
        report_diagnostics(diagnostics, format)
        if not errors:
            print(f"语义检查通过：{source}")
    return (1 if errors else 0), tree


def run_compilation(args, source, project, output):
    result = build(project, source, output, cc=args.cc or os.environ.get("CC", "cc"), release=args.release,
                   sanitize=args.sanitize, extra_flags=args.cflag, rebuild=args.rebuild,
                   timeout=args.build_timeout, link_inputs=args.link_input)
    report_diagnostics(result.warnings, args.message_format)
    return result


def protect_inputs(source_paths, outputs):
    """源文件及其本地包清单都属于输入；文档/C 输出不能破坏依赖包。"""
    for filename in source_paths:
        source = Path(filename)
        protect_source(source, outputs)
        manifest = find_manifest(source.parent)
        if manifest:
            protect_source(manifest, outputs)


def publish(project, kind, source_paths, output, text):
    store = ArtifactStore(project.root)
    if output.resolve().is_relative_to(store.directory):
        raise BuildError("输出不能覆盖工具链的内部收据目录")
    protect_inputs(source_paths, [output])
    if project.manifest:
        protect_source(project.manifest, [output])
    atomic_text(output, text)
    store.record(kind, output, [output], expected_hashes={str(output.resolve()):
                 hashlib.sha256(text.encode("utf-8")).hexdigest()})


def compiler_action(args, program_arguments):
    project, source = selected(args, library=args.command == "check")
    if args.command == "check":
        return check_source(source, args.message_format)[0]
    if args.command in {"ast", "emit-c"}:
        store = ArtifactStore(project.root)
        output = args.output or str(project.target / ("ast" if args.command == "ast" else "c") /
                                   (source.name + (".ast.json" if args.command == "ast" else ".c")))
        if output != "-" and Path(output).resolve().is_relative_to(store.directory):
            raise BuildError("输出不能覆盖工具链的内部收据目录")
        if output != "-" and project.manifest:
            protect_source(project.manifest, [Path(output)])
        if output != "-":
            # emit-c 的旧入口保护模块源码，此处另保护可达依赖的包清单。
            # ast 是单文件解析，不为了保护输出额外加载无关模块。
            inputs = [source] if args.command == "ast" else load_program(source)[1]["_sources"]
            protect_inputs(inputs, [Path(output)])
        arguments = [str(source), "-o", output, "--diagnostic-format", args.message_format]
        if args.command == "emit-c":
            arguments.append("--emit-c")
        if args.message_format == "json" and output != "-":
            # 旧入口的人类状态文字不混进统一命令的 JSON 结果。
            with contextlib.redirect_stdout(io.StringIO()):
                status = legacy_main(arguments)
        else:
            status = legacy_main(arguments)
        if status == 0 and output != "-":
            store.record(args.command, Path(output), [Path(output)])
            if args.message_format == "json":
                progress({"schema_version": 1, "action": args.command, "output": str(Path(output).absolute())}, args)
        return status
    if args.output == "-":
        raise BuildError("可执行文件输出不能是 -")
    output = Path(args.output) if args.output else project.target / (
        "release" if args.release else "debug") / project.artifact_name(source)
    result = run_compilation(args, source, project, output)
    if args.command == "build":
        progress({"schema_version": 1, "program": str(result.output), "c": str(result.c_output),
                  "receipt": str(result.receipt), "cached": result.reused,
                  "message": f"{'复用' if result.reused else '构建完成'}：{result.output}"}, args)
        return 0
    # 不向目标程序 stdout 混入构建状态，交互程序保留 stdin/终端/工作目录。
    completed = subprocess.run([str(result.output), *program_arguments], timeout=args.timeout)
    return completed.returncode if completed.returncode >= 0 else 128 - completed.returncode


def format_action(args):
    if args.manifest_path:
        project_at(manifest=args.manifest_path)
    if args.source:
        paths = args.source
    else:
        project = project_at(manifest=args.manifest_path)
        if not project.manifest:
            raise BuildError("fmt 未指定文件时需要 xe.toml；不会默认改写整个仓库")
        paths = [project.source_root]
    files = source_files(paths)
    prepared = []
    # 所有输入先解析、比较 AST；一个文件非法时，不先改其他文件。
    for file in files:
        original = file.read_bytes().decode("utf-8")
        formatted = format_source(original, str(file))
        if original != formatted:
            prepared.append((file, original, formatted))
    for file, original, text in prepared:
        if not args.check:
            if file.read_bytes().decode("utf-8") != original:
                raise BuildError(f"{file}: 整理期间文件已被修改，拒绝覆盖")
            mode = file.stat().st_mode & 0o777
            atomic_text(file, text)
            file.chmod(mode)
    progress({"schema_version": 1, "changed": [str(file) for file, _, _ in prepared],
              "check": args.check, "message": f"{'需要整理' if args.check else '已整理'} {len(prepared)} 个文件"}, args)
    return 1 if args.check and prepared else 0


def lint_action(args):
    project, source = selected(args, library=True)
    status, tree = check_source(source, args.message_format)
    if status:
        return status
    warnings = []
    for filename, info in tree["_sources"].items():
        if not Path(filename).is_relative_to(project.source_root):
            continue
        for token in Lexer(info).scan():
            if token.kind == "var":
                warnings.append(Diagnostic(info, token.start, token.end,
                    "var 是隐藏兼容写法，推荐使用 let[mut]", "XE-LINT-0001",
                    hint="本提示不改变可变性、类型或所有权", severity="warning"))
    report_diagnostics(warnings, args.message_format)
    return 0


def doc_action(args):
    project, source = selected(args, library=True)
    data, warnings, inputs = collect(source, private=args.include_private)
    report_diagnostics(warnings, args.message_format)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n" if args.format == "json" else markdown(data, project.name)
    if args.output == "-":
        sys.stdout.write(text)
    else:
        output = Path(args.output) if args.output else project.target / "doc" / (
            "index.json" if args.format == "json" else "index.md")
        publish(project, "doc", inputs, output, text)
        progress({"schema_version": 1, "output": str(output.absolute()),
                  "message": f"API 文档已输出：{output}"}, args)
    return 0


def test_action(args):
    if args.compiler:
        if args.source or args.manifest_path:
            raise BuildError("--compiler 只验收本仓库编译器，不接受 Xe 源码或项目清单")
        if args.cc or args.release or args.sanitize or args.rebuild or args.cflag or args.link_input or args.build_timeout != 30:
            raise BuildError("--compiler 不接受 Xe 构建选项；现有回归自行设置编译参数")
        command = [sys.executable, "-m", "unittest", "discover", "-s", "compiler/tests", "-p", args.pattern, "-v"]
        if args.message_format == "text" and args.timeout is None:
            return subprocess.call(command, cwd=ROOT)
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            completed = subprocess.run(command, cwd=ROOT, stdout=out, stderr=err, timeout=args.timeout)
            result: dict[str, Any] = {"exit_code": completed.returncode}
            for name, stream in (("stdout", out), ("stderr", err)):
                length = stream.seek(0, 2); stream.seek(max(0, length - 16000))
                result[name] = stream.read().decode("utf-8", errors="replace")
                result[name + "_truncated"] = length > 16000
        if args.message_format == "json":
            progress({"schema_version": 1, "compiler_tests": result}, args)
        else:
            sys.stdout.write(result["stdout"]); sys.stderr.write(result["stderr"])
        return 0 if completed.returncode == 0 else 1
    if not args.source:
        raise BuildError("请指定带 main 的 Xe 测试文件，或用 --compiler 验收编译器；尚未新增单元测试语法")
    if args.pattern != "test*.py":
        raise BuildError("--pattern 只用于 --compiler，不自动发现 Xe 测试文件")
    results = []
    for supplied in args.source:
        try:
            project, source = select_source(supplied, manifest=args.manifest_path)
            identity = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
            output = project.target / "tests" / f"{source.stem}-{identity}"
            built = run_compilation(args, source, project, output)
        except Diagnostic as error:
            results.append({"file": str(supplied), "passed": False, "phase": "compile",
                            "diagnostics": [error.to_dict()], "stdout": "", "stderr": error.render()})
            continue
        except (BuildError, OSError, UnicodeError) as error:
            results.append({"file": str(supplied), "passed": False, "phase": "tool", "tool_error": True,
                            "stdout": "", "stderr": str(error)})
            continue
        # DEVNULL 防止测试等交互；输出落盘后只取尾部，不无限积存在内存。
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                completed = subprocess.run([str(built.output)], stdin=subprocess.DEVNULL,
                                           stdout=out, stderr=err, timeout=args.timeout or 5)
                item = {"file": str(source), "passed": completed.returncode == 0,
                        "exit_code": completed.returncode}
            except subprocess.TimeoutExpired:
                item = {"file": str(source), "passed": False, "timeout": args.timeout or 5}
            for name, stream in (("stdout", out), ("stderr", err)):
                length = stream.seek(0, 2); stream.seek(max(0, length - 16000))
                item[name] = stream.read().decode("utf-8", errors="replace")
                item[name + "_truncated"] = length > 16000
        # sanitizer 的错误必须失败，不因某个工具错误地返回 0 而忽略它。
        if args.sanitize and any(marker in item["stderr"] for marker in
                ("ERROR: AddressSanitizer", "ERROR: LeakSanitizer", "runtime error:")):
            item["passed"] = False
        results.append(item)
    if args.message_format == "text":
        for item in results:
            print(f"{'PASS' if item['passed'] else 'FAIL'} {item['file']}")
            if not item["passed"]:
                print(item["stdout"] + item["stderr"], file=sys.stderr)
    else:
        progress({"schema_version": 1, "tests": results}, args)
    return 2 if any(item.get("tool_error") for item in results) else 0 if all(item["passed"] for item in results) else 1


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    program_arguments = []
    separated = "--" in arguments
    if separated:
        index = arguments.index("--")
        program_arguments, arguments = arguments[index + 1:], arguments[:index]
    parser = make_parser()
    args = parser.parse_args(arguments)
    if separated and args.command != "run":
        parser.error("-- 后的参数只用于 xe run")
    try:
        if args.command in {"ast", "check", "emit-c", "build", "run"}:
            return compiler_action(args, program_arguments)
        if args.command == "fmt":
            return format_action(args)
        if args.command == "lint":
            return lint_action(args)
        if args.command == "doc":
            return doc_action(args)
        if args.command == "test":
            return test_action(args)
        if args.command == "clean":
            if args.source is not None and not args.source.exists():
                raise BuildError("clean 的显式项目路径必须存在；不会猜测其父目录")
            project = project_at(args.source, args.manifest_path)
            if args.source is not None and args.manifest_path is not None and project_at(args.source).root != project.root:
                raise BuildError("clean 的项目路径与 --manifest-path 不属于同一个包")
            result = ArtifactStore(project.root).clean(args.dry_run)
            progress({**result, "message": f"{'计划清理' if args.dry_run else '已清理'} {len(result['removed'])} 个产物；"
                      f"保留 {len(result['preserved'])} 个未知/已修改/外部文件"}, args)
            return 0
        project = project_at(manifest=args.manifest_path)
        identity = cc_identity(args.cc)
        progress({"schema_version": 1, "python": sys.executable, "python_version": sys.version.split()[0],
                  "syntax": SYNTAX_VERSION, "project": str(project.root),
                  "manifest": str(project.manifest) if project.manifest else None,
                  "cc": identity, "uv": shutil.which("uv"),
                  "message": f"工具链可用：Python {sys.version.split()[0]}；{identity['version'].splitlines()[0]}"}, args)
        return 0
    except Diagnostic as error:
        report_diagnostics([error], args.message_format)
        return 1
    except RecursionError:
        message, code, status = "源码结构嵌套过深，请拆分长表达式", "XE-TOOL-DEPTH", 1
    except subprocess.TimeoutExpired:
        message, code, status = "目标程序运行超时", "XE-TOOL-TIMEOUT", 1
    except (BuildError, OSError, UnicodeError, ValueError) as error:
        message, code, status = str(error), "XE-TOOL-0001", 2
    except KeyboardInterrupt:
        return 130
    if args.message_format == "json":
        print(json.dumps({"diagnostics": [{"code": code, "severity": "error", "message": message}]},
                         ensure_ascii=False), file=sys.stderr)
    else:
        print(f"xe: {message}", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
