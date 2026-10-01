"""stage0 构建管理：始终重新检查 Xe，只缓存系统 C 编译结果。

缓存不是 HIR/MIR 或按模块增量编译。生成 C、输入、工具身份与选项相同，且
产物内容没有被改写时才能复用。诊断/warning 每次仍由当前前端产生。
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ArtifactStore, digest
from .project import Project
from .xe_ast.ast import SYNTAX_VERSION
from .xe_ast.backend_c import lower_program_to_c
from .xe_ast.build import BuildError, atomic_text, compile_generated, protect_source
from .xe_ast.modules import load_program

VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parents[1]


def cc_identity(cc: str) -> dict:
    path = shutil.which(cc)
    if path is None:
        raise BuildError(f"找不到 C 编译器 {cc}，请安装 GCC/Clang 或使用 --cc 指定")
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError(f"无法查询 C 编译器 {cc}：{error}") from error
    if result.returncode:
        raise BuildError(f"C 编译器 --version 失败：{result.stderr or result.stdout}")
    return {"invocation": str(Path(path).absolute()), "path": str(Path(path).resolve()), "sha256": digest(Path(path)),
            "version": (result.stdout or result.stderr).strip()}


def profile_flags(release=False, sanitize=False, extra=()) -> tuple[str, ...]:
    flags = ["-O2"] if release else []
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        if platform.system() == "Linux":
            flags += ["-no-pie"]
    return (*flags, *extra)


@dataclass
class BuildResult:
    output: Path
    c_output: Path
    receipt: Path
    reused: bool
    warnings: list


def build(project: Project, source: Path, output: Path, *, cc="cc", release=False,
          sanitize=False, extra_flags=(), rebuild=False, timeout=30) -> BuildResult:
    output = output.absolute()
    c_path = output.with_name(output.name + ".c")
    store = ArtifactStore(project.root)
    if output.resolve().is_relative_to(store.directory) or c_path.resolve().is_relative_to(store.directory):
        raise BuildError("输出不能覆盖工具链的内部收据目录")
    receipt = store.receipt_path("build", output)
    source_info, tree = load_program(source)
    inputs = {filename: hashlib.sha256(info.text.encode("utf-8")).hexdigest()
              for filename, info in tree["_sources"].items()}
    from .project import find_manifest
    for filename in tuple(inputs):
        manifest = find_manifest(Path(filename).parent)
        if manifest:
            inputs[str(manifest)] = digest(manifest)
    for filename in inputs:
        protect_source(Path(filename), [output, c_path, receipt])
    warnings = []
    generated = lower_program_to_c(source_info, tree, warnings=warnings)
    identity = cc_identity(cc)
    flags = profile_flags(release, sanitize, extra_flags)
    compiler_hashes = {str(path.relative_to(ROOT)): digest(path) for path in
                       sorted((ROOT / "compiler").glob("*.py")) +
                       sorted((ROOT / "compiler/xe_ast").glob("*.py"))}
    key_data = {"syntax": SYNTAX_VERSION, "tool_version": VERSION, "source": str(source),
                "inputs": inputs, "generated_c": hashlib.sha256(generated.encode("utf-8")).hexdigest(),
                "cc": identity, "flags": flags, "platform": platform.platform(),
                "compiler": compiler_hashes,
                "environment": {name: os.environ.get(name) for name in
                    ("PATH", "CPATH", "C_INCLUDE_PATH", "LIBRARY_PATH", "COMPILER_PATH",
                     "GCC_EXEC_PREFIX", "SDKROOT", "MACOSX_DEPLOYMENT_TARGET", "SOURCE_DATE_EPOCH")}}
    key = hashlib.sha256(json.dumps(key_data, sort_keys=True).encode("utf-8")).hexdigest()
    previous = store.read("build", output)
    # 自定义 C 选项可读取未登记的 -include/响应文件；保守禁用缓存避免遗漏输入。
    reused = bool(not rebuild and not extra_flags and previous and previous.get("cache_key") == key and
                  output.is_file() and not output.is_symlink() and os.access(output, os.X_OK) and
                  previous.get("files", {}).get(str(output.resolve())) == digest(output))
    # C 可读产物也恢复为当前生成结果，手动修改 C 不进入此次编译输入。
    if not c_path.is_file() or c_path.read_bytes() != generated.encode("utf-8"):
        atomic_text(c_path, generated)
    expected_hashes = {str(c_path.resolve()): key_data["generated_c"]}
    if reused:
        assert previous is not None  # 缓存命中必然来自有效收据。
        expected_hashes[str(output.resolve())] = previous["files"][str(output.resolve())]
    if not reused:
        try:
            compile_generated(generated, c_path, output, cc=identity["invocation"], extra_flags=flags,
                              timeout=timeout, published_hashes=expected_hashes)
        except (BuildError, OSError):
            # 失败也保留并登记可检查的 C；不把旧可执行文件标记为新构建成功。
            try:
                store.record("failed-c", c_path, [c_path], expected_hashes=expected_hashes, source=str(source))
            except OSError:
                pass  # 保留原始构建错误，避免收据写入错误掩盖原因。
            raise
    receipt = store.record("build", output, [output, c_path], expected_hashes=expected_hashes, cache_key=key,
                           build=key_data, profile="release" if release else "debug")
    return BuildResult(output, c_path, receipt, reused, warnings)
