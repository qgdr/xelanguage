"""The CLI, AST envelope and locked project must describe one release."""
import subprocess
import sys
import tomllib
from pathlib import Path

from compiler.tests.legacy_support import LegacyTestCase
from compiler.version import PACKAGE_VERSION, RELEASE_NAME, SYNTAX_VERSION, VERSION
from compiler.xe_ast.ast import SCHEMA_VERSION, document
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Source

ROOT = Path(__file__).resolve().parents[2]


class ReleaseMetadataTests(LegacyTestCase):
    def test_legacy_script_entrypoint_works_outside_repository(self):
        completed = subprocess.run([sys.executable, str(ROOT / "compiler/main.py"), "--help"],
                                   cwd=ROOT.parent, capture_output=True, text=True, timeout=10)
        self.assertEqual((completed.returncode, completed.stderr), (0, ""))
        self.assertIn("usage:", completed.stdout)

    def test_project_and_lock_match_python_version(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        locked = next(item for item in lock["package"] if item["name"] == "xelanguage")
        self.assertEqual(project["version"], PACKAGE_VERSION)
        self.assertEqual(locked["version"], PACKAGE_VERSION)
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual((RELEASE_NAME, VERSION, PACKAGE_VERSION, SYNTAX_VERSION),
                         ("Xe Core Preview 1", "0.1.0-alpha.1", "0.1.0a1", "xe-core-0.1"))

    def test_cli_and_ast_use_public_contract(self):
        completed = subprocess.run([sys.executable, "-m", "compiler", "--version"], cwd=ROOT,
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual((completed.returncode, completed.stderr), (0, ""))
        self.assertEqual(completed.stdout, f"xe {VERSION} ({SYNTAX_VERSION}, stage0)\n")
        source = Source("fn main() {}")
        envelope = document(source, parse_source(source.text))
        self.assertEqual(envelope["syntax_version"], SYNTAX_VERSION)
        self.assertEqual(envelope["schema_version"], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 1)
