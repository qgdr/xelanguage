"""Calls stop at the first expression that does not complete normally."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import check_source

TRACE = '''
struct Trace { number: i32, text: String, }
impl Drop for Trace {
    fn drop(self: Self@[mut]) { println("drop {}", self.number); }
}
fn trace(number: i32) -> Trace {
    Trace { .number = number; .text << String::from("owned"); }
}
'''


class CompletionCallTests(LegacyTestCase):
    def assert_checked(self, source):
        errors = check_source(source)
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))

    def assert_compiles(self, source):
        self.assert_checked(source)
        generated = lower_to_c(source)
        result = subprocess.run(
            ["gcc", "-x", "c", "-std=c11", "-Werror=incompatible-pointer-types",
             "-fsyntax-only", "-"],
            input=generated, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def assert_runs(self, source, expected):
        self.assert_checked(source)
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "main.xe"
            binary = Path(directory) / "program"
            entry.write_text(source, encoding="utf-8")
            build_executable(entry, binary, cc="gcc")
            result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_noncompleting_arguments_make_direct_and_indirect_calls_never(self):
        for declarations, expression in (
            ("fn sink(value: i32) {}", 'sink(panic("unused"))'),
            ("fn sink(value: i32) {}", '{ let f = sink; f(panic("unused")) }'),
            ("fn sink(value: i32) {}", '{ let f = sink; let p = f@; p(panic("unused")) }'),
            ("", '{ let f = fn(value: i32) {}; f(panic("unused")) }'),
            ("fn[T] sink(value: T) {}", 'sink[i32](panic("unused"))'),
            ("fn[T] sink(first: T, second: i32) {}", 'sink(1, panic("unused"))'),
            ("fn[T] sink(first: T, second: T) {}", 'sink(panic("unused"), 1)'),
        ):
            with self.subTest(expression=expression):
                # The empty main leaves every panic uncalled. GCC checking is
                # evidence about lowering, not execution of the Never body.
                self.assert_compiles(declarations + "\nfn stop() -> Never { " + expression + "; } fn main() {}")

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_noncompleting_method_arguments_include_generic_and_trait_methods(self):
        for declarations, method in (
            ("impl S { fn sink(self: Self@, value: i32) {} }", "sink"),
            ("impl S { fn[T] sink(self: Self@, value: T) {} }", "sink[i32]"),
            ("trait Sink { fn sink(self: Self@, value: i32); } "
             "impl Sink for S { fn sink(self: Self@, value: i32) {} }", "sink"),
            ("trait Sink { fn sink(self: Self@, value: i32) {} } impl Sink for S {}", "sink"),
        ):
            with self.subTest(declarations=declarations):
                self.assert_compiles("struct S {} " + declarations +
                    " fn stop() -> Never { let s << S {}; s." + method +
                    '(panic("unused")); } fn main() {}')

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_noncompleting_builtin_constructor_arguments(self):
        for expression in (
            'String::from(panic("unused"))',
            'File::open(panic("unused"))',
            'File::create(panic("unused"))',
            'Vec[i32]::with_capacity(panic("unused"))',
            'Box[i32]::new(panic("unused"))',
            'Shared[i32]::new(panic("unused"))',
            'Mutex[i32]::new(panic("unused"))',
            'Thread[i32]::spawn(panic("unused"))',
            'std::iter::from_fn(panic("unused"))',
            'u8::try_from(panic("unused"))',
        ):
            with self.subTest(expression=expression):
                self.assert_compiles("fn stop() -> Never { " + expression + "; } fn main() {}")

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_builtin_methods_and_format_arguments_can_stop_evaluation(self):
        for body in (
            'println("{}", panic("unused"));',
            'let[mut] text << String::new(); text.push_str(panic("unused"));',
            'let[mut] values << Vec[i32]::new(); values.push(panic("unused"));',
            'let result: i32? = Maybe::None; result.expect(panic("unused"));',
        ):
            with self.subTest(body=body):
                self.assert_compiles("fn stop() -> Never { " + body + " } fn main() {}")

    def test_never_callee_and_receiver_without_signatures_remain_rejected(self):
        for expression, code in (
            ("({ return 7; })(unreachable)", "XE-CALL-0001"),
            ("({ return 7; }).unreachable_method(unreachable)", "XE-NAME-0001"),
        ):
            with self.subTest(expression=expression):
                # Never supplies no callable signature or member owner here.
                # This stage does not make arbitrary callee shapes callable.
                errors = check_source("fn value() -> i32 { " + expression + "; } fn main() {}")
                self.assertTrue(errors, expression)
                self.assertEqual(errors[0].code, code, errors[0].render())

    def test_noncompleting_calls_still_check_later_argument_names(self):
        for declarations, body in (
            ("fn sink(first: i32, second: i32) {}", "sink({ return 7; }, unreachable);"),
            ("fn sink(first: i32, second: i32) {}",
             "let f = sink; f({ return 7; }, unreachable);"),
            ("fn[T] sink(first: i32, second: T) {}",
             "sink[i32]({ return 7; }, unreachable);"),
            ("", "let f = fn(first: i32, second: i32) {}; f({ return 7; }, unreachable);"),
            ("struct S {} impl S { fn sink(self: Self@, first: i32, second: i32) {} }",
             "let s << S {}; s.sink({ return 7; }, unreachable);"),
            ("", 'println("{} {}", { return 7; }, unreachable);'),
        ):
            with self.subTest(body=body):
                errors = check_source(declarations + " fn value() -> i32 { " + body + " } fn main() {}")
                self.assertTrue(errors, body)
                self.assertEqual(errors[0].code, "XE-NAME-0001", errors[0].render())

    def test_noncompleting_calls_keep_existing_resource_argument_checks(self):
        # The checking policy still examines each syntactically supplied
        # argument, although the runtime never reaches later ones.
        errors = check_source('''
        fn sink(first: i32, second: String, third: String) {}
        fn value(flag: bool) -> usize {
            let text << String::from("owned");
            if flag { sink({ return 7; }, text, text); } else {};
            text.len()
        }
        fn main() {}
        ''')
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-MOVE-0001", errors[0].render())

    def test_reached_calls_still_check_arity_types_and_duplicate_resource_moves(self):
        for source, code in (
            ('fn sink(value: i32) {} fn main() { sink("wrong"); }', "XE-TYPE-0001"),
            ('''fn sink(first: i32, second: i32) {}
                fn main() { sink(panic("unused"), "wrong"); }''', "XE-TYPE-0001"),
            ('fn sink(value: i32) {} fn main() { sink(panic("unused"), 1); }', "XE-CALL-0001"),
            ('''fn sink(first: String, second: String) {}
                fn main() { let text << String::from("owned"); sink(text, text); }''', "XE-MOVE-0001"),
            ('''fn[T] sink(value: T) {}
                fn main() { sink(panic("unused")); }''', "XE-GENERIC-0001"),
            ('''fn[T] sink(first: T, second: T) { unknown }
                fn main() { sink(panic("unused"), 1); }''', "XE-NAME-0001"),
            ('fn main() { println("{}", panic("unused"), 1); }', "XE-FORMAT-0001"),
            ('fn main() { println("{:unknown}", panic("unused")); }', "XE-SEM-0001"),
            ('fn main() { println(panic("unused")); }', "XE-SEM-0001"),
            ('fn main() { f32::try_from(panic("unused")); }', "XE-SEM-0001"),
        ):
            with self.subTest(source=source):
                errors = check_source(source)
                self.assertTrue(errors, source)
                self.assertEqual(errors[0].code, code, errors[0].render())

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_noncompleting_function_body_does_not_terminate_function_creation(self):
        self.assert_runs('''
        fn value() -> i32 {
            let item << String::from("owned");
            let callback << fn[item]() -> Never { panic("unused"); };
            let function = fn() -> Never { panic("also unused"); };
            42
        }
        fn main() { println("{}", value()); }
        ''', "42\n")

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_return_during_direct_call_cleans_earlier_resource_arguments(self):
        self.assert_runs(TRACE + '''
        fn sink(item: Trace, number: i32) { println("unreachable sink"); }
        fn value() -> i32 {
            let outer << trace(0);
            sink(trace(1), { return 7; });
            0
        }
        fn main() { println("{}", value()); }
        ''', "drop 1\ndrop 0\n7\n")

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_return_during_closure_call_cleans_arguments_and_callee_environment(self):
        self.assert_runs(TRACE + '''
        fn value() -> i32 {
            let item << trace(10);
            let callback << fn[item](argument: Trace, number: i32) {};
            callback(trace(1), { return 7; });
            0
        }
        fn main() { println("{}", value()); }
        ''', "drop 1\ndrop 10\n7\n")

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_return_during_method_call_cleans_receiver_and_earlier_arguments(self):
        self.assert_runs(TRACE + '''
        impl Trace { fn sink(self: Self, argument: Trace, number: i32) {} }
        fn value() -> i32 {
            let outer << trace(0);
            trace(1).sink(trace(2), { return 7; });
            0
        }
        fn main() { println("{}", value()); }
        ''', "drop 2\ndrop 1\ndrop 0\n7\n")

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_only_completed_call_branch_contributes_initialization_state(self):
        self.assert_runs('''
        fn sink(value: i32) {}
        fn value(flag: bool) -> i32 {
            let result: i32;
            if flag { sink({ return 7; }); }
            else { result = 42; };
            result
        }
        fn main() { println("{} {}", value(true), value(false)); }
        ''', "7 42\n")


if __name__ == "__main__":
    unittest.main()
