"""可重复的自举驱动；只组织构建，不解析、改写或翻译 Xe 源码。

seed 由现有 Python stage0 构建。此后每一份 C 都由上一代 Xe 可执行文件
读取同一 compiler.xe 后生成，系统 cc 只处理 C。保留中间产物与哈希报告。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from compiler.xe_ast.build import BuildError, atomic_text, build_executable
from compiler.xe_ast.source import Diagnostic

SOURCE = ROOT / "bootstrap/compiler.xe"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(arguments: list[str], timeout: int = 120) -> None:
    result = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {arguments!r}\n"
                           f"{result.stdout}{result.stderr}")
    # Sanitizer reports must not get hidden merely because a process returned zero.
    if "Sanitizer" in result.stderr or "runtime error:" in result.stderr:
        raise RuntimeError(result.stderr)


def compile_c(source: Path, output: Path, cc: str, sanitize: bool = False) -> None:
    flags = ["-std=c11", "-O0", "-g", "-Werror=return-type",
             "-Werror=implicit-function-declaration", "-Werror=incompatible-pointer-types",
             "-I", str(ROOT / "compiler/runtime")]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"]
    command([cc, *flags, str(source), "-o", str(output)])


def bootstrap(output: Path, cc: str = "cc", sanitize: bool = False) -> dict:
    output = output.resolve()
    # This driver owns generated files only; never let a target overwrite source files.
    if output == ROOT or output in SOURCE.parents or output == SOURCE:
        raise ValueError("自举产物目录不能是仓库根或源码路径")
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    warnings = []
    seed = output / "seed-xec"
    build_executable(SOURCE, seed, cc=cc, warnings=warnings)
    generations, previous = [], seed
    for number in range(1, 4):
        c_source = output / f"stage{number}.c"
        executable = output / f"stage{number}-xec"
        command([str(previous), str(SOURCE), str(c_source)])
        compile_c(c_source, executable, cc)
        generations.append({"stage": number, "c": str(c_source),
                            "sha256": digest(c_source), "program": str(executable)})
        previous = executable
    if len({stage["sha256"] for stage in generations}) != 1:
        raise RuntimeError("自举固定点失败：三代生成的 C 不相同")
    if sanitize:
        checked = output / "checked-xec"
        compile_c(output / "stage3.c", checked, cc, sanitize=True)
        command([str(checked), str(SOURCE), str(output / "checked.c")])
        if digest(output / "checked.c") != generations[0]["sha256"]:
            raise RuntimeError("sanitizer 构建改变了生成结果")
    version_lines = subprocess.run([cc, "--version"], capture_output=True, text=True,
                                   check=True, timeout=10).stdout.splitlines()
    report = {
        "schema_version": 1, "subset": "xe-selfhost-0.1", "source": str(SOURCE),
        "source_sha256": digest(SOURCE), "cc": cc, "stages": generations,
        "cc_version": version_lines[0] if version_lines else "unknown",
        "runtime_sha256": {str(path.relative_to(ROOT)): digest(path) for path in
                           (ROOT / "compiler/runtime/xe_runtime.h", ROOT / "stdlib/io/xe_io.h",
                            ROOT / "stdlib/env/xe_env.h")},
        "fixed_point": True, "comparison": "byte-identical generated C",
        "sanitizers": "address,undefined; default leak detection" if sanitize else None,
        "seed_warnings": [{"code": warning.code, "message": warning.message}
                          for warning in warnings],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    atomic_text(output / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "target/bootstrap")
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"))
    parser.add_argument("--sanitize", action="store_true")
    args = parser.parse_args()
    try:
        report = bootstrap(args.output, args.cc, args.sanitize)
    except (BuildError, Diagnostic, RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        detail = error.render() if isinstance(error, Diagnostic) else str(error)
        print(f"自举失败：{detail}", file=sys.stderr)
        return 1
    print(f"Xe 子集自举固定点通过：3 代 C 完全相同，SHA-256 {report['stages'][0]['sha256']}")
    print(f"报告：{args.output / 'report.json'}")
    if report["seed_warnings"]:
        print(f"stage0 保留 {len(report['seed_warnings'])} 条指针风险 warning；详情见报告。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
