"""工具链的文件/项目定位，不定义新的 Xe 语法或依赖解析规则。

xe.toml、src/main.xe、src/lib.xe、src/bin/name.xe 沿用第 09 章。
模块和依赖仍交给 xe_ast.modules；此处只选择入口和产物目录。
"""
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .xe_ast.build import BuildError


def find_manifest(directory: Path) -> Path | None:
    directory = directory.resolve()
    return next((parent / "xe.toml" for parent in (directory, *directory.parents)
                 if (parent / "xe.toml").is_file()), None)


@dataclass(frozen=True)
class Project:
    root: Path
    manifest: Path | None
    metadata: dict

    @property
    def source_root(self) -> Path:
        return self.root / "src" if self.manifest else self.root

    @property
    def name(self) -> str:
        return self.metadata.get("package", {}).get("name", self.root.name)

    def artifact_name(self, source: Path) -> str:
        name = self.name if self.manifest and source == self.source_root / "main.xe" else source.stem
        # 包显示名称不是文件路径；不允许名称把产物带出 target。
        return re.sub(r"[^\w.-]", "_", name, flags=re.ASCII).strip(".") or "program"

    @property
    def target(self) -> Path:
        return self.root / "target"


def project_at(source: Path | None = None, manifest: Path | None = None) -> Project:
    """显式文件无清单时独立成项目；无参数时从 cwd 向上找清单。"""
    if manifest is not None:
        manifest = manifest.resolve()
        if manifest.is_dir():
            manifest = manifest / "xe.toml"
        if manifest.name != "xe.toml" or not manifest.is_file():
            raise BuildError("--manifest-path 需要存在的 xe.toml 文件或含它的目录")
    elif source is not None:
        manifest = find_manifest(source if source.is_dir() else source.parent)
    else:
        manifest = find_manifest(Path.cwd())
    root = manifest.parent if manifest else (source if source and source.is_dir() else
           source.parent if source else Path.cwd()).resolve()
    data = {}
    if manifest:
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            raise BuildError(f"{manifest}: xe.toml 格式错误：{error}") from error
        package = data.get("package", {})
        if not isinstance(package, dict):
            raise BuildError(f"{manifest}: package 必须是 TOML 表")
        if "name" in package and (not isinstance(package["name"], str) or not package["name"].strip()):
            raise BuildError(f"{manifest}: package.name 需要非空字符串")
    return Project(root.resolve(), manifest, data)


def select_source(source: Path | None = None, *, manifest: Path | None = None,
                  binary: str | None = None, library: bool = False) -> tuple[Project, Path]:
    source = source.resolve() if source is not None else None
    project = project_at(source, manifest)
    if binary:
        if source is not None:
            raise BuildError("--bin 与显式源码路径不能同时使用")
        if not project.manifest:
            raise BuildError("--bin 需要 xe.toml 项目")
        if Path(binary).name != binary or binary in {".", ".."} or "\\" in binary:
            raise BuildError("--bin 只接受 src/bin/ 下的文件名，不接受路径")
        entry = project.source_root / "bin" / (binary + ".xe")
    elif source is not None and not source.is_dir():
        entry = source
    else:
        root = project.source_root
        candidates = [root / "lib.xe", root / "main.xe"] if library else [root / "main.xe"]
        entry = next((path for path in candidates if path.is_file()), candidates[-1])
    entry = entry.resolve()
    if not entry.is_file() or entry.suffix != ".xe":
        raise BuildError(f"找不到 .xe 入口：{entry}；请指定文件或创建 src/main.xe")
    if project.manifest and find_manifest(entry.parent) != project.manifest:
        raise BuildError("源码入口与 --manifest-path 不属于同一个包")
    return project, entry
