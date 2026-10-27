"""Never ends evaluation; it does not convert the layout of containing values."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import check_source


class FoundationControlTests(LegacyTestCase):
    def run_source(self, source, expected, sanitize=False):
        self.assertEqual(check_source(source), [])
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "main.xe"
            binary = Path(directory) / "program"
            entry.write_text(source, encoding="utf-8")
            build_executable(entry, binary, cc="gcc", extra_flags=flags)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")

    def assert_type_error(self, source):
        errors = check_source(source)
        self.assertTrue(errors, source)
        self.assertEqual(errors[0].code, "XE-TYPE-0001", errors[0].render())

    def test_never_does_not_convert_function_parameters_or_results(self):
        self.assert_type_error('''
        fn fake(value: Never) -> Never { value }
        fn main() { let f: fn(i32) -> Never = fake; f(1); }
        ''')
        self.assert_type_error('''
        fn stop() -> Never { panic("unused"); }
        fn main() { let f: fn() -> i32 = stop; }
        ''')

    def test_never_does_not_change_container_element_types(self):
        self.assert_type_error('fn main() { let value: Vec[i32] << Vec[Never]::new(); }')
        self.assert_type_error('''
        fn main() {
            let empty: Array[Never, 0] = [];
            let other: Array[i32, 0] = empty;
        }
        ''')

    def test_never_does_not_change_user_generic_arguments(self):
        self.assert_type_error('''
        struct[T] Empty {}
        fn main() {
            let empty << Empty[Never] {};
            let other: Empty[i32] << empty;
        }
        ''')

    def test_never_does_not_change_nested_tuple_or_result_types(self):
        for source in (
            'fn f(x: tuple[Array[Never, 0]]) -> tuple[Array[i32, 0]] { x } fn main() {}',
            'fn f(x: Never?) -> i32? { x } fn main() {}',
            'fn f(x: i32?[Never]) -> i32?[i32] { x } fn main() {}',
        ):
            with self.subTest(source=source):
                self.assert_type_error(source)

    def test_same_never_containing_types_and_function_signatures_remain_valid(self):
        source = '''
        struct[T] Empty {}
        fn stop() -> Never { panic("unused"); }
        fn main() {
            let empty << Empty[Never] {};
            let same: Empty[Never] << empty;
            let array: Array[Never, 0] = [];
            let same_array: Array[Never, 0] = array;
            let f: fn() -> Never = stop;
        }
        '''
        self.assertEqual(check_source(source), [])

    def test_never_initializer_ends_its_block(self):
        for source in (
            'fn stop() -> Never { let x: i32 = panic("stop"); } fn main() {}',
            'fn stop() -> Never { let x: String << panic("stop"); } fn main() {}',
            'fn main() { let x: i32 = panic("stop"); unreachable_name; }',
        ):
            with self.subTest(source=source):
                self.assertEqual(check_source(source), [])

    def test_never_assignment_ends_its_block_without_initializing_target(self):
        for source in (
            'fn stop() -> Never { let x: i32; x = panic("stop"); } fn main() {}',
            'fn main() { let[mut] x = 1; x = panic("stop"); unreachable_name; }',
        ):
            with self.subTest(source=source):
                self.assertEqual(check_source(source), [])

    def test_never_functions_still_reject_normal_completion(self):
        for source in (
            'fn invalid() -> Never {} fn main() {}',
            'fn invalid() -> Never { return; } fn main() {}',
            'fn invalid() -> Never { unit } fn main() {}',
        ):
            with self.subTest(source=source):
                self.assert_type_error(source)

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_noncompleting_variant_payloads_compile_without_running_panic(self):
        for source in (
            'fn f() -> i32? { Maybe::Yes[panic("stop")] } fn main() {}',
            'fn f() -> i32?[str] { Maybe::No[panic("stop")] } fn main() {}',
            'fn f() -> Never { Maybe::Yes[panic("stop")] } fn main() {}',
            '''type Outcome = i32?[str];
               fn f() -> Outcome { Maybe::No[panic("stop")] } fn main() {}''',
            '''enum[T] Event { Ready[T], Stop, }
               fn f() -> Event[i32] { Event[i32]::Ready[panic("stop")] } fn main() {}''',
        ):
            with self.subTest(source=source):
                self.assertEqual(check_source(source), [])
                generated = lower_to_c(source)
                result = subprocess.run(
                    ["gcc", "-x", "c", "-std=c11", "-fsyntax-only", "-"],
                    input=generated, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")

    def variant_payload_return_source(self):
        return '''
        struct Trace { number: i32, text: String, }
        impl Drop for Trace {
            fn drop(self: Self@[mut]) { println("drop {}", self.number); }
        }
        enum Pack { All[Trace, Trace, i32], }
        impl Drop for Pack {
            fn drop(self: Self@[mut]) { println("drop pack"); }
        }
        fn make(number: i32) -> Trace {
            Trace { .number = number; .text << String::from("owned"); }
        }
        fn construct() -> i32 {
            let outer << make(0);
            let incomplete: Pack << Pack::All[make(1), make(2), { return 7; }];
        }
        fn main() { println("{}", construct()); }
        '''

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_noncompleting_variant_cleans_earlier_resource_payloads(self):
        self.run_source(self.variant_payload_return_source(), "drop 2\ndrop 1\ndrop 0\n7\n")

    @unittest.skipUnless(shutil.which("gcc") and sys.platform == "linux", "requires Linux/GCC sanitizers")
    def test_noncompleting_variant_resource_cleanup_with_sanitizers(self):
        self.run_source(self.variant_payload_return_source(), "drop 2\ndrop 1\ndrop 0\n7\n", sanitize=True)

    @unittest.skipUnless(shutil.which("gcc"), "requires GCC")
    def test_initialization_merges_only_paths_that_complete(self):
        source = '''
        fn binding(flag: bool) -> i32 {
            let x: i32;
            if flag { let ignored: i32 = { return 7; }; }
            else { x = 42; };
            x
        }
        fn assignment(flag: bool) -> i32 {
            let x: i32;
            if flag { x = { return 8; }; }
            else { x = 43; };
            x
        }
        fn main() {
            println("{} {} {} {}", binding(true), binding(false),
                    assignment(true), assignment(false));
        }
        '''
        self.run_source(source, "7 42 8 43\n")

    @unittest.skipUnless(shutil.which("gcc") and sys.platform == "linux", "requires Linux/GCC sanitizers")
    def test_noncompleting_initialization_and_replacement_cleanup(self):
        # RHS return cleans its temporary and the still-owned old value. The
        # never-completed declaration/write must not create another owner.
        source = '''
        struct Trace { number: i32, text: String, }
        impl Drop for Trace {
            fn drop(self: Self@[mut]) { println("drop {}", self.number); }
        }
        fn make(number: i32) -> Trace {
            Trace { .number = number; .text << String::from("owned"); }
        }
        fn initialize() -> i32 {
            let owner << make(1);
            let unused: Trace << { let temporary << make(2); return 7; };
        }
        fn replace() -> i32 {
            let[mut] owner << make(3);
            owner << { let temporary << make(4); return 8; };
        }
        fn main() { println("{} {}", initialize(), replace()); }
        '''
        self.run_source(source, "drop 2\ndrop 1\ndrop 4\ndrop 3\n7 8\n", sanitize=True)


if __name__ == "__main__":
    unittest.main()
