"""Monomorphization must preserve values, ownership and C evaluation order.

The semantic checker selects concrete signatures; the backend only lowers those
checked instances. Tests compile and run the resulting C, including resource
instances under ASan/UBSan, instead of merely searching generated source text.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.source import Diagnostic

CC = shutil.which("cc") or ""
ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(CC, "需要系统 C 编译器")
class GenericBackendTests(unittest.TestCase):
    def run_source(self, text, *, sanitize=True):
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "test.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, cc=CC, extra_flags=flags)
            completed = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        return completed.stdout

    def test_project_fixture(self):
        text = (ROOT / "tests/backend/generic_instances.xe").read_text(encoding="utf-8")
        self.assertEqual(self.run_source(text), "42 hello payload 7\n")

    def test_same_function_has_independent_numeric_and_resource_instances(self):
        text = '''fn[T] identity(value: T) -> T { value }
        fn main() { let a = identity(42); let text << identity(String::from("hello"));
            println("{} {} {}", a, identity[i64](43), text@); }'''
        self.assertEqual(self.run_source(text), "42 43 hello\n")

    def test_nested_struct_instantiations_have_distinct_complete_layouts(self):
        text = '''struct[T] Holder { value: T, }
        fn[T] wrap(value: T) -> Holder[T] { Holder[T] { value >> .value; } }
        fn main() { let a << wrap(wrap(42)); let b << wrap(wrap(String::from("hello")));
            println("{} {}", a.value.value, b.value.value@); }'''
        self.assertEqual(self.run_source(text), "42 hello\n")

    def test_array_and_optional_generic_return_layouts(self):
        text = '''fn[T: Copy] duplicate(value: T) -> Array[T, 2] { [value, value] }
        fn[T] present(value: T) -> T? { value }
        fn main() { let values = duplicate(21);
            let text << present(String::from("hello"))?[panic];
            println("{} {}", values[0] + values[1], text@); }'''
        self.assertEqual(self.run_source(text), "42 hello\n")

    def test_generic_result_propagates_owned_error_without_double_drop(self):
        text = '''fn[T, E] forward(value: T?[E]) -> T?[E] { value?[return] }
        fn main() {
            let good: i32?[String] << Maybe::Yes[42];
            let bad: i32?[String] << Maybe::No[String::from("error")];
            let a << forward(good); let b << forward(bad);
            a? 1> n -> println("{}", n) 2> e -> println("{}", e);
            b? 1> n -> println("{}", n) 2> e -> println("{}", e);
        }'''
        self.assertEqual(self.run_source(text), "42\nerror\n")

    def test_generic_enum_payload_and_empty_variants(self):
        text = '''enum[T] Event { Value[T], End, }
        fn read(event: Event[i32]) -> i32 { event? {
            Event::Value :> number -> number, Event::End :> _ -> 0,
        } }
        fn main() { let a << Event[i32]::Value[42]; let b << Event[i32]::End;
            let c: Event[i32] << Event::End;
            println("{} {} {}", read(a), read(b), read(c)); }'''
        self.assertEqual(self.run_source(text), "42 0 0\n")

    def test_generic_resource_enum_drops_only_its_active_payload(self):
        text = '''struct Trace { number: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.number); } }
        enum[T] Event { Value[T], End, }
        fn main() { let a << Event[Trace]::Value[Trace { .number = 1; }];
            let b << Event[Trace]::End; }'''
        self.assertEqual(self.run_source(text), "drop 1\n")

    def test_partial_move_of_generic_struct_drops_each_resource_once(self):
        text = '''struct Trace { number: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.number); } }
        struct[T, U] Pair { first: T, second: U, }
        fn consume(trace: Trace) { println("take {}", trace.number); }
        fn main() { let pair << Pair[Trace, Trace] {
            Trace { .number = 1; } >> .first; Trace { .number = 2; } >> .second;
        }; consume(pair.first); println("remaining {}", pair.second.number); }'''
        self.assertEqual(self.run_source(text), "take 1\ndrop 1\nremaining 2\ndrop 2\n")

    def test_generic_enum_pointer_match_copies_addresses_without_owning_payload(self):
        text = '''enum[T] Event { Value[T], End, }
        fn main() { let[mut] token << Event[String]::Value[String::from("hello")];
            token ?[@[mut]] { Event::Value :> text: String@[mut] -> text.push_str("!"),
                Event::End :> _ -> {}, };
            token ?[@] { Event::Value :> text: String@ -> println("{}", text),
                Event::End :> _ -> {}, };
        }'''
        self.assertEqual(self.run_source(text), "hello!\n")

    def test_generic_recursive_call_reuses_its_checked_instance(self):
        text = '''fn[T] repeat(value: T, n: i32) -> T {
            if n == 0 { value } else { repeat(value, n - 1) }
        }
        fn main() { let value << repeat(String::from("hello"), 4); println("{}", value@); }'''
        self.assertEqual(self.run_source(text), "hello\n")

    def test_bare_generic_pipe_targets_are_inferred_from_payloads(self):
        text = '''fn[T] identity(value: T) -> T { value }
        fn main() { let a = 42 |> identity;
            let maybe: i32? = Maybe::Yes[42];
            let b = maybe? 1> identity 2> _ -> 0;
            let text << String::from("hello") |> identity;
            println("{} {} {}", a, b, text@); }'''
        self.assertEqual(self.run_source(text), "42 42 hello\n")

    def test_generic_method_on_struct_with_resource_and_numeric_instances(self):
        text = '''struct[T] Holder { value: T, }
        impl[T] Holder[T] {
            fn new(value: T) -> Self { Self { value >> .value; } }
            fn pointer(self: Self@) -> T@ { self.value@ }
            fn take(self: Self) -> T { self.value }
        }
        fn main() { let a << Holder[i32]::new(42); let b << Holder[String]::new(String::from("hello"));
            println("{} {}", a.pointer()#, b.pointer());
            let number = a.take(); let text << b.take(); println("{} {}", number, text@); }'''
        self.assertEqual(self.run_source(text), "42 hello\n42 hello\n")

    def test_generic_method_with_own_type_parameter(self):
        text = '''struct Runner { number: i32, }
        impl Runner { fn[T] pass(self: Self@, value: T) -> T { value } }
        fn main() { let r << Runner { .number = 1; };
            let a = r.pass[i32](42); let b << r.pass(String::from("hello"));
            println("{} {}", a, b@); }'''
        self.assertEqual(self.run_source(text), "42 hello\n")

    def test_generic_call_arguments_and_custom_drop_preserve_source_order(self):
        text = '''struct Trace { number: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.number); } }
        fn make(number: i32) -> Trace { println("make {}", number); Trace { .number = number; } }
        fn[T] first(a: T, b: T) -> T { a }
        fn main() { let result << first(make(1), make(2)); println("result {}", result.number); }'''
        self.assertEqual(self.run_source(text), "make 1\nmake 2\ndrop 2\nresult 1\ndrop 1\n")

    def test_pointer_recursive_generic_layout_needs_only_forward_declarations(self):
        text = '''struct[T] Node { value: T, next: Node[T]@, }
        fn touch(value: Node[i32]@) -> i32 { value.value }
        fn main() { println("ready"); }'''
        self.assertEqual(self.run_source(text), "ready\n")

    def test_expanding_pointer_targets_are_not_eagerly_completed(self):
        text = '''struct[T] Node { next: Node[Node[T]]@, }
        fn next(pointer: Node[i32]@) -> Node[Node[i32]]@ { pointer.next }
        fn main() { println("ready"); }'''
        self.assertEqual(self.run_source(text), "ready\n")

    def test_recursive_slice_descriptor_is_not_a_by_value_recursive_element(self):
        text = '''struct[T] Node { value: T, children: Slice[Node[T]], }
        fn read(node: Node[i32]@) -> i32 { node.value }
        fn main() { println("ready"); }'''
        self.assertEqual(self.run_source(text), "ready\n")

    def test_by_value_recursive_generic_is_a_language_diagnostic(self):
        text = '''struct[T] Node { next: Node[T], }
        fn consume(value: Node[i32]) {} fn main() {}'''
        with self.assertRaises(Diagnostic) as caught:
            lower_to_c(text)
        self.assertNotIsInstance(caught.exception, RecursionError)

    def test_unused_template_and_layout_are_not_required_by_c_backend(self):
        text = '''struct[T] Holder { value: T, }
        fn[T] identity(value: T) -> T { value }
        fn main() { println("ready"); }'''
        self.assertEqual(self.run_source(text), "ready\n")


if __name__ == "__main__":
    unittest.main()
