"""Current access documentation must contain real runnable core programs."""
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


@unittest.skipUnless(CC, "documentation execution requires a C compiler")
class CoreAccessDocumentationTests(unittest.TestCase):
    def execute(self, source, expected):
        assert CC is not None  # The class is skipped otherwise; narrow for type checking.
        with tempfile.TemporaryDirectory(prefix="xe-access-doc-") as directory:
            filename, output = Path(directory) / "main.xe", Path(directory) / "main"
            filename.write_text(source, encoding="utf-8")
            build_executable(filename, output, cc=CC)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)

    def test_access_spec_and_language_examples_are_not_zero_sample_checks(self):
        pages = {
            "docs/spec/box_access.md": ["20 99 99 99\n"],
            "docs/language/box-access.md": ["20 99 99 99\n"],
        }
        for name, expected in pages.items():
            blocks = [code for info, _, code in fenced_blocks(private_document(name).read_text(encoding="utf-8"))
                      if info == "xe-core"]
            self.assertEqual(len(blocks), len(expected), name)
            for index, (code, output) in enumerate(zip(blocks, expected)):
                with self.subTest(page=name, sample=index):
                    self.execute(code, output)

    def test_standalone_access_example(self):
        self.execute((ROOT / "examples/core/access.xe").read_text(encoding="utf-8"), "40 99 99\n")
