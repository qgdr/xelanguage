"""Executable result/array checks, including ownership on both result paths.

These are end-to-end tests: parsing and checking alone cannot detect a double drop
in generated C. Sanitized executions cover the resource paths as well.
"""
from pathlib import Path
import shutil
import json
import subprocess
import tempfile
import unittest

from compiler.xe_ast.build import build_executable

CC = shutil.which("cc")
ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(CC, "需要系统 C 编译器")
class ResultExecutionTests(unittest.TestCase):
    def run_source(self, text, *, sanitize=False, status=0):
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "test.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, True, CC, flags)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, status, result.stderr)
        if status == 0:
            self.assertEqual(result.stderr, "")
        return result

    def test_existing_maybe_example(self):
        text = (ROOT / "tests/stage999/maybe_error.xe").read_text()
        self.assertEqual(self.run_source(text).stdout, "10\n")

    def test_optional_named_and_channel_branches(self):
        text = '''fn optional(ok: bool) -> i64? { if ok { 42 } else { None } }
        fn main() {
            let a = optional(true)? 1> n -> n 2> _ -> 0;
            let b = optional(false)? { Maybe::Yes :> n -> n, Maybe::None :> _ -> -1, };
            println("{} {}", a, b);
        }'''
        self.assertEqual(self.run_source(text).stdout, "42 -1\n")

    def test_propagation_moves_errors_and_cleans_local_resources(self):
        text = '''struct Trace { n: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        fn inner(ok: bool) -> Trace?[Trace] {
            if ok { Trace { .n = 1; } } else { Maybe::No[Trace { .n = 2; }] }
        }
        fn outer(ok: bool) -> Trace?[Trace] {
            let local << Trace { .n = 3; };
            let t << inner(ok)?[return];
            t
        }
        fn main() {
            outer(true)? 1> t -> { println("yes {}", t.n); } 2> t -> { println("no {}", t.n); };
            outer(false)? 1> t -> { println("yes {}", t.n); } 2> t -> { println("no {}", t.n); };
        }'''
        expected = "drop 3\nyes 1\ndrop 1\ndrop 3\nno 2\ndrop 2\n"
        self.assertEqual(self.run_source(text, sanitize=True).stdout, expected)

    def test_result_destructors_release_only_active_payload(self):
        text = '''fn result(ok: bool) -> String?[String] {
            if ok { String::from("yes") } else { Maybe::No[String::from("no")] }
        }
        fn main() {
            let a << result(true);
            let b << result(false);
            let c << result(true);
            c ?[@] { Maybe::Yes :> s: String@ -> { println("{}", s); },
                     Maybe::No :> s: String@ -> { println("{}", s); }, };
        }'''
        self.assertEqual(self.run_source(text, sanitize=True).stdout, "yes\n")

    def test_panic_extraction_success_and_failure(self):
        text = '''fn result(ok: bool) -> i32?[str] {
            if ok { 7 } else { Maybe::No["bad number"] }
        }
        fn main() { println("{}", result(true)?[panic]); }'''
        self.assertEqual(self.run_source(text).stdout, "7\n")
        failure = self.run_source(text.replace("result(true)", "result(false)"), status=1)
        self.assertIn("bad number", failure.stderr)
        self.assertIn("result extraction failed", failure.stderr)

    def test_integer_conversion_checks_signedness_and_boundaries(self):
        text = '''fn byte(n: i64) -> u8?[ConversionError] { u8::try_from(n) }
        fn main() {
            println("{}", byte(255)?[panic]);
            println("{}", byte(256)? 1> n -> n as i32 2> error -> { println("{}", error); -1 });
            println("{}", byte(-1)? 1> n -> n as i32 2> _ -> -1);
            let maximum: u64 = 18446744073709551615;
            println("{}", i64::try_from(maximum)? 1> n -> n 2> _ -> -1);
            println("{}", u64::try_from(-1)? 1> n -> n 2> _ -> 0);
        }'''
        self.assertEqual(self.run_source(text, sanitize=True).stdout,
                         "255\ninteger conversion out of range\n-1\n-1\n-1\n0\n")

    def test_utf8_slice_checks_range_and_character_boundaries(self):
        text = '''fn main() {
            let text: str = "你好abc";
            println("{}", text.slice_bytes(0..6)?[panic]);
            println("{}", text.slice_bytes(1..3)? 1> _ -> false 2> _ -> true);
            println("{}", text.slice_bytes(6..9).expect("ASCII suffix"));
            println("{}", text.slice_bytes(8..7)? 1> _ -> false 2> _ -> true);
        }'''
        self.assertEqual(self.run_source(text, sanitize=True).stdout, "你好\ntrue\nabc\ntrue\n")

    def test_array_index_iteration_and_resource_drop(self):
        text = '''fn main() {
            let values: Array[i64, 3] = [10, 20, 30];
            let[mut] sum: i64 = 0;
            for value: i64@ in values { sum = sum + value#; }
            println("{} {}", sum, values[1]);
            let words << [String::from("hello"), String::from("world")];
            for word: String@ in words { println("{}", word); }
            let empty: Array[i32, 0] = [];
            for value: i32@ in empty { println("{}", value#); }
        }'''
        self.assertEqual(self.run_source(text, sanitize=True).stdout, "60 20\nhello\nworld\n")

    def test_array_runtime_bounds_check(self):
        text = '''fn read(index: usize) -> i32 { let values = [1, 2]; values[index] }
        fn main() { println("{}", read(2)); }'''
        failure = self.run_source(text, status=1)
        self.assertIn("array index out of bounds", failure.stderr)

    def test_nested_optional_lifting_is_not_flattening(self):
        text = '''fn inner() -> i32? { None }
        fn outer() -> i32?? { inner() }
        fn main() {
            let value: i32? = outer()?[panic];
            println("{}", value? 1> _ -> false 2> _ -> true);
        }'''
        self.assertEqual(self.run_source(text).stdout, "true\n")

    def test_file_read_size_and_error_propagation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.txt"
            path.write_text("hello 世界\n" * 1000, encoding="utf-8")
            literal = json.dumps(str(path), ensure_ascii=False)
            text = '''fn read(path: str) -> String?[io::Error] {
                let file: File << File::open(path)?[return];
                println("size {}", file.size());
                file.read_to_string()
            }
            fn main() {
                let text: String << read(INPUT)?[panic];
                println("length {}", text.len());
                let missing: i32 = read(MISSING)? 1> _ -> 1 2> _ -> 0;
                println("missing {}", missing);
            }'''.replace("INPUT", literal).replace("MISSING", json.dumps(str(path) + ".missing"))
            self.assertEqual(self.run_source(text, sanitize=True).stdout,
                             "size 13000\nlength 13000\nmissing 0\n")

    def test_non_utf8_file_returns_error_without_leaking_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.txt"
            path.write_bytes(b"\xf0\x80\x80\x80")
            literal = json.dumps(str(path))
            text = '''fn main() {
                let file: File << File::open(INPUT)?[panic];
                let invalid = file.read_to_string()? 1> _ -> false 2> error -> { println("{}", error); true };
                println("{}", invalid);
            }'''.replace("INPUT", literal)
            output = self.run_source(text, sanitize=True).stdout
            self.assertTrue(output.endswith("true\n"), output)

    def test_file_path_embedded_nul_is_error_not_truncated_path(self):
        text = '''fn main() {
            let invalid = File::open("README.md\\0ignored")? 1> _ -> false 2> _ -> true;
            println("{}", invalid);
        }'''
        self.assertEqual(self.run_source(text, sanitize=True).stdout, "true\n")

    def test_array_partial_initialization_propagation_releases_previous_elements(self):
        text = '''struct Trace { n: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
        fn failure() -> Trace? { None }
        fn construct() -> Array[Trace, 2]? {
            let items << [Trace { .n = 1; }, failure()?[return]];
            items
        }
        fn main() { construct()? 1> _ -> {} 2> _ -> {}; }'''
        self.assertEqual(self.run_source(text, sanitize=True).stdout, "drop 1\n")

    def test_array_slice_views_iteration_and_copy(self):
        text = '''fn main() {
            let[mut] target = [1, 2, 3, 4, 5];
            let source = [9, 8];
            {
                let[mut] tail: SliceMut[i32] = target.slice_mut(3..);
                let origin: Slice[i32] = source.slice(..);
                tail.copy_from(origin);
                for value: i32@ in origin { println("source {}", value#); }
            };
            for value: i32@ in target { println("target {}", value#); }
        }'''
        expected = "source 9\nsource 8\ntarget 1\ntarget 2\ntarget 3\ntarget 9\ntarget 8\n"
        self.assertEqual(self.run_source(text, sanitize=True).stdout, expected)

    def test_range_values_and_closed_maximum_with_continue(self):
        text = '''fn interval() -> Range[i32] { 2..4 }
        fn main() {
            for value in interval() { println("{}", value); }
            let maximum: u64 = 18446744073709551615;
            for value: u64 in maximum..=maximum { println("{}", value); continue; }
            for value in 3..=2 { println("unexpected"); }
        }'''
        self.assertEqual(self.run_source(text, sanitize=True).stdout, "2\n3\n18446744073709551615\n")
