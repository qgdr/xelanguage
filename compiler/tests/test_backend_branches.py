"""实际运行管道和枚举匹配，包含资源、指针及提前退出边界。"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.source import Diagnostic

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which("cc")


@unittest.skipUnless(CC, "运行后端验收需要系统 C 编译器")
class BranchExecutionTests(unittest.TestCase):
    def run_source(self, text, checked=True, sanitize=False):
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "test.xe"
            output = Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, checked, CC, flags)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_existing_pipeline_and_enum_examples(self):
        for name, expected in (("pipe", "10\n"), ("enum", "identifier\n")):
            for checked in (False, True):
                with self.subTest(name=name, checked=checked):
                    text = (ROOT / "tests/stage999" / (name + ".xe")).read_text()
                    self.assertEqual(self.run_source(text, checked), expected)

    def test_owned_pipeline_and_borrowed_enum(self):
        text = (ROOT / "tests/backend/enum_pipeline.xe").read_text()
        self.assertEqual(self.run_source(text), "length 5\nsize 5\n")

    def test_pipeline_discards_resources_and_preserves_returns(self):
        self.assertEqual(self.run_source('''struct Trace { n: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        fn keep(value: Trace) -> Trace { value }
        fn main() {
            let t << Trace { .n = 1; };
            let kept << t |> keep |> x: Trace -> x;
            let n = Trace { .n = 2; } |> _ -> 10;
            println("n {}", n);
        }'''), "drop 2\nn 10\ndrop 1\n")

    def test_pipeline_integer_context_and_sink_assignment(self):
        self.assertEqual(self.run_source('''fn identity(n: u64) -> u64 { n }
        fn main() {
            let a = 18446744073709551615 |> (identity);
            let b = 18446744073709551615 |> n: u64 -> n;
            let wide: i64 = 4000000000 |> n -> n;
            let output: i32;
            (3 |> n -> n + 1) >> output;
            println("{} {} {} {}", a, b, wide, output);
        }'''), "18446744073709551615 18446744073709551615 4000000000 4\n")

    def test_multiple_payloads_and_zero_argument_handler(self):
        self.assertEqual(self.run_source('''enum Pair { Empty, Values[i32, i32], }
        fn sum(a: i32, b: i32) -> i32 { a + b }
        fn zero() -> i32 { 0 }
        fn total(p: Pair) -> i32 {
            p ? { Pair::Values :> sum, Pair::Empty :> zero, }
        }
        fn main() {
            println("{} {}", total(Pair::Values[2, 3]), total(Pair::Empty));
            let p << Pair::Values[4, 5];
            println("{}", p ? { Pair::Values :> [a, b] -> a * b, Pair::Empty :> _ -> 0, });
        }'''), "5 0\n20\n")

    def test_unselected_arms_have_no_effect(self):
        self.assertEqual(self.run_source('''enum E { A[i32], B, }
        fn main() {
            let x << E::A[8];
            let n = x ? {
                E::A :> value -> { println("selected"); value },
                E::B :> _ -> { println("unselected"); 0 },
            };
            println("{}", n);
        }'''), "selected\n8\n")

    def test_literal_and_wildcard_matching(self):
        self.assertEqual(self.run_source('''enum E { A[i32], B, }
        fn main() {
            let x << E::A[4];
            println("{}", x ? { E::B :> _ -> 0, _ :> whole: E -> 7, });
            let b = true;
            println("{}", b ? { true :> _ -> 1, false :> _ -> 2, });
            let text: str = "hello";
            println("{}", text ? { "hello" :> _ -> 3, _ :> _ -> 4, });
        }'''), "7\n1\n3\n")

    def test_borrowed_payload_pointer_remains_in_original_object(self):
        self.assertEqual(self.run_source('''enum E { Number[i64], End, }
        fn access(value: E@) -> i64@ {
            value ?[@] { E::Number :> number: i64@ -> number, E::End :> _ -> { panic("end"); }, }
        }
        fn main() {
            let value << E::Number[42];
            let pointer = access(value@);
            println("{}", pointer#);
        }'''), "42\n")

    def test_mutable_borrowed_matching(self):
        self.assertEqual(self.run_source('''enum E { Number[i32], End, }
        fn update(value: E@[mut]) {
            value ?[@[mut]] {
                E::Number :> number: i32@[mut] -> { number# = 9; },
                E::End :> _ -> {},
            };
        }
        fn main() {
            let[mut] value << E::Number[1];
            update(value@[mut]);
            println("{}", value ? { E::Number :> number -> number, E::End :> _ -> 0, });
        }'''), "9\n")

    def test_nested_resource_payload_and_ignored_bindings(self):
        self.assertEqual(self.run_source('''struct Trace { n: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        enum Inner { Value[Trace], Empty, }
        enum Outer { Pair[Inner, Trace], Empty, }
        fn main() {
            let first << Outer::Pair[Inner::Value[Trace { .n = 1; }], Trace { .n = 2; }];
            first ? { Outer::Pair :> _ -> {}, Outer::Empty :> _ -> {}, };
            let second << Outer::Pair[Inner::Value[Trace { .n = 3; }], Trace { .n = 4; }];
            println("finished");
        }'''), "drop 2\ndrop 1\nfinished\ndrop 3\ndrop 4\n")

    def test_return_and_continue_from_handler_cleanup(self):
        self.assertEqual(self.run_source('''struct Trace { n: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        enum E { Value[Trace], Empty, }
        fn early(value: E) -> i32 {
            value ? { E::Value :> t -> { return t.n; }, E::Empty :> _ -> 0, }
        }
        fn main() {
            println("return {}", early(E::Value[Trace { .n = 7; }]));
            for n in 0..2 {
                let value << E::Value[Trace { .n = n; }];
                value ? { E::Value :> t -> { continue; }, E::Empty :> _ -> {}, };
            }
            println("done");
        }'''), "drop 7\nreturn 7\ndrop 0\ndrop 1\ndone\n")

    def test_custom_enum_drop_precedes_payload_cleanup(self):
        self.assertEqual(self.run_source('''struct Trace { n: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        enum E { Value[Trace], Empty, }
        impl Drop for E { fn drop(self: Self@[mut]) { println("enum drop"); } }
        fn main() { let e << E::Value[Trace { .n = 3; }]; }'''), "enum drop\ndrop 3\n")

    def test_constructor_early_return_drops_preceding_payload(self):
        self.assertEqual(self.run_source('''struct Trace { n: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        enum E { Pair[Trace, Trace], }
        fn create() { E::Pair[Trace { .n = 1; }, { return; }]; }
        fn main() { create(); println("after"); }'''), "drop 1\nafter\n")

    def test_enum_copy_marker_and_wildcard_function_target(self):
        self.assertEqual(self.run_source('''enum E { Number[i32], End, }
        impl Copy for E {}
        fn handle(value: E) -> i32 { value ? { E::Number :> number -> number, E::End :> _ -> 0, } }
        fn main() {
            let value = E::Number[4];
            let copy = value;
            println("{} {}", handle(value), copy ? { _ :> handle, });
        }'''), "4 4\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie 验收针对 Linux")
    def test_resource_pipeline_and_enum_are_sanitizer_clean(self):
        for name, expected in (("enum_pipeline", "length 5\nsize 5\n"),
                               ("enum_resources", "left\nright\nfinished\n")):
            with self.subTest(name=name):
                text = (ROOT / "tests/backend" / (name + ".xe")).read_text()
                self.assertEqual(self.run_source(text, sanitize=True), expected)

    def test_nested_heap_payload_and_wildcard_transfer(self):
        text = (ROOT / "tests/backend/enum_resources.xe").read_text()
        self.assertEqual(self.run_source(text), "left\nright\nfinished\n")


class BranchDiagnosticTests(unittest.TestCase):
    def test_resource_payload_borrow_cannot_be_moved_into_function(self):
        text = '''enum E { Text[String], Empty, }
        fn consume(text: String) {}
        fn inspect(value: E@) {
            value ?[@] { E::Text :> text: String@ -> consume(text#), E::Empty :> _ -> {}, };
        }
        fn main() {}'''
        with self.assertRaises(Diagnostic) as caught:
            lower_to_c(text, check_borrows=True)
        self.assertEqual(caught.exception.code, "XE-MOVE-0002")

    def test_matching_non_exhaustive_or_moved_value_reports_semantic_error(self):
        examples = [
            ('''enum E { A, B, } fn main() { let e << E::A;
             e ? { E::A :> _ -> 0, }; }''', "XE-MATCH-0003"),
            ('''enum E { A, B, } fn main() { let e << E::A;
             e ? { E::A :> _ -> 0, E::B :> _ -> 0, };
             e ? { E::A :> _ -> 0, E::B :> _ -> 0, }; }''', "XE-MOVE-0001"),
        ]
        for source, code in examples:
            with self.subTest(code=code):
                with self.assertRaises(Diagnostic) as caught:
                    lower_to_c(source)
                self.assertEqual(caught.exception.code, code)
