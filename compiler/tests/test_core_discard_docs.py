"""DEC-024 examples are runnable core programs, not empty document checks."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.tests.documentation_support import private_document
from compiler.tests.test_repository import fenced_blocks
from compiler.xe_ast.build import build_executable

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which(os.environ.get("CC", "gcc"))


@unittest.skipUnless(CC, "discard documentation execution needs a C compiler")
class CoreDiscardDocumentationTests(unittest.TestCase):
    def execute(self, code, expected):
        assert CC is not None
        with tempfile.TemporaryDirectory(prefix="xe-discard-doc-") as directory:
            source, output = Path(directory) / "main.xe", Path(directory) / "program"
            source.write_text(code, encoding="utf-8")
            build_executable(source, output, cc=CC)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)

    def test_decision_has_one_complete_verified_example(self):
        page = private_document("docs/decisions/DEC-024-discard-owned-input.md")
        blocks = [code for info, _, code in fenced_blocks(page.read_text(encoding="utf-8")) if info == "xe-core"]
        self.assertEqual(len(blocks), 1, "do not pass silently if the example disappears")
        self.execute(blocks[0], "body\nafter\n7\n")

    def test_standalone_example(self):
        self.execute((ROOT / "examples/core/discard.xe").read_text(encoding="utf-8"),
                     "handling pipe\nhandling text\n42\nbefore return\ndone\n")
