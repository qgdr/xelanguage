"""通过真正 C 编译器和可执行程序验证后端，不仅断言生成文本。"""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable, emit_c, BuildError
from compiler.xe_ast.cli import main
from compiler.xe_ast.source import Diagnostic

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which("cc")


@unittest.skipUnless(CC, "运行后端验收需要系统 C 编译器")
class BackendExecutionTests(unittest.TestCase):
    def run_source(self, text, checked=True, flags=()):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "test.xe"
            output = Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, checked, CC, flags)
            return subprocess.run([str(output)], capture_output=True, text=True, timeout=5)

    def assert_output(self, text, expected, checked=True):
        result = self.run_source(text, checked)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")

    def test_existing_structs_methods_strings_and_drop(self):
        examples = {
            "struct_create": "hello (10, 20)\n",
            "struct_methods": "12 12\n5\nhello\n",
            "struct_move": "1:hello\n",
            "block_drop": "drop 2\ndrop 1\nblock finished\ndrop 3\ndrop 0\n",
            "trait_copy_drop": "1 2\ndrop 1\n",
            "ownership": "yours hello\n",
            "String": "before = hello\nhello, world!\n",
            "comparison_chain": "true false true true\n",
            "pointer_unchecked": "42\n",
        }
        for name, expected in examples.items():
            with self.subTest(name=name):
                text = (ROOT / "tests/stage999" / (name + ".xe")).read_text()
                self.assert_output(text, expected, name != "pointer_unchecked")

    def test_return_break_continue_and_conditional_moves(self):
        text = (ROOT / "tests/backend/struct_control.xe").read_text()
        self.assert_output(text, "drop 2\ndrop 1\nreturned 7\ndrop 10\ndrop 11\ntake 20\ndrop 20\nfinished\n")

    def test_partial_move_drop_once(self):
        text = (ROOT / "tests/backend/struct_partial_move.xe").read_text()
        self.assert_output(text, "remaining\ndrop 1\ndrop 2\n")

    def test_argument_order_and_comparison_short_circuit(self):
        text = (ROOT / "tests/backend/evaluation_order.xe").read_text()
        self.assert_output(text, "1 2\n2 3\nobserved 1\nobserved 2\nchain false\nshort false\n")

    def test_field_initialization_order(self):
        self.assert_output('''struct Point { a: i32, b: i32, }
        impl Copy for Point;
        fn mark(x: i32) -> i32 { println("mark {}", x); x }
        fn main() {
            let p = Point { .b = mark(2); .a = mark(1); };
            println("{} {}", p.a, p.b);
        }''', "mark 2\nmark 1\n1 2\n")

    def test_resource_reassignment_and_field_replacement(self):
        self.assert_output('''struct S { text: String, }
        fn main() {
            let[mut] s << S { .text << String::from("first"); };
            s.text << String::from("second");
            println("{}", s.text@);
            let[mut] text << String::from("hello");
            text << text.clone();
            println("{}", text);
        }''', "second\nhello\n")

    def test_nested_drop_then_fields_in_declaration_order(self):
        self.assert_output('''struct Inner { n: i32, }
        impl Drop for Inner { fn drop(self: Self@[mut]) { println("inner {}", self.n); } }
        struct Outer { first: Inner, second: Inner, }
        impl Drop for Outer { fn drop(self: Self@[mut]) { println("outer"); } }
        fn main() { let x << Outer {
            .second << Inner { .n = 2; }; .first << Inner { .n = 1; };
        }; }''', "outer\ninner 1\ninner 2\n")

    def test_while_rechecks_condition_and_cleans_continue(self):
        self.assert_output('''struct Trace { n: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        fn main() {
            let[mut] n = 0;
            while n < 2 {
                let trace << Trace { .n = n; };
                n = n + 1;
                continue;
            }
            println("{}", n);
        }''', "drop 0\ndrop 1\n2\n")

    def test_shadowing_and_struct_forward_declarations(self):
        self.assert_output('''struct Outer { inner: Inner, } struct Inner { n: i32, }
        impl Copy for Outer;
        impl Copy for Inner;
        fn main() {
            let x = 1;
            { let x = 2; println("{}", x); };
            let value = Outer { .inner = Inner { .n = 3; }; };
            println("{} {}", x, value.inner.n);
        }''', "2\n1 3\n")

    def test_if_values_early_returns_and_entry_exit_code(self):
        self.assert_output('''fn choose(c: bool) -> String {
            if c { return String::from("yes"); } else { return String::from("no"); }
        }
        fn main() {
            let value << choose(true);
            let n = if false { 1 } else { 2 };
            println("{} {}", value@, n);
        }''', "yes 2\n")
        self.assertEqual(self.run_source("fn main() -> i32 { 7 }").returncode, 7)

    def test_unicode_nul_format_and_stderr(self):
        result = self.run_source('''fn main() {
            let text << String::from("猫\\0a");
            println("{} {} {{}}", text@, '猫');
            eprintln("error {}", 42);
        }''')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "猫\0a 猫 {}\n")
        self.assertEqual(result.stderr, "error 42\n")

    def test_signed_minimum_unsigned_and_large_comparison(self):
        self.assert_output('''fn main() {
            let minimum: i64 = -9223372036854775808;
            let maximum: u64 = 18446744073709551615;
            let large: usize = 5000000000;
            println("{} {} {}", minimum, maximum, 1 < large < 6000000000);
        }''', "-9223372036854775808 18446744073709551615 true\n")

    def test_integer_overflow_and_zero_division_report_runtime_failure(self):
        for expression, message in [("2147483647 + 1", "integer overflow"), ("1 / 0", "division by zero")]:
            with self.subTest(expression=expression):
                result = self.run_source(f'fn main() {{ let value: i32 = {expression}; println("{{}}", value); }}')
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_owned_method_parameter_drops(self):
        self.assert_output('''struct S { n: i32, }
        impl Drop for S { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        impl S { fn consume(self: Self) { println("consume {}", self.n); } }
        fn main() { let s << S { .n = 8; }; s.consume(); }''', "consume 8\ndrop 8\n")

    def test_sanitized_resource_programs(self):
        for name in ("struct_methods", "ownership", "block_drop"):
            text = (ROOT / "tests/stage999" / (name + ".xe")).read_text()
            result = self.run_source(text, flags=("-fsanitize=undefined", "-fno-sanitize-recover=all"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")

    @unittest.skipUnless(__import__("sys").platform.startswith("linux"), "ASan 的 no-pie 验收当前针对 Linux")
    def test_address_sanitizer_and_leak_checks(self):
        # no-pie 避免部分 Linux/WSL 环境中 ASan 地址空间布局的随机冲突。
        for relative in ("stage999/struct_methods.xe", "backend/struct_partial_move.xe"):
            text = (ROOT / "tests" / relative).read_text()
            result = self.run_source(text, flags=("-fsanitize=address,undefined",
                                                  "-fno-sanitize-recover=all", "-no-pie"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")

    def test_arithmetic_and_assignment_snapshot_order(self):
        self.assert_output('''fn change(x: i32@[mut]) -> i32 { x# = 2; 10 }
        fn main() { let[mut] x = 1; let y = x + change(x@[mut]); println("{} {}", x, y); }''',
                           "2 11\n")
        self.assert_output('''fn main() {
            let[mut] x = 1; let[mut] y = 2; let[mut] p = x@[mut];
            p# = { p = y@[mut]; 9 };
            println("{} {}", x, y);
        }''', "9 2\n")

    def test_resource_temporaries_drop_at_statement_boundary(self):
        self.assert_output('''struct Trace { n: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        fn main() { Trace { .n = 1; }; println("after"); }''', "drop 1\nafter\n")

    def test_drop_order_tracks_deferred_initialization(self):
        self.assert_output('''struct Trace { n: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        fn main() {
            let first: Trace;
            let second << Trace { .n = 2; };
            first << Trace { .n = 1; };
        }''', "drop 1\ndrop 2\n")

    def test_recursion_and_mutual_function_forward_declarations(self):
        self.assert_output('''fn a(x: i32) -> i32 { if x == 0 { 0 } else { b(x - 1) + 1 } }
        fn b(x: i32) -> i32 { a(x) }
        fn main() { println("{}", a(3)); }''', "3\n")


class BackendFailureTests(unittest.TestCase):
    def test_nested_custom_drop_field_cannot_be_moved_out(self):
        text = '''struct Inner { text: String, }
        impl Drop for Inner { fn drop(self: Self@[mut]) { println("{}", self.text@); } }
        struct Outer { inner: Inner, }
        fn main() {
            let outer << Outer { .inner << Inner { .text << String::from("x"); }; };
            let text << outer.inner.text;
        }'''
        with self.assertRaises(Diagnostic) as caught:
            lower_to_c(text, check_borrows=True)
        self.assertEqual(caught.exception.code, "XE-OWN-0002")

    def test_tool_failure_preserves_executable_and_reports_stderr(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.xe", Path(directory) / "program"
            source.write_text("fn main() {}")
            output.write_text("previous executable")
            failure = subprocess.CompletedProcess([], 1, "", "compiler failure details")
            with patch("compiler.xe_ast.build.subprocess.run", return_value=failure):
                with self.assertRaises(BuildError) as caught:
                    build_executable(source, output)
            self.assertIn("compiler failure details", str(caught.exception))
            self.assertEqual(output.read_text(), "previous executable")

    def test_semantic_failures_stop_before_codegen(self):
        text = (ROOT / "tests/fails/borrow_move_resource.xe").read_text()
        with self.assertRaises(Diagnostic) as caught:
            lower_to_c(text)
        self.assertEqual(caught.exception.code, "XE-MOVE-0002")

    def test_unimplemented_features_report_language_diagnostic(self):
        # 捕获环境已实现；Vec 的运行布局仍是明确未实现能力。
        for source in ["fn main() { let values: Vec[i32]; }"]:
            with self.subTest(source=source):
                with self.assertRaises(Diagnostic) as caught:
                    lower_to_c(source)
                self.assertEqual(caught.exception.code, "XE-BACKEND-0001")

    def test_unused_generic_template_does_not_force_code_generation(self):
        # 泛型以具体类型使用时才生成 C，不能为未实例化的 T 猜测布局。
        generated = lower_to_c("fn[T] same(x: T) -> T { x } fn main() {}")
        self.assertIn("int main(int argc, char **argv)", generated)

    def test_recursive_value_layout_fails_cleanly(self):
        with self.assertRaises(Diagnostic) as caught:
            lower_to_c("struct Recursive { value: Recursive, } fn main() {}")
        self.assertEqual(caught.exception.code, "XE-TYPE-0006")
        self.assertIn("按值递归", caught.exception.message)

    def test_invalid_program_preserves_existing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.xe", Path(directory) / "program"
            source.write_text("fn main() { missing; }")
            output.write_text("previous executable")
            c_path = output.with_name(output.name + ".c")
            c_path.write_text("previous C")
            with self.assertRaises(Diagnostic):
                build_executable(source, output)
            self.assertEqual(output.read_text(), "previous executable")
            self.assertEqual(c_path.read_text(), "previous C")

    def test_missing_compiler_preserves_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.xe", Path(directory) / "program"
            source.write_text("fn main() {}")
            output.write_text("previous executable")
            with self.assertRaises(BuildError):
                build_executable(source, output, cc=str(Path(directory) / "missing-cc"))
            self.assertEqual(output.read_text(), "previous executable")
            self.assertTrue(output.with_name(output.name + ".c").exists())
            self.assertFalse(any(p.name.startswith(".xe-build-") for p in Path(directory).iterdir()))

    def test_outputs_cannot_overwrite_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.xe"
            source.write_text("fn main() {}")
            with self.assertRaises(BuildError):
                emit_c(source, source)
            with self.assertRaises(BuildError):
                build_executable(source, source)
            self.assertEqual(source.read_text(), "fn main() {}")

    def test_cli_emit_c_and_json_backend_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.xe"
            source.write_text("fn main() {}")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = main([str(source), "--emit-c", "-o", "-"])
            self.assertEqual(status, 0, err.getvalue())
            self.assertIn("int main(int argc, char **argv)", out.getvalue())
            source.write_text("fn main() { let values: Vec[i32]; }")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status = main([str(source), "--emit-c", "-o", "-", "--diagnostic-format", "json"])
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(err.getvalue())["diagnostics"][0]["code"], "XE-BACKEND-0001")
