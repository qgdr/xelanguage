"""A real core program against independent language and algorithm references.

Only bounded safe graphs are executed. This suite tests behavior and packaging,
not a universal memory-safety or compiler-correctness claim.
"""
import contextlib
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.tests.documentation_support import private_example
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.semantic import check_source

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "examples/dependency-planner"
CC = shutil.which(os.environ.get("CC", "gcc"))
REFERENCES = CC and shutil.which("g++") and shutil.which("rustc")


class DependencyPlannerProjectTests(unittest.TestCase):
    """The public project remains independently usable in a source package."""

    def test_standalone_project_uses_core_without_diagnostics(self):
        text = (HERE / "main.xe").read_text(encoding="utf-8")
        self.assertEqual(check_source(text, str(HERE / "main.xe")), [])
        warnings = []
        self.assertIn("int main(", lower_to_c(text, str(HERE / "main.xe"), warnings=warnings))
        self.assertEqual(warnings, [])


class PrivateDependencyPlannerProtocolTests(unittest.TestCase):
    """Local reference fixtures never become a public package dependency."""

    @classmethod
    def setUpClass(cls):
        cls.verifier = private_example("dependency_planner/verify.py")
        private_example("dependency_planner/reference.rs")
        private_example("dependency_planner/reference.cpp")
        namespace = runpy.run_path(str(cls.verifier))
        cls.check_document = staticmethod(namespace["check_document"])
        cls.require_clean_check = staticmethod(namespace["require_clean_check"])

    def command(self, *arguments):
        result = subprocess.run([sys.executable, str(self.verifier), *arguments],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_hand_calculated_oracle_fixtures(self):
        self.assertIn("9 hand-calculated fixtures passed", self.command("--self-check"))

    def test_warning_only_check_json_uses_stderr_and_is_rejected_intentionally(self):
        # Static check only. The unknown external pointer slot is never read at
        # runtime, and this test neither builds nor executes the warning source.
        from compiler.toolchain import main

        with tempfile.TemporaryDirectory(prefix="xe-planner-check-protocol-") as directory:
            source, report = Path(directory) / "main.xe", Path(directory) / "check.json"
            source.write_text("fn load(slot:i32@@)->i32@{slot#}fn main()->Unit{unit}", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = main(["check", str(source), "--message-format=json"])
            self.assertEqual((status, out.getvalue()), (0, ""))
            warnings = json.loads(err.getvalue())["diagnostics"]
            self.assertTrue(warnings)
            self.assertTrue(all(warning["severity"] == "warning" for warning in warnings))
            self.assertIn("XE-PTR-0003", [warning["code"] for warning in warnings])
            result = subprocess.CompletedProcess([], status, out.getvalue(), err.getvalue())
            with self.assertRaisesRegex(RuntimeError, "unexpected Xe diagnostics"):
                self.require_clean_check(result, report)
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["diagnostics"], warnings)

    def test_check_json_reader_accepts_clean_stdout_but_not_mixed_or_malformed_output(self):
        clean = subprocess.CompletedProcess([], 0, '{"diagnostics": []}\n', "")
        self.assertEqual(self.check_document(clean), {"diagnostics": []})
        for stdout, stderr in ((clean.stdout, "tool chatter"), ("", ""), ("invalid", ""),
                               ('{"diagnostics": "lost warning list"}', "")):
            with self.subTest(stdout=stdout, stderr=stderr), self.assertRaises(RuntimeError):
                self.check_document(subprocess.CompletedProcess([], 0, stdout, stderr))

    @unittest.skipUnless(REFERENCES, "three-language differential needs C, C++ and Rust compilers")
    def test_three_languages_match_independent_oracle(self):
        assert CC is not None
        output = self.command("--cc", CC)
        for language in ("xe", "rust", "cpp"):
            self.assertIn(f"{language}: 73 cases match the specification", output)

    @unittest.skipUnless(REFERENCES and sys.platform.startswith("linux"), "sanitizers require Linux C/C++ and Rust")
    def test_safe_batch_keeps_default_leak_detection(self):
        assert CC is not None
        output = self.command("--cc", CC, "--sanitize")
        for language in ("xe", "rust", "cpp"):
            self.assertIn(f"{language}: 73 cases match the specification", output)


if __name__ == "__main__":
    unittest.main()
