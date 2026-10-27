"""Public core CLI, source archive examples and cache boundaries for else.

These exercise the actual front end and C toolchain. A cached executable never
authorizes changed or invalid Xe source, and examples must remain nonempty.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compiler.driver import build
from compiler.project import project_at
from compiler.tests.documentation_support import private_document
from compiler.tests.test_repository import fenced_blocks
from compiler.toolchain import main
from compiler.xe_ast.language_policy import current_profile
from compiler.xe_ast.source import Diagnostic

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which(os.environ.get("CC", "gcc"))
EXAMPLE = ROOT / "examples/core/else.xe"
EXPECTED = "42 7 0 1\n0 0\n99\n"
REDUNDANT = '''enum Event { End, }
fn inspect(event:Event)->i32 {
    event ? { Event::End :> () -> 0, else => original:Event -> 0, }
}
fn main()->Unit { println("{}", inspect(Event::End)); }
'''


class CoreElsePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xe-core-else-pipeline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "main.xe"
        self.source.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(current_profile(), "core")

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(list(map(str, arguments)))
        self.assertEqual(current_profile(), "core")
        return status, stdout.getvalue(), stderr.getvalue()

    def test_public_ast_check_and_c_generation_accept_the_documented_example(self):
        status, output, error = self.cli("ast", self.source, "-o", "-")
        self.assertEqual(status, 0, error)
        tree = json.loads(output)
        self.assertEqual(tree["schema_version"], 1)
        self.assertIn('"WholeBranchArm"', output)
        self.assertEqual(self.cli("check", self.source)[0], 0)
        status, generated, error = self.cli("emit-c", self.source, "-o", "-")
        self.assertEqual(status, 0, error)
        self.assertIn("invalid enum tag", generated)

    @unittest.skipUnless(CC, "else execution requires a system C compiler")
    def test_source_is_rechecked_before_cache_and_invalid_run_never_executes_old_output(self):
        assert CC is not None
        project, output = project_at(self.root), self.root / "program"
        first = build(project, self.source, output, cc=CC)
        second = build(project, self.source, output, cc=CC)
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, EXPECTED, ""))
        previous = output.read_bytes()
        self.source.write_text(REDUNDANT, encoding="utf-8")
        with patch("compiler.driver.cc_identity") as identity, \
                patch("compiler.toolchain.subprocess.run") as execute:
            with self.assertRaises(Diagnostic) as caught:
                build(project, self.source, output, cc=CC)
            self.assertIn("else", caught.exception.message)
            identity.assert_not_called()
            status, stdout, stderr = self.cli("run", self.source, "--cc", CC, "-o", output)
            execute.assert_not_called()
        self.assertEqual(status, 1, stderr)
        self.assertNotIn(EXPECTED, stdout)
        self.assertIn("else", stderr)
        self.assertEqual(output.read_bytes(), previous, "failed Xe checking must preserve prior artifacts")

    @unittest.skipUnless(CC, "else documentation execution requires a system C compiler")
    def test_readable_chapter_has_one_complete_executable_example(self):
        text = private_document("docs/language/branch-delivery.md").read_text(encoding="utf-8")
        blocks = [code for info, _, code in fenced_blocks(text) if info == "xe-core"]
        self.assertEqual(len(blocks), 1, "do not replace runnable evidence with zero examples")
        # The private chapter is a historical snapshot. The public example may
        # be reformatted independently; both still execute against EXPECTED.
        self.source.write_text(blocks[0], encoding="utf-8")
        assert CC is not None
        output = self.root / "chapter"
        build(project_at(self.root), self.source, output, cc=CC)
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, EXPECTED, ""))
