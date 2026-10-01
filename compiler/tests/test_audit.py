"""审核分类本身的测试：未实现、非法源码、外部工具错误与执行结果不可混淆。"""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compiler.audit import audit_directory, audit_file, main, write_report
from compiler.xe_ast.source import Diagnostic, Source

CC = shutil.which("cc") or ""


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def source(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def warning_source(self):
        # unused 存在可检测的悬垂返回，但 main 没有调用它。显式运行验收
        # 不会真的解引用悬垂指针，也不把可预测的 UB 纳入自动执行。
        return self.source("warning.xe", '''fn unused() -> i32@ {
            let local = 1; local@
        }
        fn main() { println("safe entry"); }''')

    def test_syntax_and_semantic_errors_stop_at_distinct_layers(self):
        self.source("syntax.xe", "fn main() {")
        self.source("semantic.xe", 'fn main(){ let value:i32 = "not an integer"; }')
        report = audit_directory(self.root)
        self.assertEqual(report["summary"]["outcomes"], {"invalid": 2})
        rows = {Path(row["file"]).name: row for row in report["results"]}
        self.assertEqual(rows["syntax.xe"]["stages"]["ast"]["status"], "rejected")
        self.assertEqual(rows["syntax.xe"]["stages"]["semantic"]["status"], "skipped")
        self.assertEqual(rows["semantic.xe"]["stages"]["ast"]["status"], "passed")
        self.assertEqual(rows["semantic.xe"]["stages"]["semantic"]["status"], "rejected")
        self.assertEqual(rows["semantic.xe"]["stages"]["c"]["status"], "skipped")

    def test_backend_capability_is_not_invalid_source(self):
        path = self.source("valid.xe", "fn main(){}")
        # 注入能力诊断，不绑定到某个暂未实现的特性：那个特性未来可能真正实现。
        diagnostic = Diagnostic(Source(path.read_text(), str(path)), 0, 2,
                                "测试用后端能力边界", "XE-BACKEND-0001")
        with patch("compiler.audit.lower_to_c", side_effect=diagnostic):
            row = audit_file(path)
        self.assertEqual(row["outcome"], "unsupported")
        self.assertEqual(row["stages"]["semantic"]["status"], "passed")
        self.assertEqual(row["stages"]["c"]["status"], "unsupported")
        self.assertEqual(row["stages"]["compile"]["status"], "skipped")
        self.assertEqual(row["stages"]["c"]["diagnostics"][0]["code"], "XE-BACKEND-0001")

    @unittest.skipUnless(CC, "需要 C 编译器")
    def test_reachable_modules_are_checked_and_run(self):
        entry = self.source("main.xe", 'use crate::helper::answer;fn main(){println("{}",answer());}')
        self.source("helper.xe", 'pub fn answer()->i32 {42}')
        row = audit_file(entry)
        self.assertEqual(row['outcome'],'passed',row)
        self.assertEqual(row['stages']['run']['stdout'],'42\n')

    def test_semantic_capability_and_compiler_crash_are_distinct(self):
        path = self.source("valid.xe", "fn main(){}")
        diagnostic = Diagnostic(Source(path.read_text(), str(path)), 0, 2,
                                "测试用语义能力边界", "XE-SEM-0001")
        with patch("compiler.audit.Checker.check", return_value=[diagnostic]):
            row = audit_file(path)
        self.assertEqual(row["outcome"], "unsupported")
        self.assertEqual(row["stages"]["semantic"]["status"], "unsupported")
        with patch("compiler.audit.lower_to_c", side_effect=RuntimeError("unexpected defect")):
            row = audit_file(path)
        self.assertEqual(row["outcome"], "internal_error")
        self.assertIn("unexpected defect", row["stages"]["c"]["diagnostics"][0]["message"])

    def test_source_protection_and_json_cli(self):
        source = self.source("syntax.xe", "fn main() {")
        original = source.read_bytes()
        report = audit_directory(self.root)
        with self.assertRaises(ValueError):
            write_report(report, source)
        self.assertEqual(source.read_bytes(), original)
        output = self.root / "report.json"
        with contextlib.redirect_stdout(io.StringIO()) as messages:
            self.assertEqual(main([str(self.root), "-o", str(output)]), 0)
            self.assertEqual(main([str(self.root), "-o", str(output), "--strict"]), 1)
        self.assertEqual(json.loads(output.read_text())["summary"]["files"], 1)
        self.assertIn("XE-PARSE-0001", messages.getvalue())
        self.assertEqual(source.read_bytes(), original)

    def test_invalid_directory_and_timeout_settings(self):
        with self.assertRaises(ValueError):
            audit_directory(self.root / "missing")
        with self.assertRaises(ValueError):
            audit_directory(self.root, run_timeout=0)
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                audit_directory(self.root, compile_timeout=value)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.root / "missing")]), 2)

    def test_missing_c_compiler_is_tool_failure_after_c_generation(self):
        path = self.source("valid.xe", "fn main(){}")
        row = audit_file(path, cc=str(self.root / "missing-cc"))
        self.assertEqual(row["outcome"], "tool_error")
        self.assertEqual(row["stages"]["c"]["status"], "passed")
        self.assertEqual(row["stages"]["compile"]["diagnostics"][0]["code"], "XE-AUDIT-TOOL")
        self.assertEqual(row["stages"]["run"]["status"], "skipped")

    def test_warning_does_not_hide_compile_tool_failure(self):
        row = audit_file(self.warning_source(), cc=str(self.root / "missing-cc"))
        self.assertEqual(row["outcome"], "tool_error")
        self.assertEqual(row["stages"]["semantic"]["status"], "passed")
        self.assertTrue(row["stages"]["semantic"]["warnings"])
        self.assertEqual(row["stages"]["c"]["status"], "passed")
        self.assertEqual(row["stages"]["compile"]["diagnostics"][0]["code"], "XE-AUDIT-TOOL")

    @unittest.skipUnless(CC, "警告程序编译验收需要 C 编译器")
    def test_pointer_warning_program_compiles_but_is_not_run_by_default(self):
        source = self.warning_source()
        row = audit_file(source, cc=CC)
        self.assertEqual(row["outcome"], "warning_not_run")
        for phase in ("ast", "semantic", "c", "compile"):
            self.assertEqual(row["stages"][phase]["status"], "passed")
        warning = row["stages"]["semantic"]["warnings"][0]
        self.assertEqual(warning["severity"], "warning")
        self.assertEqual(warning["code"], "XE-PTR-0001")
        self.assertIn("unsafe", warning["inferred_type"])
        self.assertEqual(row["stages"]["run"],
                         {"status": "skipped", "reason": "pointer_risk_warning"})
        report = audit_directory(self.root, cc=CC)
        self.assertEqual(report["configuration"]["pointer_risk_warnings"], True)
        self.assertEqual(report["configuration"]["execute_warning_programs"], False)
        self.assertNotIn("safety_checks", report["configuration"])
        self.assertEqual(report["summary"]["outcomes"], {"warning_not_run": 1})

    @unittest.skipUnless(CC, "显式允许运行警告程序需要 C 编译器")
    def test_run_warnings_keyword_explicitly_enables_execution(self):
        source = self.warning_source()
        row = audit_file(source, cc=CC, run_warnings=True)
        self.assertEqual(row["outcome"], "passed")
        self.assertEqual(row["stages"]["run"]["stdout"], "safe entry\n")
        self.assertTrue(row["stages"]["semantic"]["warnings"])
        report = audit_directory(self.root, cc=CC, run_warnings=True)
        self.assertEqual(report["configuration"]["execute_warning_programs"], True)
        self.assertEqual(report["summary"]["outcomes"], {"passed": 1})

    @unittest.skipUnless(CC, "警告执行 CLI 验收需要 C 编译器")
    def test_run_warnings_cli_is_explicit_and_warning_is_visible(self):
        self.warning_source()
        report_path = self.root / "report.json"
        args = [str(self.root), "-o", str(report_path), "--cc", CC, "--strict"]
        with contextlib.redirect_stdout(io.StringIO()) as messages:
            self.assertEqual(main(args), 1)
        self.assertIn("warning XE-PTR-0001", messages.getvalue())
        report = json.loads(report_path.read_text())
        self.assertEqual(report["results"][0]["outcome"], "warning_not_run")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([*args, "--run-warnings"]), 0)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["configuration"]["execute_warning_programs"], True)
        self.assertEqual(report["results"][0]["stages"]["run"]["stdout"], "safe entry\n")

    @unittest.skipUnless(CC, "实际编译运行审核需要 C 编译器")
    def test_real_execution_stderr_nonzero_and_source_preservation(self):
        source = self.source("ok.xe", 'fn main(){println("hello");eprintln("note");}')
        original = source.read_bytes()
        row = audit_file(source, cc=CC)
        self.assertEqual(row["outcome"], "passed")
        self.assertTrue(all(stage["status"] == "passed" for stage in row["stages"].values()))
        self.assertEqual(row["stages"]["run"]["stdout"], "hello\n")
        self.assertEqual(row["stages"]["run"]["stderr"], "note\n")
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["ok.xe"])
        source = self.source("nonzero.xe", "fn main()->i32 { 7 }")
        row = audit_file(source, cc=CC)
        self.assertEqual(row["outcome"], "execution_nonzero")
        self.assertEqual(row["stages"]["run"]["exit_code"], 7)
        self.assertNotEqual(row["outcome"], "invalid")

    @unittest.skipUnless(CC, "实际运行 timeout 审核需要 C 编译器")
    def test_run_timeout_stops_infinite_program(self):
        source = self.source("loop.xe", "fn main(){while true {}}")
        row = audit_file(source, cc=CC, run_timeout=0.05)
        self.assertEqual(row["outcome"], "timeout")
        self.assertEqual(row["stages"]["compile"]["status"], "passed")
        self.assertEqual(row["stages"]["run"]["diagnostics"][0]["code"], "XE-AUDIT-TIMEOUT")


if __name__ == "__main__":
    unittest.main()
