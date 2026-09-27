"""构建驱动：生成 C、调用系统工具，再原子发布可执行文件。

不用 shell 拼接命令。失败保留旧可执行文件；生成 C 留在旁边方便排查。
语言诊断与外部工具失败分开，后者使用退出码 2。
"""
import os
from pathlib import Path
import subprocess
import tempfile
from .backend_c import lower_to_c


class BuildError(Exception):
    pass


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                dir=path.parent, prefix=".xe-output-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def protect_source(source: Path, targets):
    if any(target.resolve() == source.resolve() for target in targets):
        raise BuildError("输出路径不能覆盖输入源码")


def emit_c(source: Path, output: Path, check_borrows=True, warnings=None):
    protect_source(source, [output])
    text = source.read_bytes().decode("utf-8")
    generated = lower_to_c(text, str(source), check_borrows, warnings=warnings)
    atomic_text(output, generated)
    return output


def build_executable(source: Path, output: Path, check_borrows=True,
                     cc="cc", extra_flags=(), warnings=None):
    c_path = output.with_name(output.name + ".c")
    protect_source(source, [output, c_path])
    emit_c(source, c_path, check_borrows, warnings=warnings)
    with tempfile.TemporaryDirectory(prefix=".xe-build-", dir=output.parent) as directory:
        executable = Path(directory) / "program"
        command = [cc, "-std=c11", "-O0", "-g",
                   "-Werror=implicit-function-declaration", "-Werror=incompatible-pointer-types",
                   "-Werror=return-type", *extra_flags, str(c_path.resolve()), "-o", str(executable)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        except FileNotFoundError as error:
            raise BuildError(f"找不到 C 编译器 {cc}，请安装 GCC/Clang 或使用 --cc 指定") from error
        except subprocess.TimeoutExpired as error:
            raise BuildError(f"C 编译器运行超过 30 秒；生成代码保留于 {c_path}") from error
        if result.returncode:
            raise BuildError(f"C 编译器未能生成程序；生成代码：{c_path}\n"
                             + (result.stderr or result.stdout)[-16000:])
        os.replace(executable, output)
    return output
