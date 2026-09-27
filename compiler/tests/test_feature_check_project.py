"""真实 Xe 交互工具的功能验收、EOF 与资源清理测试。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples/feature_check/main.xe"
CC = shutil.which("cc")
FEATURES = (
    "numeric", "comparison-chain", "short-circuit", "evaluation-order",
    "struct-copy-methods", "resource-drop", "generics", "tuple-alias",
    "enum-owning", "enum-pointer", "maybe-conversion", "array-slice-range",
    "function-pipeline", "string-utf8", "pointer-permissions", "resource-array",
)


class FeatureCheckSemanticTests(unittest.TestCase):
    def test_project_is_valid_and_uses_live_pointers(self):
        text = SOURCE.read_text(encoding="utf-8")
        checker = Checker(Source(text, str(SOURCE)), parse_source(text, str(SOURCE)))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        self.assertEqual(checker.warnings, [], "\n".join(warning.render() for warning in checker.warnings))


@unittest.skipUnless(CC, "运行项目需要系统 C 编译器")
class FeatureCheckExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="xe-feature-check-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.program = Path(cls.directory.name) / "feature_check"
        build_executable(SOURCE, cls.program, cc=CC,
                         extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))

    def run_commands(self, text, status=0):
        result = subprocess.run([str(self.program)], input=text, capture_output=True,
                                text=True, cwd=ROOT, timeout=10)
        self.assertEqual(result.returncode, status, result.stderr)
        if status == 0:
            self.assertEqual(result.stderr, "")
        self.assertNotIn("AddressSanitizer", result.stderr)
        self.assertNotIn("runtime error:", result.stderr)
        return result

    def test_check_compares_all_feature_results(self):
        result = self.run_commands("check\nquit\n")
        lines = result.stdout.splitlines()
        self.assertEqual([line for line in lines if line.startswith("PASS ")],
                         ["PASS " + feature for feature in FEATURES])
        self.assertNotIn("FAIL ", result.stdout)
        self.assertEqual(lines[-1], "summary: 16 passed, 0 failed")

    def test_help_echo_unicode_and_quit(self):
        result = self.run_commands("help\necho\n你好，Xe! 🌍\nquit\ncheck\n")
        self.assertEqual(result.stdout.count("commands: help | check | echo | quit"), 2)
        self.assertEqual(result.stdout.splitlines()[-1], "你好，Xe! 🌍")
        self.assertNotIn("PASS ", result.stdout)

    def test_empty_stdin_and_blank_commands_exit_normally(self):
        for text in ("", "\n\n", "quit", "echo\n"):
            with self.subTest(text=text):
                result = self.run_commands(text)
                self.assertNotIn("PASS ", result.stdout)

    def test_empty_echo_line_is_a_real_line(self):
        result = self.run_commands("echo\n\nquit\n")
        self.assertTrue(result.stdout.endswith("normally.\n\n"), result.stdout)

    def test_unknown_command_reports_stderr_and_nonzero_status(self):
        result = self.run_commands("未知命令\n", status=2)
        self.assertEqual(result.stderr, "unknown command: 未知命令\n")
        self.assertNotIn("PASS ", result.stdout)

    def test_checks_can_repeat_without_leaks_or_double_drops(self):
        result = self.run_commands("check\n" * 5)
        self.assertEqual(result.stdout.count("summary: 16 passed, 0 failed"), 5)
        self.assertEqual(sum(line.startswith("PASS ") for line in result.stdout.splitlines()), 80)

    def test_long_unicode_echo_and_unterminated_final_line(self):
        payload = "你🌍好" * 4096
        result = self.run_commands("echo\n" + payload)
        self.assertEqual(result.stdout.splitlines()[-1], payload)

    def test_crlf_commands_and_echo(self):
        result = self.run_commands("echo\r\nWindows 文本\r\ncheck\r\nquit\r\n")
        self.assertIn("\nWindows 文本\n", result.stdout)
        self.assertTrue(result.stdout.endswith("summary: 16 passed, 0 failed\n"))


if __name__ == "__main__":
    unittest.main()
