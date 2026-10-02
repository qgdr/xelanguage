"""Local source candidates, not a publishing service.

The archive is made from an explicit working-tree allowlist, not ``git archive``:
new compiler files must not silently disappear merely because they are untracked.
No Git state is changed and no network, package registry or release API is used.
Runtime dependencies are empty, so smoke testing needs only a clean Python 3.13+
and a C compiler; it deliberately does not reuse the repository's virtualenv.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from .version import PACKAGE_VERSION, SYNTAX_VERSION, VERSION
from .xe_ast.ast import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "release-manifest.json"
NOTICE_NAME = "CANDIDATE-NOTICE.txt"
MANIFEST_SCHEMA_VERSION = 1
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 10000

# Directory + extension allowlists keep caches, executables, local configuration
# and credentials out. New source formats require an intentional policy change.
ROOT_FILES = frozenset({
    "AGENTS.md", "Makefile", "README.md", "pyproject.toml", "uv.lock", "xe",
    "LICENSE", "NOTICE", "CHANGELOG.md", "RELEASE.md", "ARCHITECTURE.md",
    "CONTRIBUTING.md", ".gitignore", ".python-version",
})
DIRECTORY_SUFFIXES = {
    "compiler": frozenset({".py", ".h", ".md"}),
    "bootstrap": frozenset({".py", ".xe", ".md"}),
    "stdlib": frozenset({".h", ".xe", ".md"}),
    "doc": frozenset({".md"}),
    "examples": frozenset({".xe", ".c", ".h", ".md", ".toml", ".calc"}),
    "tests": frozenset({".xe", ".md"}),
}
EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".venv", "target", "ignore", "__pycache__", "node_modules",
    ".aws", ".ssh", ".codex", ".agents", "build", "dist", "wheels",
})
SECRET_NAMES = frozenset({
    "credentials", "credentials.json", "secrets.json", "secrets.py", "credentials.py",
    "id_rsa", "id_ed25519", "authorized_keys", "known_hosts",
})
REQUIRED_FILES = frozenset({
    "xe", "README.md", "pyproject.toml", "uv.lock", "LICENSE", "NOTICE",
    "compiler/__init__.py", "compiler/__main__.py", "compiler/version.py",
    "compiler/runtime/xe_runtime.h",
})


class ReleaseError(Exception):
    """An actionable packaging/verification error, without an internal traceback."""


def allowed_source(name: str) -> bool:
    """Apply the same policy both before packaging and before extracting."""
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        return False
    if len(path.parts) == 1:
        return name in ROOT_FILES
    if any(part in EXCLUDED_DIRECTORIES for part in path.parts):
        return False
    if any(part.startswith(".") for part in path.parts):
        # Only checked-in workflow definitions are allowed below a hidden root.
        return (len(path.parts) == 3 and path.parts[:2] == (".github", "workflows")
                and path.suffix in {".yml", ".yaml"}
                and not path.name.startswith("."))
    if path.name.lower() in SECRET_NAMES or path.name.lower().startswith(".env"):
        return False
    return path.suffix in DIRECTORY_SUFFIXES.get(path.parts[0], frozenset())


def source_snapshot(root: Path) -> dict[str, bytes]:
    """Read selected regular files once; never follow a source symlink."""
    selected: dict[str, bytes] = {}

    def visit(path: Path) -> None:
        name = path.relative_to(root).as_posix()
        if path.name in EXCLUDED_DIRECTORIES or (path.name.startswith(".") and name != ".github"):
            return
        info = path.lstat()
        # Even a link with a harmless suffix can conceal a directory to follow.
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseError(f"候选源码不接受符号链接：{name}")
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)
        elif allowed_source(name):
            if not stat.S_ISREG(info.st_mode):
                raise ReleaseError(f"候选源码必须是普通文件：{name}")
            selected[name] = path.read_bytes()

    for name in sorted(ROOT_FILES | frozenset(DIRECTORY_SUFFIXES) | {".github"}):
        path = root / name
        if path.exists() or path.is_symlink():
            # Root dotfiles on the explicit allowlist need the same link check.
            if name in ROOT_FILES:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise ReleaseError(f"候选源码必须是普通文件，不能是符号链接：{name}")
                selected[name] = path.read_bytes()
            else:
                visit(path)
    missing = REQUIRED_FILES - selected.keys()
    if missing:
        raise ReleaseError("候选源码缺少必需文件：" + ", ".join(sorted(missing)))
    return dict(sorted(selected.items()))


def git_environment() -> dict[str, str]:
    """Query this repository without inherited overrides or index refresh writes."""
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"})
    return environment


def git_metadata(root: Path) -> dict[str, Any]:
    """Metadata only: a source tarball or a system without Git is also usable."""
    if not (root / ".git").exists():
        return {"head": None, "dirty": None, "available": False}
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10, check=True,
                              env=git_environment()).stdout.strip()
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
                                capture_output=True, text=True, timeout=10, check=True,
                                env=git_environment()).stdout
        return {"head": head, "dirty": bool(status), "available": True}
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None, "available": False}


def verify_clean_snapshot(root: Path, head: str, selected: dict[str, bytes]) -> None:
    """Read committed blobs, not the index, to bind every source byte to HEAD.

    A clean status can coexist with Git-ignored .py/.h files. They are useful in a
    local candidate, but must never be described as part of a tagged snapshot.
    One batch read avoids starting a separate Git process for every source file.
    Only Git's read-only object queries are used; the tool never stages/commits.
    """
    tree = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", head],
                          capture_output=True, timeout=10, check=True, env=git_environment()).stdout
    blobs: dict[str, bytes] = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        if kind == b"blob" and mode in {b"100644", b"100755"}:
            blobs[name.decode("utf-8")] = object_id
    missing = selected.keys() - blobs.keys()
    if missing:
        raise ReleaseError("干净提交包不能包含未记录在 HEAD 的源码：" + ", ".join(sorted(missing)))
    names = sorted(selected)
    objects = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"],
                             input=b"".join(blobs[name] + b"\n" for name in names),
                             capture_output=True, timeout=30, check=True, env=git_environment()).stdout
    stream = io.BytesIO(objects)
    for name in names:
        header = stream.readline().split()
        if len(header) != 3 or header[0] != blobs[name] or header[1] != b"blob":
            raise ReleaseError(f"无法读取候选源码的提交内容：{name}")
        data = stream.read(int(header[2]))
        if stream.read(1) != b"\n" or data != selected[name]:
            raise ReleaseError(f"候选源码与干净提交内容不一致：{name}")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reject_symlink_components(path: Path) -> None:
    # resolve() alone would hide the very symlink we want to reject.
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ReleaseError(f"候选包输出路径不能包含符号链接：{component}")


def write_atomic(path: Path, data: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ReleaseError(f"候选包输出必须是普通文件，不能是符号链接：{path}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".xe-candidate-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def package_sources(output_dir: Path, *, root: Path = ROOT, require_clean: bool = False) -> dict[str, Any]:
    """Same snapshot + Git metadata gives identical tar.gz bytes on repeated runs."""
    root = root.resolve()
    output_dir = output_dir.absolute()
    reject_symlink_components(output_dir)
    output_dir = output_dir.resolve()
    if output_dir.is_relative_to(root):
        relative = output_dir.relative_to(root)
        if len(relative.parts) < 2 or relative.parts[0] != "target":
            raise ReleaseError("仓库内候选包输出只能位于 target 的子目录，不能覆盖源码目录")
    metadata = git_metadata(root)
    if require_clean and (not metadata["available"] or metadata["dirty"] is not False):
        raise ReleaseError("--require-clean 需要可读取的 Git HEAD 且工作树无未提交/未跟踪修改")
    selected = source_snapshot(root)
    if require_clean:
        verify_clean_snapshot(root, metadata["head"], selected)
        if git_metadata(root) != metadata:
            raise ReleaseError("读取源码期间 Git 状态发生变化；请冻结修改后重新打包")
    project = tomllib.loads(selected["pyproject.toml"].decode("utf-8")).get("project")
    if not isinstance(project, dict):
        raise ReleaseError("pyproject.toml 需要 [project] 配置")
    if project.get("version") != PACKAGE_VERSION:
        raise ReleaseError("pyproject.toml 版本与 compiler.version.PACKAGE_VERSION 不一致")
    license_name = project.get("license")
    if license_name != "Apache-2.0":
        raise ReleaseError("候选包需要已确认的 Apache-2.0 元数据及 LICENSE/NOTICE")
    if project.get("dependencies", []) != []:
        raise ReleaseError("当前解包验收只支持无 Python 运行时依赖的源码包；需要显式更新安装流程")
    # Runtime smoke needs no uv, but the included development lock must still
    # name the same version. Otherwise a seemingly good bundle cannot use its
    # documented frozen developer install/CI commands.
    lock = tomllib.loads(selected["uv.lock"].decode("utf-8"))
    packages = lock.get("package", [])
    locked_project = [item for item in packages if isinstance(item, dict)
                      and item.get("name") == project.get("name")] if isinstance(packages, list) else []
    if len(locked_project) != 1 or locked_project[0].get("version") != PACKAGE_VERSION:
        raise ReleaseError("uv.lock 项目版本与 compiler.version.PACKAGE_VERSION 不一致")
    archive_root = f"xelanguage-{VERSION}"
    selected[NOTICE_NAME] = (
        f"Xe {VERSION}: local source release candidate, not a published release.\n"
        "This workflow does not create a Git tag, push, or publish externally.\n"
        "The included Apache-2.0 LICENSE and NOTICE govern use of this source.\n"
        "Candidate status is not an additional restriction on that license.\n"
        + ("The manifest records exact committed HEAD source contents.\n" if require_clean else
           "The manifest records working-tree contents, including uncommitted files.\n")
    ).encode("utf-8")
    if len(selected) + 1 > MAX_ARCHIVE_FILES or sum(map(len, selected.values())) > MAX_ARCHIVE_BYTES:
        raise ReleaseError("候选源码超过 64 MiB 或文件数量验收限制")
    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "version": VERSION, "package_version": PACKAGE_VERSION,
        "syntax_version": SYNTAX_VERSION, "ast_schema_version": SCHEMA_VERSION,
        "archive_root": archive_root, "release_status": "local-candidate",
        "publication_authorized": False, "license_status": "Apache-2.0",
        "git": metadata, "source_snapshot": "git-head" if require_clean else "working-tree",
        "files": {name: {"sha256": sha256(data), "size": len(data),
                         "mode": 0o755 if name == "xe" else 0o644}
                  for name, data in sorted(selected.items())},
    }
    manifest_data = json_bytes(manifest)
    if sum(map(len, selected.values())) + len(manifest_data) > MAX_ARCHIVE_BYTES:
        raise ReleaseError("候选源码及 manifest 超过 64 MiB 验收限制")
    selected[MANIFEST_NAME] = manifest_data
    compressed = io.BytesIO()
    # Both gzip and tar timestamps/owners/modes are normalized. No absolute path,
    # wall-clock time or host name is allowed into the deterministic payload.
    with gzip.GzipFile(fileobj=compressed, filename="", mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, data in sorted(selected.items()):
                info = tarfile.TarInfo(f"{archive_root}/{name}")
                info.size = len(data)
                info.mode = 0o755 if name == "xe" else 0o644
                info.mtime = info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(data))
    payload = compressed.getvalue()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{archive_root}-source.tar.gz"
    checksum_path = archive_path.with_name(archive_path.name + ".sha256")
    manifest_path = output_dir / f"{archive_root}-manifest.json"
    # Preflight all destinations before changing any prior candidate artifacts.
    for destination in (archive_path, checksum_path, manifest_path):
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ReleaseError(f"候选包输出必须是普通文件：{destination}")
    digest = sha256(payload)
    write_atomic(archive_path, payload)
    write_atomic(checksum_path, f"{digest}  {archive_path.name}\n".encode("ascii"))
    write_atomic(manifest_path, manifest_data)
    return {"archive": str(archive_path), "sha256": digest, "checksum": str(checksum_path),
            "manifest": str(manifest_path), "files": len(manifest["files"]),
            "version": VERSION, "git": manifest["git"], "source_snapshot": manifest["source_snapshot"],
            "release_status": "local-candidate",
            "publication_authorized": False, "license_status": "Apache-2.0"}


def verify_archive(archive_path: Path) -> tuple[dict[str, Any], dict[str, bytes], bool]:
    """Validate everything before extraction or executing any packaged code.

    Hashes detect accidental changes, not authenticity of an untrusted publisher:
    smoke intentionally executes the supplied compiler and requires a trusted
    locally produced candidate. Links, devices and path traversal are never used.
    """
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ReleaseError("候选包必须是普通 tar.gz 文件，不能是符号链接")
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ReleaseError("候选包超过 64 MiB 验收限制")
    checksum_path = archive_path.with_name(archive_path.name + ".sha256")
    checksum_verified = False
    if checksum_path.exists() or checksum_path.is_symlink():
        if checksum_path.is_symlink() or not checksum_path.is_file():
            raise ReleaseError("SHA-256 清单必须是普通文件")
        expected = f"{sha256(archive_path.read_bytes())}  {archive_path.name}\n"
        if checksum_path.read_text(encoding="ascii") != expected:
            raise ReleaseError("候选包的 SHA-256 校验失败")
        checksum_verified = True
    selected: dict[str, bytes] = {}
    roots: set[str] = set()
    total = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            parts = member.name.split("/")
            if (not member.isfile() or len(parts) < 2 or "\\" in member.name
                    or any(part in {"", ".", ".."} for part in parts)
                    or not re.fullmatch(r"xelanguage-[A-Za-z0-9.-]+", parts[0])):
                raise ReleaseError(f"候选包包含不安全成员（路径/链接/非普通文件）：{member.name}")
            roots.add(parts[0])
            name = "/".join(parts[1:])
            if name in selected or (name not in {MANIFEST_NAME, NOTICE_NAME} and not allowed_source(name)):
                raise ReleaseError(f"候选包成员重复或不在源码白名单：{member.name}")
            total += member.size
            if (member.size < 0 or total > MAX_ARCHIVE_BYTES
                    or len(selected) >= MAX_ARCHIVE_FILES):
                raise ReleaseError("候选包展开后超过大小或文件数量限制")
            stream = archive.extractfile(member)
            if stream is None:
                raise ReleaseError(f"候选包成员无法读取：{member.name}")
            with stream:
                selected[name] = stream.read()
            if member.mode != (0o755 if name == "xe" else 0o644):
                raise ReleaseError(f"候选包成员权限不符合规则：{member.name}")
    if len(roots) != 1 or MANIFEST_NAME not in selected:
        raise ReleaseError("候选包需要唯一根目录和 release-manifest.json")
    manifest = json.loads(selected.pop(MANIFEST_NAME))
    if (not isinstance(manifest, dict)
            or manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION
            or not isinstance(manifest.get("archive_root"), str)
            or manifest.get("archive_root") not in roots
            or not isinstance(manifest.get("version"), str)
            or manifest.get("archive_root") != f"xelanguage-{manifest.get('version')}"
            or not isinstance(manifest.get("syntax_version"), str)
            or not isinstance(manifest.get("package_version"), str)
            or manifest.get("ast_schema_version") != SCHEMA_VERSION
            or manifest.get("release_status") != "local-candidate"
            or manifest.get("publication_authorized") is not False
            or manifest.get("license_status") != "Apache-2.0"):
        raise ReleaseError("候选包 manifest 格式或候选/许可声明不正确")
    snapshot = manifest.get("source_snapshot", "working-tree")
    git = manifest.get("git")
    if (not isinstance(snapshot, str) or snapshot not in {"working-tree", "git-head"} or not isinstance(git, dict)
            or snapshot == "git-head" and (git.get("available") is not True
                or git.get("dirty") is not False or not isinstance(git.get("head"), str)
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", git["head"]) is None)):
        raise ReleaseError("候选包 manifest 的源码快照/Git 状态声明不正确")
    records = manifest.get("files")
    if not isinstance(records, dict) or set(records) != set(selected):
        raise ReleaseError("候选包文件集合与 manifest 不一致")
    for name, data in selected.items():
        record = records[name]
        if (not isinstance(record, dict) or record.get("sha256") != sha256(data)
                or record.get("size") != len(data)
                or record.get("mode") != (0o755 if name == "xe" else 0o644)):
            raise ReleaseError(f"候选包文件 SHA-256/大小/权限校验失败：{name}")
    if not (REQUIRED_FILES | {NOTICE_NAME}).issubset(selected):
        raise ReleaseError("候选包缺少必需源码或候选声明")
    return manifest, selected, checksum_verified


def clean_environment(base_python: Path) -> dict[str, str]:
    """Do not borrow import paths, user-site packages or an activated virtualenv."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PYTHON") and key not in {
                       "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
                       "UV_PROJECT_ENVIRONMENT",
                   }}
    environment["PYTHONNOUSERSITE"] = "1"
    virtual_roots = [Path(value).resolve() for value in
                     (os.environ.get("VIRTUAL_ENV"), sys.prefix if sys.prefix != sys.base_prefix else None)
                     if value]
    search = [str(base_python.parent)]
    for entry in os.environ.get("PATH", os.defpath).split(os.pathsep):
        if entry and not any(Path(entry).resolve().is_relative_to(root) for root in virtual_roots):
            search.append(entry)
    environment["PATH"] = os.pathsep.join(search)
    return environment


