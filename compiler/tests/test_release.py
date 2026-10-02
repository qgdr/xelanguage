"""Candidate contents, safe deterministic archives and clean consumer smoke tests."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compiler.release import (
    MANIFEST_NAME,
    NOTICE_NAME,
    REQUIRED_FILES,
    ROOT,
    ReleaseError,
    allowed_source,
    clean_environment,
    git_metadata,
    main,
    package_sources,
    sha256,
    smoke_archive,
    source_snapshot,
    verify_archive,
)
from compiler.version import PACKAGE_VERSION, SYNTAX_VERSION, VERSION
from compiler.xe_ast.ast import SCHEMA_VERSION

CC = shutil.which("cc") or ""
GIT = shutil.which("git") or ""


class ReleaseFixtures(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="xe-release-test-")
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name)
        self.root = self.temporary / "source"
        self.output = self.temporary / "candidates"
        for name in REQUIRED_FILES:
            self.file(name, f"source fixture: {name}\n")
        self.file("pyproject.toml", f'[project]\nname="xelanguage"\nversion="{PACKAGE_VERSION}"\n'
                  'license="Apache-2.0"\ndependencies=[]\n')
        self.file("uv.lock", f'[[package]]\nname="xelanguage"\nversion="{PACKAGE_VERSION}"\n')
        self.file("compiler/xe_ast/new_untracked_feature.py", "VALUE = 42\n")

    def file(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)
        return path

    def package(self):
        return package_sources(self.output, root=self.root)

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(list(arguments))
        return status, out.getvalue(), err.getvalue()

    def archive_with(self, result, *, extra=None, replace=None, remove=None):
        """Keep the original manifest to test that changes cannot go unnoticed."""
        path = self.temporary / "changed.tar.gz"
        with tarfile.open(result["archive"], "r:gz") as original, tarfile.open(path, "w:gz") as changed:
            for member in original:
                if remove and member.name.endswith("/" + remove):
                    continue
                stream = original.extractfile(member)
                assert stream is not None
                data = stream.read()
                if replace and member.name.endswith("/" + replace[0]):
                    data = replace[1]
                    member.size = len(data)
                changed.addfile(member, io.BytesIO(data))
            if extra:
                info, data = extra
                changed.addfile(info, io.BytesIO(data))
        return path


class SourceCandidateTests(ReleaseFixtures):
    def test_manifest_records_working_tree_versions_license_and_hashes(self):
        metadata = {"head": "1234567890abcdef", "dirty": True, "available": True}
        with patch("compiler.release.git_metadata", return_value=metadata):
            result = self.package()
        manifest, selected, checked = verify_archive(Path(result["archive"]))
        self.assertTrue(checked)
        self.assertEqual(result["sha256"], sha256(Path(result["archive"]).read_bytes()))
        self.assertEqual(json.loads(Path(result["manifest"]).read_text()), manifest)
        self.assertEqual((manifest["version"], manifest["package_version"], manifest["syntax_version"],
                          manifest["ast_schema_version"]),
                         (VERSION, PACKAGE_VERSION, SYNTAX_VERSION, SCHEMA_VERSION))
        self.assertEqual(manifest["git"], metadata)
        self.assertEqual(manifest["source_snapshot"], "working-tree")
        self.assertEqual(manifest["release_status"], "local-candidate")
        self.assertFalse(manifest["publication_authorized"])
        self.assertEqual(manifest["license_status"], "Apache-2.0")
        self.assertIn("compiler/xe_ast/new_untracked_feature.py", selected)
        self.assertEqual(selected["compiler/xe_ast/new_untracked_feature.py"], b"VALUE = 42\n")
        self.assertIn(b"not an additional restriction", selected[NOTICE_NAME])
        self.assertEqual(manifest["files"]["xe"]["mode"], 0o755)
        self.assertEqual(manifest["files"]["LICENSE"]["sha256"], sha256(selected["LICENSE"]))

    def test_archive_is_deterministic_despite_mtime_and_source_permissions(self):
        first = self.package()
        previous = Path(first["archive"]).read_bytes()
        for path in self.root.rglob("*"):
            if path.is_file():
                os.utime(path, (1000000000, 1000000000))
                path.chmod(0o600)
        self.assertEqual(self.package()["sha256"], first["sha256"])
        self.assertEqual(Path(first["archive"]).read_bytes(), previous)
        self.assertEqual(previous[4:8], b"\0\0\0\0")  # gzip timestamp
        with tarfile.open(first["archive"], "r:gz") as archive:
            names = []
            for member in archive:
                names.append(member.name)
                self.assertEqual((member.mtime, member.uid, member.gid, member.uname, member.gname),
                                 (0, 0, 0, "", ""))
            self.assertEqual(names, sorted(names))

    def test_explicit_allowlist_excludes_caches_keys_env_and_local_files(self):
        excluded = (
            ".git/config", ".venv/bin/python", "target/program", "ignore/private.py",
            "compiler/__pycache__/release.cpython-313.pyc", "compiler/.env", "compiler/secrets.py",
            "compiler/private.key", "examples/private.pem", "examples/id_rsa", ".aws/credentials",
            "notes.md", "compiler/generated.o", ".github/private/token.json",
            "compiler/build/generated.py", "bootstrap/dist/generated.xe", "examples/wheels/generated.c",
        )
        for name in excluded:
            self.file(name, "PRIVATE MUST NOT BE PACKAGED\n")
        public = (
            ".github/workflows/release-check.yml", "examples/ffi/functions.c",
            "examples/toolchain/xe.toml", "examples/calculator/input.calc", "doc/37.md",
            "CHANGELOG.md", "RELEASE.md", "ARCHITECTURE.md", "CONTRIBUTING.md", ".gitignore", ".python-version",
        )
        for name in public:
            self.file(name, "public fixture\n")
        selected = source_snapshot(self.root)
        for name in excluded:
            self.assertNotIn(name, selected, name)
        for name in public:
            self.assertIn(name, selected, name)
        self.assertTrue(all(b"PRIVATE" not in data for data in selected.values()))

    def test_allowlist_rejects_traversal_and_hidden_configuration(self):
        for name in ("/compiler/x.py", "compiler/../x.py", "compiler//x.py", "compiler/./x.py",
                     "compiler\\x.py", ".git/config", "examples/.env", "compiler/.private/x.py",
                     ".github/workflows/.secret.yml"):
            self.assertFalse(allowed_source(name), name)

    @unittest.skipUnless(hasattr(os, "symlink"), "平台需要符号链接支持")
    def test_source_file_directory_and_launcher_symlinks_are_rejected(self):
        external = self.temporary / "external.py"
        external.write_text("secret\n")
        for name, target in (("compiler/link.py", external), ("examples/linked", self.temporary),
                             ("xe", external)):
            with self.subTest(name=name):
                path = self.root / name
                if path.exists():
                    path.unlink()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(target)
                with self.assertRaisesRegex(ReleaseError, "符号链接"):
                    self.package()
                path.unlink()
        # Excluded virtualenvs may legitimately use links; they are never read.
        (self.root / ".venv").symlink_to(self.temporary)
        self.file("xe", "launcher\n")
        self.package()

    def test_outputs_cannot_be_source_directory_and_existing_source_is_preserved(self):
        original = (self.root / "compiler/__main__.py").read_bytes()
        for destination in (self.root, self.root / "compiler", self.root / "examples/releases", self.root / "target"):
            with self.subTest(destination=destination), self.assertRaisesRegex(ReleaseError, "覆盖源码"):
                package_sources(destination, root=self.root)
        self.assertEqual((self.root / "compiler/__main__.py").read_bytes(), original)
        package_sources(self.root / "target/releases", root=self.root)

    @unittest.skipUnless(hasattr(os, "symlink"), "平台需要符号链接支持")
    def test_symlink_output_directory_or_existing_destination_is_rejected(self):
        self.output.mkdir()
        link = self.temporary / "linked-output"
        link.symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(ReleaseError, "符号链接"):
            package_sources(link / "child", root=self.root)
        target = self.root / "compiler/__main__.py"
        archive = self.output / f"xelanguage-{VERSION}-source.tar.gz"
        archive.symlink_to(target)
        previous = target.read_bytes()
        with self.assertRaises(ReleaseError):
            self.package()
        self.assertEqual(target.read_bytes(), previous)
        self.assertFalse((self.output / f"xelanguage-{VERSION}-manifest.json").exists())

    def test_missing_source_version_or_license_metadata_fails_before_output(self):
        (self.root / "NOTICE").unlink()
        with self.assertRaisesRegex(ReleaseError, "NOTICE"):
            self.package()
        self.file("NOTICE", "notice\n")
        self.file("pyproject.toml", '[project]\nversion="0.1.0"\nlicense="Apache-2.0"\n')
        with self.assertRaisesRegex(ReleaseError, "版本"):
            self.package()
        self.file("pyproject.toml", f'[project]\nversion="{PACKAGE_VERSION}"\n')
        with self.assertRaisesRegex(ReleaseError, "Apache-2.0"):
            self.package()
        self.file("pyproject.toml", 'project=42\n')
        with self.assertRaisesRegex(ReleaseError, "project"):
            self.package()
        self.file("pyproject.toml", f'[project]\nversion="{PACKAGE_VERSION}"\n'
                  'license="Apache-2.0"\ndependencies=["not-installed"]\n')
        with self.assertRaisesRegex(ReleaseError, "运行时依赖"):
            self.package()
        self.assertFalse(self.output.exists())

    def test_require_clean_without_git_fails_before_writing(self):
        with self.assertRaisesRegex(ReleaseError, "require-clean"):
            package_sources(self.output, root=self.root, require_clean=True)
        self.assertFalse(self.output.exists())

    def test_mismatched_development_lock_fails_before_writing(self):
        self.file("uv.lock", '[[package]]\nname="xelanguage"\nversion="0.1.0"\n')
        with self.assertRaisesRegex(ReleaseError, "uv.lock"):
            self.package()
        self.assertFalse(self.output.exists())

    def test_git_is_read_only_optional_metadata(self):
        self.assertEqual(git_metadata(self.root), {"head": None, "dirty": None, "available": False})
        (self.root / ".git").mkdir()
        with patch("compiler.release.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, "abc123\n", ""),
            subprocess.CompletedProcess([], 0, "?? compiler/new.py\n", ""),
        ]) as command:
            self.assertEqual(git_metadata(self.root), {"head": "abc123", "dirty": True, "available": True})
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args_list[0].args[0][-2:], ["rev-parse", "HEAD"])
        self.assertEqual(command.call_args_list[1].args[0][-3:], ["status", "--porcelain", "--untracked-files=normal"])
        self.assertEqual(command.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(command.call_args.kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")
        with patch("compiler.release.subprocess.run", side_effect=FileNotFoundError("no git")):
            self.assertFalse(git_metadata(self.root)["available"])


@unittest.skipUnless(GIT, "干净提交快照验收需要 Git")
class CommittedSnapshotTests(ReleaseFixtures):
    """Git writes only inside a new TemporaryDirectory fixture, never the project."""

    def setUp(self):
        super().setUp()
        self.environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        self.environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        self.file(".gitignore", "compiler/local_ignored.py\n")
        self.git("init", "--initial-branch=main")
        self.git("add", ".")
        self.git("-c", "user.name=Release Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgSign=false", "-c", f"core.hooksPath={os.devnull}",
                 "commit", "--no-gpg-sign", "-m", "isolated release fixture")

    def git(self, *arguments):
        return subprocess.run([GIT, "-C", str(self.root), *arguments], env=self.environment,
                              check=True, capture_output=True, text=True, timeout=10)

    def test_clean_commit_is_bound_to_manifest_and_repeated_package(self):
        # An activated Git worktree/index must not redirect the release query.
        with patch.dict(os.environ, {"GIT_DIR": str(self.temporary / "missing-git-dir"),
                                     "GIT_WORK_TREE": str(self.temporary / "not-this-source"),
                                     "GIT_INDEX_FILE": str(self.temporary / "missing-index")}):
            result = package_sources(self.output, root=self.root, require_clean=True)
        manifest, selected, verified = verify_archive(Path(result["archive"]))
        self.assertTrue(verified)
        self.assertEqual(manifest["source_snapshot"], "git-head")
        self.assertEqual(manifest["git"]["head"], self.git("rev-parse", "HEAD").stdout.strip())
        self.assertFalse(manifest["git"]["dirty"])
        self.assertIn(b"exact committed HEAD", selected[NOTICE_NAME])
        self.assertNotIn(b"uncommitted files", selected[NOTICE_NAME])
        self.assertEqual(selected["compiler/xe_ast/new_untracked_feature.py"], b"VALUE = 42\n")
        self.assertEqual(package_sources(self.output, root=self.root, require_clean=True)["sha256"], result["sha256"])

    def test_modified_staged_or_untracked_files_reject_before_output(self):
        path = self.root / "compiler/__main__.py"
        original = path.read_bytes()
        path.write_text("changed\n")
        with self.assertRaisesRegex(ReleaseError, "require-clean"):
            package_sources(self.output, root=self.root, require_clean=True)
        self.git("add", "compiler/__main__.py")
        with self.assertRaisesRegex(ReleaseError, "require-clean"):
            package_sources(self.output, root=self.root, require_clean=True)
        path.write_bytes(original)
        self.git("add", "compiler/__main__.py")
        self.file("untracked.txt", "not a source file, but the tree is not clean\n")
        with self.assertRaisesRegex(ReleaseError, "require-clean"):
            package_sources(self.output, root=self.root, require_clean=True)
        self.assertFalse(self.output.exists())

    def test_git_ignored_eligible_source_cannot_be_described_as_head(self):
        self.file("compiler/local_ignored.py", "local-only source\n")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        with self.assertRaisesRegex(ReleaseError, "未记录在 HEAD"):
            package_sources(self.output, root=self.root, require_clean=True)
        self.assertFalse(self.output.exists())
        # Local worktree candidates intentionally keep eligible untracked files.
        local = package_sources(self.output, root=self.root)
        manifest, selected, _ = verify_archive(Path(local["archive"]))
        self.assertEqual(manifest["source_snapshot"], "working-tree")
        self.assertIn("compiler/local_ignored.py", selected)

    def test_assume_unchanged_does_not_hide_mismatched_committed_contents(self):
        self.git("update-index", "--assume-unchanged", "compiler/__main__.py")
        self.file("compiler/__main__.py", "different source despite clean status\n")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        with self.assertRaisesRegex(ReleaseError, "提交内容不一致"):
            package_sources(self.output, root=self.root, require_clean=True)
        self.assertFalse(self.output.exists())

    def test_git_metadata_change_during_snapshot_is_rejected(self):
        before = git_metadata(self.root)
        after = {**before, "dirty": True}
        with patch("compiler.release.git_metadata", side_effect=[before, after]):
            with self.assertRaisesRegex(ReleaseError, "Git 状态发生变化"):
                package_sources(self.output, root=self.root, require_clean=True)
        self.assertFalse(self.output.exists())


class ArchiveSafetyTests(ReleaseFixtures):
    def test_sidecar_checksum_catches_changed_archive(self):
        result = self.package()
        path = Path(result["archive"])
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ReleaseError, "SHA-256"):
            verify_archive(path)

    def test_internal_hash_catches_changes_without_external_sidecar(self):
        result = self.package()
        changed = self.archive_with(result, replace=("compiler/__main__.py", b"different\n"))
        with self.assertRaisesRegex(ReleaseError, "SHA-256"):
            verify_archive(changed)
        changed = self.archive_with(result, remove="compiler/__main__.py")
        with self.assertRaisesRegex(ReleaseError, "文件集合"):
            verify_archive(changed)

    def test_traversal_absolute_links_devices_duplicate_and_unlisted_files_rejected(self):
        result = self.package()
        root = f"xelanguage-{VERSION}"
        members = []
        for name in (f"{root}/../escape", f"/{root}/compiler/x.py", f"{root}/compiler//x.py",
                     f"{root}/compiler\\x.py", f"{root}/compiler/.env", f"{root}/compiler/__main__.py"):
            members.append(tarfile.TarInfo(name))
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            member = tarfile.TarInfo(f"{root}/compiler/link.py")
            member.type = kind
            member.linkname = "/outside"
            members.append(member)
        for member in members:
            member.mode = 0o644
            with self.subTest(name=member.name, kind=member.type):
                changed = self.archive_with(result, extra=(member, b""))
                with self.assertRaises(ReleaseError):
                    verify_archive(changed)

    def test_malformed_manifest_is_a_friendly_cli_error(self):
        result = self.package()
        for content in (b"not json", b"[]", b'{"manifest_schema_version":1}', b'null',
                        b'{"manifest_schema_version":1,"archive_root":[]}',
                        b'{"manifest_schema_version":1,"archive_root":{}}'):
            with self.subTest(content=content):
                changed = self.archive_with(result, replace=(MANIFEST_NAME, content))
                status, out, err = self.cli("smoke", str(changed))
                self.assertEqual((status, out), (2, ""))
                self.assertIn("候选发布工具", err)
                self.assertNotIn("Traceback", err)

    def test_invalid_committed_snapshot_metadata_is_rejected(self):
        result = self.package()
        original = json.loads(Path(result["manifest"]).read_text())
        for snapshot, git in (([], original["git"]), ("unknown", original["git"]),
                              ("git-head", {"available": True, "dirty": True, "head": "a" * 40}),
                              ("git-head", {"available": True, "dirty": False, "head": "not-a-commit"})):
            with self.subTest(snapshot=snapshot, git=git):
                manifest = {**original, "source_snapshot": snapshot, "git": git}
                changed = self.archive_with(result, replace=(MANIFEST_NAME, json.dumps(manifest).encode()))
                status, out, err = self.cli("smoke", str(changed))
                self.assertEqual((status, out), (2, ""))
                self.assertIn("源码快照/Git 状态", err)
                self.assertNotIn("Traceback", err)

    def test_missing_c_compiler_and_cli_json_contract(self):
        result = self.package()
        with self.assertRaisesRegex(ReleaseError, "找不到 C 编译器"):
            smoke_archive(Path(result["archive"]), cc="xe-no-such-c-compiler")
        with patch("compiler.release.package_sources", return_value=result):
            status, out, err = self.cli("package", "--output-dir", str(self.output))
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(json.loads(out), result)
        with patch("compiler.release.package_sources", return_value=result) as package:
            self.cli("package", "--output-dir", str(self.output), "--require-clean")
            package.assert_called_once_with(self.output, require_clean=True)


class CleanConsumerTests(unittest.TestCase):
    def test_clean_environment_removes_python_virtualenv_and_user_site_inputs(self):
        with patch.dict(os.environ, {
            "PYTHONPATH": "/private/module-path", "PYTHONHOME": "/private/python-home",
            "PYTHONUSERBASE": "/private/user-site", "VIRTUAL_ENV": "/private/venv",
            "CONDA_PREFIX": "/private/conda", "UV_PROJECT_ENVIRONMENT": "/private/environment",
            "PATH": "/private/venv/bin" + os.pathsep + os.defpath,
        }):
            cleaned = clean_environment(Path("/base/python/bin/python3"))
        self.assertEqual([key for key in cleaned if key.startswith("PYTHON")], ["PYTHONNOUSERSITE"])
        self.assertEqual(cleaned["PYTHONNOUSERSITE"], "1")
        for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "UV_PROJECT_ENVIRONMENT"):
            self.assertNotIn(key, cleaned)
        self.assertNotIn("/private/venv/bin", cleaned["PATH"].split(os.pathsep))
        self.assertEqual(cleaned["PATH"].split(os.pathsep)[0], "/base/python/bin")

    @unittest.skipUnless(CC and sys.version_info >= (3, 13), "真实解包验收需要 Python 3.13+ 和 C 编译器")
    def test_real_working_tree_candidate_works_without_repository_virtualenv(self):
        with tempfile.TemporaryDirectory(prefix="xe-release-real-") as directory:
            result = package_sources(Path(directory), root=ROOT)
            with patch.dict(os.environ, {"PYTHONPATH": "/xe-invalid-import-path",
                                         "PYTHONHOME": "/xe-invalid-python-home",
                                         "VIRTUAL_ENV": "/xe-invalid-virtualenv"}):
                smoke = smoke_archive(Path(result["archive"]), cc=CC)
            self.assertTrue(smoke["passed"])
            self.assertTrue(smoke["checksum_verified"])
            self.assertFalse(smoke["uv_sync_required"])
            self.assertFalse(smoke["publication_authorized"])
            self.assertEqual(smoke["clean_python"],
                             str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()))
            self.assertIn("doctor", smoke["checks"])
            self.assertIn("legacy-script-help", smoke["checks"])
            self.assertIn("build-multiple-modules", smoke["checks"])
            self.assertIn("run-argument-boundaries", smoke["checks"])
            self.assertIn("run-c-ffi", smoke["checks"])
            self.assertIn("run-stdlib-multiple-modules", smoke["checks"])
            self.assertIn("reject-semantic-error", smoke["checks"])
            self.assertIn("reject-syntax-error", smoke["checks"])


if __name__ == "__main__":
    unittest.main()
