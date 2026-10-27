"""Execute the enum chapter, not merely check that its links exist."""
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


@unittest.skipUnless(CC, "enum documentation execution needs a C compiler")
class CoreEnumDocumentationTests(unittest.TestCase):
    def execute(self, code, expected):
        assert CC is not None
        with tempfile.TemporaryDirectory(prefix="xe-enum-doc-") as directory:
            source, output = Path(directory) / "main.xe", Path(directory) / "main"
            source.write_text(code, encoding="utf-8")
            build_executable(source, output, cc=CC)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)

    def test_two_complete_chapter_programs(self):
        text = private_document("docs/language/enums.md").read_text(encoding="utf-8")
        blocks = [code for info, _, code in fenced_blocks(text) if info == "xe-core"]
        expected = ["hello\n42\nend\n", "end\nnumber\n"]
        self.assertEqual(len(blocks), len(expected), "must not pass with no samples")
        for index, (code, output) in enumerate(zip(blocks, expected)):
            with self.subTest(sample=index):
                self.execute(code, output)

    def test_standalone_enum_program(self):
        self.execute((ROOT / "examples/core/enums.xe").read_text(encoding="utf-8"),
                     "text: hello\n1\n42 0\n")
