"""构建驱动：生成 C、调用系统工具，再原子发布可执行文件。

不用 shell 拼接命令。失败保留旧可执行文件；生成 C 留在旁边方便排查。
语言诊断与外部工具失败分开，后者使用退出码 2。
"""
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from .backend_c import lower_program_to_c
from .modules import load_program


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
    if any(target.resolve() == source.resolve() or
           target.exists() and source.exists() and target.samefile(source) for target in targets):
        raise BuildError("输出路径不能覆盖输入源码")


def emit_c(source: Path, output: Path, check_borrows=True, warnings=None):
    protect_source(source, [output])
    source_info, tree = load_program(source)
    for filename in tree["_sources"]:
        protect_source(Path(filename), [output])
    generated = lower_program_to_c(source_info, tree, check_borrows, warnings=warnings)
    atomic_text(output, generated)
    return output


def build_executable(source: Path, output: Path, check_borrows=True,
                     cc="cc", extra_flags=(), warnings=None):
    c_path = output.with_name(output.name + ".c")
    protect_source(source, [output, c_path])
    source_info, tree = load_program(source)
    for filename in tree["_sources"]:
        protect_source(Path(filename), [output, c_path])
    generated = lower_program_to_c(source_info, tree, check_borrows, warnings=warnings)
    atomic_text(c_path, generated)
    return compile_generated(generated, c_path, output, cc=cc, extra_flags=extra_flags)


def compile_generated(generated: str, c_path: Path, output: Path, *, cc="cc",
                      extra_flags=(), timeout=30, published_hashes=None):
    """编译不可变的 C 快照；旧入口与统一工具链共用同一个外部工具边界。

私有 C 输入防止并发写可查看的 .c 文件改变此次实际编译内容。
可执行文件仍在同文件系统完成后原子发布，失败保留旧版本。
"""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".xe-build-", dir=output.parent) as directory:
        executable = Path(directory) / "program"
        private_c = Path(directory) / "input.c"
        private_c.write_text(generated, encoding="utf-8", newline="\n")
        command = [cc, "-std=c11", "-O0", "-g",
                   "-Werror=implicit-function-declaration", "-Werror=incompatible-pointer-types",
                   "-Werror=return-type", *extra_flags, str(private_c), "-o", str(executable)]
        if "#include <pthread.h>" in generated:
            command.insert(1, "-pthread")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as error:
            raise BuildError(f"找不到 C 编译器 {cc}，请安装 GCC/Clang 或使用 --cc 指定") from error
        except subprocess.TimeoutExpired as error:
            raise BuildError(f"C 编译器运行超过 {timeout:g} 秒；生成代码保留于 {c_path}") from error
        if result.returncode:
            raise BuildError(f"C 编译器未能生成程序；生成代码：{c_path}\n"
                             + (result.stderr or result.stdout)[-16000:])
        if published_hashes is not None:
            # 在原子发布前记录此次真正编译的二进制，而非稍后读取共享输出路径。
            # 否则并行发布可把另一个程序的哈希错误登记到本次缓存键下。
            # 只解析父目录：os.replace 会替换输出自身，而不是输出原符号链接的目标。
            published_hashes[str(output.parent.resolve() / output.name)] = hashlib.sha256(executable.read_bytes()).hexdigest()
        os.replace(executable, output)
    return output
