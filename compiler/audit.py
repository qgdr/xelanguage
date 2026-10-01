"""逐例核实 AST、语义、C、系统编译和执行，不把一个示例当成完整功能证明。

运行：uv run --project . --frozen --offline python -m compiler.audit
也可使用 python compiler/audit.py DIRECTORY -o target/audit/report.json。

只有 JSON 报告保留；每个程序的 C、可执行文件和运行输出都放在临时目录。
这不是安全沙箱：没有发现指针风险不代表程序安全，只应选择可信任的源码。
有指针风险警告的程序默认只编译、不执行；--run-warnings 才显式允许执行。
未实现由编译器能力诊断码识别；普通类型/所有权错误不会冒充未实现。
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# 兼容直接运行脚本和 python -m；不依赖当前工作目录恰好是仓库根目录。
ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from compiler.xe_ast.ast import SYNTAX_VERSION
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import atomic_text, protect_source
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.modules import load_program
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Diagnostic, Source

PHASES = ("ast", "semantic", "c", "compile", "run")
CAPABILITY_CODES = {"XE-SEM-0001", "XE-BACKEND-0001"}
OUTPUT_LIMIT = 16000


def _error(code, message):
    return {"code": code, "message": message}


def _diagnostics(errors):
    """保留原始诊断与位置；分类依据稳定代码，不搜索自然语言中的‘未实现’。"""
    unsupported = all(error.code in CAPABILITY_CODES for error in errors)
    return {"status": "unsupported" if unsupported else "rejected",
            "diagnostics": [error.to_dict() for error in errors]}


def _tail(stream):
    """输出落盘而非无限积存在内存中；报告保留最后 16 KB 及截断标记。"""
    length = stream.seek(0, 2)
    stream.seek(max(0, length - OUTPUT_LIMIT))
    return stream.read().decode("utf-8", errors="replace"), length > OUTPUT_LIMIT


def _process(command, directory, timeout, phase):
    # DEVNULL 防止程序等待交互输入。cwd 隔离相对路径产物，不声称隔离绝对路径访问。
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            completed = subprocess.run(command, cwd=directory, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, timeout=timeout)
            status = "passed" if completed.returncode == 0 else (
                "tool_error" if phase == "compile" else "nonzero")
            result = {"status": status, "exit_code": completed.returncode}
            if completed.returncode:
                result["diagnostics"] = [_error(
                    "XE-AUDIT-COMPILE" if phase == "compile" else "XE-AUDIT-EXIT",
                    f"{'C 编译器' if phase == 'compile' else '程序'}退出码为 {completed.returncode}")]
        except subprocess.TimeoutExpired:
            result = {"status": "timeout", "diagnostics": [_error(
                "XE-AUDIT-TIMEOUT", f"{phase} 超过 {timeout:g} 秒") ]}
        except OSError as error:
            result = {"status": "tool_error", "diagnostics": [_error("XE-AUDIT-TOOL", str(error))]}
        stdout, stdout_truncated = _tail(out)
        stderr, stderr_truncated = _tail(err)
        # 临时目录随机名称不是被审核语言行为，移除它使报告更便于重复对比。
        result.update(stdout=stdout.replace(str(directory), "<temporary>"),
                      stderr=stderr.replace(str(directory), "<temporary>"),
                      stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated)
        return result


def audit_file(path, *, cc="cc", compile_timeout=10.0, run_timeout=2.0,
               run_warnings=False):
    """错误停止后续阶段；指针警告不拒绝编译，但默认停止在执行之前。"""
    path = Path(path).resolve()
    result = {"file": str(path), "outcome": "passed",
              "stages": {phase: {"status": "skipped"} for phase in PHASES}}
    stages = result["stages"]

    def failed(phase, stage):
        stages[phase] = stage
        result["outcome"] = {"rejected": "invalid", "nonzero": "execution_nonzero"}.get(
            stage["status"], stage["status"])
        return result

    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
        result["source_sha256"] = hashlib.sha256(data).hexdigest()
    except (OSError, UnicodeError) as error:
        return failed("ast", {"status": "io_error",
                              "diagnostics": [_error("XE-AUDIT-SOURCE", str(error))]})

    current_phase = "ast"
    try:
        tree = parse_source(text, str(path))
        stages["ast"] = {"status": "passed"}
        current_phase = "semantic"
        source_info, tree = load_program(path, text)
        checker = Checker(source_info, tree)
        errors = checker.check()
        warnings = [warning.to_dict() for warning in checker.warnings]
        if errors:
            stage = _diagnostics(errors)
            stage["warnings"] = warnings
            return failed("semantic", stage)
        stages["semantic"] = {"status": "passed", "warnings": warnings}
        current_phase = "c"
        generated = lower_to_c(text, str(path))
        stages["c"] = {"status": "passed"}
        # 工具链与运行由本脚本拆成两层；不要把编译器失败归类成 Xe 源码非法。
        # 与 build.py 使用相同基础旗标，独立调用是为了分别记录阶段和短 timeout。
        with tempfile.TemporaryDirectory(prefix="xe-audit-") as name:
            directory = Path(name)
            c_path, executable = directory / "program.c", directory / "program"
            atomic_text(c_path, generated)
            current_phase = "compile"
            resolved_cc = shutil.which(str(cc))
            compiler = str(Path(resolved_cc).resolve()) if resolved_cc else str(cc)
            command = [
                compiler, "-std=c11", "-O0", "-g",
                "-Werror=implicit-function-declaration", "-Werror=incompatible-pointer-types",
                "-Werror=return-type", str(c_path), "-o", str(executable),
            ]
            if "#include <pthread.h>" in generated:
                command.insert(1, "-pthread")
            stages["compile"] = _process(command, directory, compile_timeout, "compile")
            if stages["compile"]["status"] != "passed":
                return failed("compile", stages["compile"])
            if not run_warnings and any(warning["code"].startswith("XE-PTR-")
                                        for warning in warnings):
                # 警告允许产出二进制，不等于授权审核工具实际触发潜在 UB。
                # outcome 与 invalid 区分，保留语义/C/系统编译阶段的成功。
                stages["run"] = {"status": "skipped", "reason": "pointer_risk_warning"}
                result["outcome"] = "warning_not_run"
                return result
            current_phase = "run"
            stages["run"] = _process([str(executable)], directory, run_timeout, "run")
            if stages["run"]["status"] != "passed":
                return failed("run", stages["run"])
    except Diagnostic as error:
        return failed(current_phase, _diagnostics([error]))
    except Exception as error:
        # 编译器崩溃单独报告，不能被统计为“正确拒绝了非法代码”。
        return failed(current_phase, {"status": "internal_error", "diagnostics": [_error(
            "XE-AUDIT-INTERNAL", f"{type(error).__name__}: {error}")]})
    return result


def audit_directory(directory, *, cc="cc", compile_timeout=10.0, run_timeout=2.0,
                    run_warnings=False):
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise ValueError(f"源码目录不存在：{directory}")
    if any(not math.isfinite(value) or value <= 0 for value in (compile_timeout, run_timeout)):
        raise ValueError("timeout 必须是有限的正数")
    files = sorted(directory.rglob("*.xe"))
    results = [audit_file(path, cc=cc, compile_timeout=compile_timeout,
                          run_timeout=run_timeout, run_warnings=run_warnings) for path in files]
    return {
        "schema_version": 1,
        "syntax_version": SYNTAX_VERSION,
        "source_directory": str(directory),
        "configuration": {"cc": str(cc), "compile_timeout": compile_timeout,
                          "run_timeout": run_timeout, "pointer_risk_warnings": True,
                          "execute_warning_programs": bool(run_warnings)},
        "scope": "逐例阶段审核，不证明整门语言功能完整、输出正确或自举固定点成立；不是安全沙箱。",
        "summary": {"files": len(results),
                    "outcomes": dict(sorted(Counter(row["outcome"] for row in results).items())),
                    "stages": {phase: dict(sorted(Counter(row["stages"][phase]["status"]
                                                           for row in results).items()))
                               for phase in PHASES}},
        "results": results,
    }


def write_report(report, output):
    """只发布报告；拒绝任何 .xe 目标及能解析到输入源码的路径。"""
    output = Path(output)
    if output.suffix.lower() == ".xe" or output.resolve().suffix.lower() == ".xe":
        raise ValueError("审核报告不能使用 .xe 输出路径，以免覆盖源码")
    for row in report["results"]:
        protect_source(Path(row["file"]), [output])
    atomic_text(output, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True,
                                   allow_nan=False) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="逐例审核 Xe 的解析、语义、C 生成、编译与运行")
    parser.add_argument("directory", nargs="?", type=Path, default=ROOT / "tests/stage999")
    parser.add_argument("-o", "--output", type=Path, default=ROOT / "target/audit/stage999.json")
    parser.add_argument("--cc", default="cc")
    parser.add_argument("--compile-timeout", type=float, default=10.0)
    parser.add_argument("--run-timeout", type=float, default=2.0)
    parser.add_argument("--run-warnings", action="store_true",
                        help="允许实际运行带指针风险警告的程序（可能触发未定义行为）")
    parser.add_argument("--strict", action="store_true", help="任何示例未通过全部阶段时退出 1")
    args = parser.parse_args(argv)
    try:
        report = audit_directory(args.directory, cc=args.cc,
                                 compile_timeout=args.compile_timeout, run_timeout=args.run_timeout,
                                 run_warnings=args.run_warnings)
        write_report(report, args.output)
    except (ValueError, OSError, UnicodeError) as error:
        print(f"无法完成审核：{error}", file=sys.stderr)
        return 2
    summary = report["summary"]
    print(f"审核 {summary['files']} 个示例：" + json.dumps(summary["outcomes"], ensure_ascii=False))
    for phase, counts in summary["stages"].items():
        print(f"  {phase}: " + json.dumps(counts, ensure_ascii=False))
    for row in report["results"]:
        for phase, stage in row["stages"].items():
            for diagnostic in stage.get("diagnostics", []) + stage.get("warnings", []):
                severity = diagnostic.get("severity", "error")
                print(f"{row['file']} [{phase}] {severity} {diagnostic['code']}: {diagnostic['message']}")
    print(f"JSON 报告：{args.output}")
    # 默认退出 0 表示审核完成，不表示全部功能已实现；CI 可使用 --strict。
    return int(args.strict and any(row["outcome"] != "passed" for row in report["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
