"""CLI 的临时文件全部在 TemporaryDirectory 中，与项目产物隔离。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from compiler.xe_ast.cli import main


class CliTests(unittest.TestCase):
    def run_cli(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(arguments)
        return status, out.getvalue(), err.getvalue()

    def test_json_file_and_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.xe"
            output = Path(directory) / "nested/main.json"
            source.write_text("fn main() { 42 }", encoding="utf-8")
            status, _, err = self.run_cli([str(source), "-o", str(output)])
            self.assertEqual(status, 0, err)
            data = json.loads(output.read_text())
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["syntax_version"], "xe-bootstrap-0.2")
            status, stdout, _ = self.run_cli([str(source), "-o", "-"])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stdout), data)

    def test_error_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "bad.xe", Path(directory) / "ast.json"
            source.write_text("fn main() { let x = ; }")
            output.write_text("previous successful output")
            status, stdout, stderr = self.run_cli(
                [str(source), "-o", str(output), "--diagnostic-format", "json"])
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(output.read_text(), "previous successful output")
            self.assertTrue(json.loads(stderr)["diagnostics"])
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()),
                             ["ast.json", "bad.xe"])

    def test_error_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "bad.xe", Path(directory) / "missing/ast.json"
            source.write_text("fn main() {")
            self.assertEqual(self.run_cli([str(source), "-o", str(output)])[0], 1)
            self.assertFalse(output.parent.exists())

    def test_missing_input_and_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "missing.xe"
            self.assertEqual(self.run_cli([str(source), "-o", "-"])[0], 2)
            source.write_bytes(b"\xff")
            self.assertEqual(self.run_cli([str(source), "-o", "-"])[0], 2)

    def test_cannot_overwrite_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.xe"
            source.write_text("fn main() {}")
            status, _, _ = self.run_cli([str(source), "-o", str(source)])
            self.assertEqual(status, 2)
            self.assertEqual(source.read_text(), "fn main() {}")

    def test_serialization_recursion_reports_error_without_traceback(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "deep.xe"
            source.write_text("fn f() { 1 }")
            # 宿主不同版本的 JSON 递归阈值不同，模拟边界而不依赖某个阈值。
            with patch("compiler.xe_ast.cli.json.dumps", side_effect=RecursionError):
                status, _, stderr = self.run_cli([str(source), "-o", "-"])
            self.assertEqual(status, 1)
            self.assertNotIn("Traceback", stderr)
