"""Run capture-free function values as ordinary C function pointers.

The language already distinguishes fn values from captured closures. These tests
exercise that existing distinction without introducing an implicit capture rule.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast import parse_source
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source

CC = shutil.which("cc") or ""
ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(CC, "需要系统 C 编译器")
class FunctionValueExecutionTests(unittest.TestCase):
    def run_source(self, text, *, sanitize=False):
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "test.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, True, CC, flags)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_existing_anonymous_function_example(self):
        text = (ROOT / "tests/language/anonymous_function.xe").read_text()
        self.assertEqual(self.run_source(text, sanitize=True), "42\n42\n")

    def test_named_values_as_arguments_return_values_and_if_choices(self):
        text = '''fn twice(n: i32) -> i32 { n * 2 }
        fn next(n: i32) -> i32 { n + 1 }
        fn choose(ok: bool) -> fn(i32) -> i32 { if ok { twice } else { next } }
        fn apply(f: fn(i32) -> i32, n: i32) -> i32 { f(n) }
        fn main() {
            let f: fn(i32) -> i32 = choose(true);
            println("{} {} {}", apply(f, 21), choose(false)(41), (if false { twice } else { next })(41));
        }'''
        self.assertEqual(self.run_source(text, sanitize=True), "42 42 42\n")

    def test_anonymous_functions_are_independent_functions(self):
        text = '''fn make() -> fn(i32) -> i32 { fn(n: i32) -> i32 { n + 1 } }
        fn nested() -> fn() -> (fn(i32) -> i32) {
            fn() -> (fn(i32) -> i32) { fn(n: i32) -> i32 { n * 2 } }
        }
        fn main() {
            let f = make();
            println("{} {}", f(41), nested()()(21));
        }'''
        self.assertEqual(self.run_source(text, sanitize=True), "42 42\n")

    def test_pipe_and_branch_targets_accept_function_values(self):
        text = '''fn twice(n: i32) -> i32 { n * 2 }
        fn next(n: i32) -> i32 { n + 1 }
        fn main() {
            let f = twice;
            let a = 20 |> fn(n: i32) -> i32 { n + 1 } |> f;
            let result: i32? = Maybe::Yes[21];
            let b = result? 1> f 2> fn() -> i32 { 0 };
            let c = 41 |> (if true { next } else { twice });
            println("{} {} {}", a, b, c);
        }'''
        self.assertEqual(self.run_source(text), "42 42 42\n")

    def test_resource_parameters_move_into_anonymous_function_and_return(self):
        text = '''fn main() {
            let f: fn(String) -> String = fn(text: String) -> String { text };
            let owned << String::from("hello");
            let returned << f(owned);
            println("{}", returned);
            let ignored = fn(text: String) { println("discard"); };
            ignored(String::from("unused"));
        }'''
        self.assertEqual(self.run_source(text, sanitize=True), "hello\ndiscard\n")

    def test_shadowed_named_function_is_called_as_local_value(self):
        text = '''fn transform(n: i32) -> i32 { n + 100 }
        fn twice(n: i32) -> i32 { n * 2 }
        fn main() { let transform = twice; println("{}", transform(21)); }'''
        self.assertEqual(self.run_source(text), "42\n")

    def test_function_pointer_grouping_and_dereference(self):
        text = '''fn twice(n: i32) -> i32 { n * 2 }
        fn main() {
            let function: fn(i32) -> i32 = twice;
            let address: (fn(i32) -> i32)@ = function@;
            println("{}", (address#)(21));
        }'''
        self.assertEqual(self.run_source(text, sanitize=True), "42\n")

    def test_function_values_inside_tuple_array_and_optional_layouts(self):
        text = '''fn twice(n: i32) -> i32 { n * 2 }
        fn next(n: i32) -> i32 { n + 1 }
        fn main() {
            let functions = [twice, next];
            let pair = tuple[twice, next];
            let maybe: (fn(i32) -> i32)? = Maybe::Yes[twice];
            println("{} {} {}", functions[0](21), (pair.1)(41), maybe?[panic](21));
        }'''
        self.assertEqual(self.run_source(text, sanitize=True), "42 42 42\n")

    def test_returned_borrows_keep_the_argument_alive(self):
        text = '''fn main() {
            let owner << String::from("hello");
            let identity: fn(str) -> str = fn(text: str) -> str { text };
            let view: str = identity(owner.as_str());
            println("{}", view);
        }'''
        self.assertEqual(self.run_source(text, sanitize=True), "hello\n")

    def test_function_typedefs_can_reference_user_struct_layouts(self):
        text = '''struct S { x: i32, }
        impl Copy for S;
        struct Runner { callback: fn(i32) -> i32, }
        impl Copy for Runner {}
        fn step(value: S) -> S { S { .x = value.x + 1; } }
        fn twice(value: i32) -> i32 { value * 2 }
        fn main() {
            let f: fn(S) -> S = step;
            let result = f(S { .x = 41; });
            let runner = Runner { .callback = twice; };
            println("{} {}", result.x, (runner.callback)(21));
        }'''
        self.assertEqual(self.run_source(text, sanitize=True), "42 42\n")

    def test_callee_expression_is_evaluated_once_before_arguments(self):
        text = '''fn twice(n: i32) -> i32 { n * 2 }
        fn select(calls: i32@[mut]) -> fn(i32) -> i32 {
            calls# = calls# + 1; twice
        }
        fn main() {
            let[mut] calls = 0;
            let result = select(calls@[mut])(21);
            println("{} {}", result, calls);
        }'''
        self.assertEqual(self.run_source(text, sanitize=True), "42 1\n")

    def test_return_inside_array_struct_and_tuple_initializers_cleans_previous_fields(self):
        text = '''struct Pair { first: String, second: String, }
        fn array() -> Array[String, 2] {
            [String::from("discard array"), { return [String::from("a"), String::from("b")]; }]
        }
        fn object() -> Pair {
            Pair { .first << String::from("discard struct");
                   .second << { return Pair { .first << String::from("c"); .second << String::from("d"); }; }; }
        }
        fn pair_of_strings() -> tuple[String, String] {
            tuple[String::from("discard tuple"), { return tuple[String::from("e"), String::from("f")]; }]
        }
        fn main() {
            let items << array();
            for item in items { println("{}", item); }
            let pair << object(); println("{} {}", pair.first@, pair.second@);
            let two << pair_of_strings(); println("{} {}", two.0@, two.1@);
        }'''
        self.assertEqual(self.run_source(text, sanitize=True), "a\nb\nc d\ne f\n")

    def test_return_inside_named_and_indirect_call_arguments_does_not_call_callee(self):
        text = '''fn consume(text: String) -> String { text }
        fn direct() -> String { consume({ return String::from("direct"); }) }
        fn indirect() -> String {
            let f: fn(String) -> String = consume;
            f({ return String::from("indirect"); })
        }
        fn main() { println("{} {}", direct(), indirect()); }'''
        self.assertEqual(self.run_source(text, sanitize=True), "direct indirect\n")

    def test_return_inside_runtime_builtin_arguments_stops_lowering(self):
        text = '''fn output() -> i32 { println("{}", { return 42; }); 0 }
        fn construct() -> String { String::from({ return String::from("built"); }) }
        fn update() -> String {
            let[mut] text << String::from("discard");
            text.push_str({ return String::from("updated"); });
            text
        }
        fn main() { println("{} {} {}", output(), construct(), update()); }'''
        self.assertEqual(self.run_source(text, sanitize=True), "42 built updated\n")

    def test_return_inside_resource_reassignment_preserves_scope_cleanup(self):
        text = '''fn replace() {
            let[mut] value << String::from("discard");
            value << { return; };
        }
        fn bind() { let value: String << { return; }; }
        fn main() { replace(); bind(); println("done"); }'''
        self.assertEqual(self.run_source(text, sanitize=True), "done\n")


class FunctionValueDiagnosticTests(unittest.TestCase):
    def assert_escape_warning_and_lowering(self, text):
        checker = Checker(Source(text), parse_source(text))
        self.assertEqual(checker.check(), [])
        self.assertTrue(any(warning.code == "XE-PTR-0001" and warning.severity == "warning"
                            for warning in checker.warnings))
        # 只编译，不运行已知悬垂视图。普通指针模型允许编译，但不保证
        # 运行安全；ASan 正例仍由上面的执行测试分别验证。
        self.assertIsInstance(lower_to_c(text), str)

    def test_captured_environment_has_concrete_c_layout(self):
        text = '''fn main() {
            let n = 1; let captured << fn[n](x: i32) -> i32 { x + n };
            println("{}", captured(2));
        }'''
        self.assertIn("closure_5f_environment", lower_to_c(text))

    def test_indirect_return_reports_unsafe_view_escape(self):
        text = '''fn bad() -> str {
            let owner << String::from("owned");
            let identity: fn(str) -> str = fn(text: str) -> str { text };
            identity(owner.as_str())
        }
        fn main() {}'''
        self.assert_escape_warning_and_lowering(text)

    def test_anonymous_function_return_reports_unsafe_resource_view(self):
        text = '''fn main() {
            let invalid = fn() -> str { let owner << String::from("owned"); owner.as_str() };
        }'''
        self.assert_escape_warning_and_lowering(text)
