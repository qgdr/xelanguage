"""Default-core evidence, independent of the explicit toy-language fixtures."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compiler.driver import build
from compiler.project import project_at
from compiler.toolchain import main
from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import (
    AnnotatedChecker,
    CBackend,
    lower_program_to_c,
    lower_to_c,
)
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.cli import main as script_main
from compiler.xe_ast.language_policy import current_profile, language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Diagnostic, Source

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which(os.environ.get("CC", "gcc"))
CORE = '''fn identity(value: i32) -> i32 { value }
fn main() -> Unit { let answer: i32 = identity(42); println("{}", answer); }'''
OLD = 'fn main() { println("legacy"); }'


class CorePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xe-core-policy-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "main.xe"
        self.source.write_text(CORE, encoding="utf-8")
        self.assertEqual(current_profile(), "core")

    def cli(self, *arguments, script=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = (script_main if script else main)(list(map(str, arguments)))
        self.assertEqual(current_profile(), "core")
        return status, stdout.getvalue(), stderr.getvalue()

    def test_default_parse_check_and_ast_document_accept_only_audited_subset(self):
        source = Source(CORE)
        tree = parse_source(CORE)
        self.assertEqual(Checker(source, tree).check(), [])
        self.assertEqual(document(source, tree)["schema_version"], 1)
        self.assertIn("int32_t", lower_to_c(CORE))
        for command in ("ast", "check", "emit-c"):
            args = [command, self.source]
            if command != "check":
                args += ["-o", "-"]
            status, _, error = self.cli(*args)
            self.assertEqual(status, 0, error)

    def test_documented_core_example_is_checked_and_lowered_without_legacy(self):
        text = (ROOT / "examples/core/main.xe").read_text(encoding="utf-8")
        tree = parse_source(text)
        self.assertEqual(Checker(Source(text), tree).check(), [])
        self.assertIn("int32_t", lower_to_c(text))

    def test_old_forms_are_rejected_by_each_default_cli_action_before_tool_invocation(self):
        self.source.write_text(OLD, encoding="utf-8")
        for action in ("ast", "check", "emit-c", "build", "run"):
            with self.subTest(action=action), patch("compiler.driver.cc_identity") as identity:
                status, _, error = self.cli(action, self.source, "--message-format=json")
                self.assertEqual(status, 1, error)
                diagnostic = json.loads(error)["diagnostics"][0]
                self.assertEqual(diagnostic["code"], "XE-CORE-0001")
                self.assertEqual(diagnostic["severity"], "error")
                self.assertIn("span", diagnostic)
                identity.assert_not_called()
        status, _, error = self.cli(self.source, "--check", script=True)
        self.assertEqual(status, 1)
        self.assertIn("XE-CORE-0001", error)

    def test_legacy_flag_is_explicit_and_can_be_before_or_after_subcommand(self):
        self.source.write_text(OLD, encoding="utf-8")
        for args in (("--language-profile=legacy", "check", self.source),
                     ("check", self.source, "--language-profile=legacy")):
            status, _, error = self.cli(*args)
            self.assertEqual(status, 0, error)
        status, _, error = self.cli(self.source, "--check", "--language-profile=legacy", script=True)
        self.assertEqual(status, 0, error)
        self.assertEqual(self.cli("check", self.source)[0], 1)

    def test_legacy_ast_cannot_cross_default_checker_document_or_backend_boundary(self):
        source = Source(OLD)
        with language_profile("legacy"):
            tree = parse_source(OLD)
            annotated = AnnotatedChecker(source, tree)
            self.assertEqual(annotated.check(), [])
        errors = Checker(source, tree).check()
        self.assertEqual([error.code for error in errors], ["XE-CORE-0001"])
        for action in (lambda: document(source, tree), lambda: lower_program_to_c(source, tree),
                       lambda: CBackend(annotated).generate()):
            with self.subTest(action=action), self.assertRaises(Diagnostic) as caught:
                action()
            self.assertEqual(caught.exception.code, "XE-CORE-0001")

    def test_unknown_ast_fields_cannot_request_an_exemption(self):
        tree = parse_source(CORE)
        tree["_approved"] = True
        errors = Checker(Source(CORE), tree).check()
        self.assertEqual([error.code for error in errors], ["XE-CORE-0001"])

    def test_failed_admission_preserves_previous_ast_and_does_not_publish_c(self):
        output = self.root / "main.json"
        self.assertEqual(self.cli("ast", self.source, "-o", output)[0], 0)
        previous = output.read_bytes()
        self.source.write_text(OLD, encoding="utf-8")
        self.assertEqual(self.cli("ast", self.source, "-o", output)[0], 1)
        self.assertEqual(output.read_bytes(), previous)
        c_output = self.root / "main.c"
        self.assertEqual(self.cli("emit-c", self.source, "-o", c_output)[0], 1)
        self.assertFalse(c_output.exists())

    def test_new_process_defaults_to_core_not_the_parent_legacy_context(self):
        self.source.write_text(OLD, encoding="utf-8")
        with language_profile("legacy"):
            result = subprocess.run([sys.executable, "-m", "compiler", "check", str(self.source)],
                                    cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("XE-CORE-0001", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(CC, "需要系统 C 编译器")
    def test_default_core_reaches_c_compilation_and_actual_execution(self):
        assert CC is not None
        program = self.root / "program"
        build_executable(self.source, program, cc=CC)
        result = subprocess.run([str(program)], capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "42\n", ""))

    @unittest.skipUnless(CC and sys.platform.startswith("linux"), "ASan/UBSan 验收需要 Linux C 编译器")
    def test_default_core_pointer_program_under_sanitizers(self):
        assert CC is not None
        self.source.write_text('''fn read(pointer: i32@) -> i32 { pointer# }
fn main() -> Unit {
    let[mut] value: i32 = 41;
    let pointer: i32@[mut] = value@[mut];
    pointer# = 42;
    let readonly: i32@ = pointer;
    println("{}", read(readonly));
}''', encoding="utf-8")
        program = self.root / "sanitized"
        build_executable(self.source, program, cc=CC,
                         extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
        result = subprocess.run([str(program)], capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "42\n", ""))

    @unittest.skipUnless(CC, "需要系统 C 编译器")
    def test_legacy_cache_never_skips_core_admission_or_replaces_old_binary(self):
        assert CC is not None
        self.source.write_text(OLD, encoding="utf-8")
        output = self.root / "program"
        project = project_at(self.source)
        with language_profile("legacy"):
            build(project, self.source, output, cc=CC)
        previous = output.read_bytes()
        with patch("compiler.driver.cc_identity") as identity, self.assertRaises(Diagnostic):
            build(project, self.source, output, cc=CC)
        identity.assert_not_called()
        self.assertEqual(output.read_bytes(), previous)

    @unittest.skipUnless(CC, "需要系统 C 编译器")
    def test_cache_identity_contains_language_profile(self):
        assert CC is not None
        output = self.root / "program"
        project = project_at(self.source)
        with language_profile("legacy"):
            first = build(project, self.source, output, cc=CC)
        second = build(project, self.source, output, cc=CC)
        self.assertFalse(first.reused)
        self.assertFalse(second.reused)
        self.assertEqual(json.loads(second.receipt.read_text())["build"]["language_profile"], "core")
        self.assertTrue(build(project, self.source, output, cc=CC).reused)


if __name__ == "__main__":
    unittest.main()
