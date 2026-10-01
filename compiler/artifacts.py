"""仅管理统一 xe 工具创建的产物；clean 不递归删除整个 target。

每个动作/输出有独立收据，记录内容哈希。手动更改的文件和 target 外输出
不被 clean 删除。收据不是可信代码或安全沙箱，也不是包依赖锁文件。
"""
import hashlib
import json
from pathlib import Path

from .xe_ast.build import BuildError, atomic_text

SCHEMA = 1


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.target = self.root / "target"
        self.directory = self.target / ".xe-tools"
        # 不沿缓存目录的符号链接读取/删除其他项目的数据。
        if self.target.is_symlink() or self.directory.is_symlink():
            raise BuildError("工具管理的 target 和 .xe-tools 目录不能是符号链接")

    def receipt_path(self, kind: str, output: Path) -> Path:
        key = hashlib.sha256(str(output.resolve()).encode("utf-8")).hexdigest()[:24]
        return self.directory / f"{kind}-{key}.json"

    def read(self, kind: str, output: Path) -> dict | None:
        path = self.receipt_path(kind, output)
        if not path.is_file() or path.is_symlink():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            return None  # 损坏缓存只导致重新构建，不成为编译失败。
        if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA or
                data.get("root") != str(self.root) or data.get("kind") != kind or
                data.get("output") != str(output.resolve()) or not isinstance(data.get("files"), dict)):
            return None
        return data

    def record(self, kind: str, output: Path, files: list[Path], *, expected_hashes=None, **metadata) -> Path:
        path = self.receipt_path(kind, output)
        data = {**metadata, "schema_version": SCHEMA, "root": str(self.root),
                "kind": kind, "output": str(output.resolve()),
                "files": {str(file.resolve()): (expected_hashes[str(file.resolve())]
                          if expected_hashes and str(file.resolve()) in expected_hashes else digest(file))
                          for file in files}}
        atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return path

    def clean(self, dry_run: bool = False) -> dict:
        """先核实每个文件，再删明确产物；未知/已修改文件保持原样。"""
        result = {"removed": [], "preserved": [], "dry_run": dry_run}
        if not self.directory.exists():
            return result
        for receipt in sorted(self.directory.glob("*.json")):
            if receipt.is_symlink():
                result["preserved"].append(str(receipt)); continue
            try:
                data = json.loads(receipt.read_text(encoding="utf-8"))
                valid = (isinstance(data, dict) and data.get("schema_version") == SCHEMA and
                         data.get("root") == str(self.root) and isinstance(data.get("files"), dict))
            except (ValueError, UnicodeError):
                result["preserved"].append(str(receipt)); continue
            if not valid:
                result["preserved"].append(str(receipt)); continue
            remaining = False
            for name, expected in data["files"].items():
                file = Path(name)
                # 必须是 target 的真正后代，不跟随符号链接，绝不删目录。
                resolved = file.resolve()
                owned = (file.is_absolute() and file != self.target and
                         resolved.is_relative_to(self.target) and not file.is_symlink() and
                         not file.is_relative_to(self.target / "bootstrap") and
                         not resolved.is_relative_to(self.target / "bootstrap"))
                if not owned:
                    result["preserved"].append(str(file)); remaining = True; continue
                if not file.exists():
                    continue
                if not file.is_file() or not isinstance(expected, str) or digest(file) != expected:
                    result["preserved"].append(str(file)); remaining = True; continue
                result["removed"].append(str(file))
                if not dry_run:
                    file.unlink()
            if not remaining:
                result["removed"].append(str(receipt))
                if not dry_run:
                    receipt.unlink()
        # 不猜测其他目录用途，不删未知文件或自举项目产物。
        return result