def smoke_archive(archive_path: Path, *, cc: str = "cc") -> dict[str, Any]:
    manifest, selected, checksum_verified = verify_archive(archive_path)
    base_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    environment = clean_environment(base_python)
    compiler = shutil.which(cc, path=environment["PATH"])
    if compiler is None:
        raise ReleaseError(f"找不到 C 编译器：{cc}")
    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="xe-release-smoke-") as directory:
        temporary = Path(directory)
        unpacked = temporary / manifest["archive_root"]
        outside = temporary / "consumer outside package"
        outside.mkdir()
        for name, data in selected.items():
            target = unpacked / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if name == "xe" else 0o644)
        (unpacked / MANIFEST_NAME).write_bytes(json_bytes(manifest))
        entry = unpacked / "xe"
        # POSIX tests the executable/shebang itself. Windows source candidates
        # can still use the base interpreter explicitly; it is not a 1.0 promise.
        command = [str(entry)] if os.name != "nt" else [str(base_python), str(entry)]

        def run(name: str, arguments: list[str], *, status: int = 0,
                stdout: str | None = None, executable: list[str] | None = None) -> subprocess.CompletedProcess[str]:
            result = subprocess.run([*(executable or command), *arguments], cwd=outside,
                                    env=environment, capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=60)
            if result.returncode != status or (stdout is not None and result.stdout != stdout):
                raise ReleaseError(f"解包验收失败：{name}（退出码 {result.returncode}，期望 {status}）\n"
                                   + (result.stdout + result.stderr)[-8000:])
            checks.append(name)
            return result

        probe = run("clean-python", ["-I", "-c", "import sys; print('.'.join(map(str,sys.version_info[:2])))"],
                    executable=[str(base_python)])
        if tuple(map(int, probe.stdout.strip().split("."))) < (3, 13):
            raise ReleaseError("解包验收需要独立 Python 3.13+；不会隐式下载解释器")
        version = run("version", ["--version"])
        if f"xe {manifest['version']} ({manifest['syntax_version']}, stage0)" not in version.stdout:
            raise ReleaseError("包内 xe --version 与 manifest 不一致")
        run("legacy-script-help", ["--help"],
            executable=[str(base_python), str(unpacked / "compiler/main.py")])
        doctor = run("doctor", ["doctor", "--cc", compiler, "--message-format=json"])
        identity = json.loads(doctor.stdout)
        if Path(identity["python"]).resolve() != base_python or Path(identity["project"]).resolve() != outside:
            raise ReleaseError("解包验收错误借用了虚拟环境或未保留调用者工作目录")
        run("check-globals", ["check", str(unpacked / "examples/globals/main.xe"), "--message-format=json"])
        program = outside / "module program"
        run("build-multiple-modules", ["build", "--manifest-path", str(unpacked / "examples/toolchain"),
                                       "--cc", compiler, "-o", str(program), "--message-format=json"])
        run("execute-built-program", [], executable=[str(program)], stdout="square(7) = 49\n")
        arguments_program = outside / "argument program"
        run("run-argument-boundaries", ["run", str(unpacked / "examples/args/main.xe"), "--cc", compiler,
                                         "-o", str(arguments_program), "--", "two words", "", "--help"],
            stdout=f"{arguments_program}\ntwo words\n\n--help\n")
        for name, path in (("reject-semantic-error", "tests/fails/semantic_unknown_name.xe"),
                           ("reject-syntax-error", "tests/syntax_fails/missing_expression.xe")):
            rejected = run(name, ["check", str(unpacked / path), "--message-format=json"], status=1)
            diagnostics = json.loads(rejected.stderr).get("diagnostics", [])
            if not any(item.get("severity") == "error" and item.get("code", "").startswith("XE-")
                       and item.get("span") for item in diagnostics):
                raise ReleaseError(f"解包验收缺少稳定的源码诊断：{name}")
        run("run-c-ffi", ["run", str(unpacked / "examples/ffi/main.xe"), "--cc", compiler,
                          "--link-input", str(unpacked / "examples/ffi/functions.c"),
                          "-o", str(outside / "ffi program")], stdout="sum=42 after=41\n")
        input_text = "let hello = 42;\n中😀 _next\0done\n"
        (outside / "input sample.xe").write_text(input_text, encoding="utf-8")
        run("run-stdlib-multiple-modules", ["run", str(unpacked / "examples/source_scan/src/main.xe"),
                                             "--cc", compiler, "-o", str(outside / "source scan"),
                                             "--", "input sample.xe", "word report.txt"],
            stdout=f"bytes={len(input_text.encode())} chars={len(input_text)} lines=2 words=4\n")
        if (outside / "word report.txt").read_text(encoding="utf-8") != "let\nhello\n_next\ndone\n":
            raise ReleaseError("解包标准库验收的 UTF-8/NUL/文件输出错误")
    return {"archive": str(archive_path.resolve()), "version": manifest["version"],
            "passed": True, "checks": checks, "checksum_verified": checksum_verified,
            "clean_python": str(base_python), "cc": compiler, "uv_sync_required": False,
            "source_snapshot": manifest.get("source_snapshot", "working-tree"),
            "release_status": "local-candidate", "publication_authorized": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Xe 本地源码候选包与解包验收；不发布、不打 tag、不 push")
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package", help="打包当前工作树（包含未提交的白名单源码）")
    package.add_argument("--output-dir", type=Path, default=Path("target/releases"))
    package.add_argument("--require-clean", action="store_true",
                         help="最终提交快照：拒绝脏/未跟踪状态，并逐文件核对 Git HEAD 内容")
    smoke = commands.add_parser("smoke", help="验证可信本地候选包并在包外目录运行验收")
    smoke.add_argument("archive", type=Path)
    smoke.add_argument("--cc", default=os.environ.get("CC", "cc"))
    args = parser.parse_args(argv)
    try:
        result = (package_sources(args.output_dir, require_clean=args.require_clean) if args.command == "package"
                  else smoke_archive(args.archive, cc=args.cc))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ReleaseError, OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"候选发布工具：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
