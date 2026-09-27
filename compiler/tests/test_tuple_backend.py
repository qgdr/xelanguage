"""Tuple values/destructuring and transparent aliases execute safely as C."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable

CC = shutil.which("cc")


@unittest.skipUnless(CC, "需要系统 C 编译器")
class TupleBackendTests(unittest.TestCase):
    def run_source(self, text):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "test.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, cc=CC,
                             extra_flags=("-fsanitize=address,undefined",
                                          "-fno-sanitize-recover=all", "-no-pie"))
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_nested_singleton_and_annotated_copy_destructuring(self):
        text = '''fn singleton() -> tuple[i64] { tuple[42] }
        fn main() {
            let tuple[number: i64] = singleton();
            let pair: tuple[tuple[i64], bool] = tuple[tuple[number], true];
            let tuple[nested, truth] = pair;
            println("{} {} {}", number, nested.0, truth);
        }'''
        self.assertEqual(self.run_source(text), "42 42 true\n")

    def test_rhs_call_executes_once(self):
        text = '''fn pair(counter: i32@[mut]) -> tuple[i32, i32] {
            counter# = counter# + 1;
            tuple[counter#, counter# + 10]
        }
        fn main() {
            let[mut] count = 0;
            let tuple[first, second] = pair(count@[mut]);
            println("{} {} {}", first, second, count);
        }'''
        self.assertEqual(self.run_source(text), "1 11 1\n")

    def test_copy_assignment_snapshots_before_swapping_targets(self):
        text = '''fn main() {
            let[mut] tuple[first, second] = tuple[1, 2];
            tuple[first, second] = tuple[second, first];
            println("{} {}", first, second);
            tuple[8, 9] >> tuple[first, second];
            println("{} {}", first, second);
        }'''
        self.assertEqual(self.run_source(text), "2 1\n8 9\n")

    def test_resource_assignment_preserves_snapshot_ownership(self):
        text = '''fn main() {
            let[mut] tuple[first, second] << tuple[String::from("left"), String::from("right")];
            tuple[first, second] << tuple[second, first];
            println("{} {}", first@, second@);
            tuple[String::from("new-left"), String::from("new-right")] >> tuple[first, second];
            println("{} {}", first@, second@);
        }'''
        self.assertEqual(self.run_source(text), "right left\nnew-left new-right\n")

    def test_mixed_copy_and_resource_forward_assignment(self):
        text = '''fn main() {
            let[mut] count = 0;
            let[mut] text << String::from("old");
            tuple[42, String::from("updated")] >> tuple[count, text];
            println("{} {}", count, text@);
        }'''
        self.assertEqual(self.run_source(text), "42 updated\n")

    def test_ignored_resources_and_nested_drop_run_once(self):
        text = '''struct Trace { label: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.label); } }
        struct Boxed { trace: Trace, text: String, }
        fn main() {
            let tuple[number, _] << tuple[42, Boxed {
                .trace << Trace { .label = 1; };
                .text << String::from("ignored");
            }];
            println("number {}", number);
            let[mut] retained << String::from("old");
            tuple[String::from("new"), Trace { .label = 2; }] >> tuple[retained, _];
            println("{}", retained@);
        }'''
        self.assertEqual(self.run_source(text), "drop 1\nnumber 42\ndrop 2\nnew\n")

    def test_partial_tuple_field_move_keeps_remaining_resources_live(self):
        text = '''struct Trace { label: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.label); } }
        fn main() {
            let pair << tuple[Trace { .label = 1; }, Trace { .label = 2; }];
            let first << pair.0;
            println("moved {}", first.label);
        }'''
        self.assertEqual(self.run_source(text), "moved 1\ndrop 1\ndrop 2\n")

    def test_struct_field_tuple_destructuring_keeps_other_fields_live(self):
        text = '''struct Holder { pair: tuple[String, String], remaining: String, }
        fn main() {
            let owner << Holder {
                .pair << tuple[String::from("left"), String::from("right")];
                .remaining << String::from("kept");
            };
            let tuple[first, second] << owner.pair;
            println("{} {} {}", first@, second@, owner.remaining@);
        }'''
        self.assertEqual(self.run_source(text), "left right kept\n")

    def test_early_return_cleans_initialized_tuple_members(self):
        text = '''struct Trace { label: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.label); } }
        fn leave() {
            let tuple[first: Trace, wide: i64, second: String] << tuple[
                Trace { .label = 1; }, 2147483647 + 1, { return; }
            ];
        }
        fn main() { leave(); println("done"); }'''
        self.assertEqual(self.run_source(text), "drop 1\ndone\n")

    def test_early_return_preserves_existing_destinations_until_cleanup(self):
        text = '''struct Trace { label: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.label); } }
        fn leave() {
            let[mut] first << Trace { .label = 1; };
            let[mut] second << Trace { .label = 2; };
            tuple[first, second] << tuple[Trace { .label = 3; }, { return; }];
        }
        fn main() { leave(); }'''
        self.assertEqual(self.run_source(text), "drop 3\ndrop 2\ndrop 1\n")

    def test_destructured_addresses_preserve_pointer_permissions(self):
        text = '''fn main() {
            let[mut] count = 1;
            let tuple[pointer: i32@[mut], number] = tuple[count@[mut], 2];
            pointer# = number + 40;
            println("{}", count);
        }'''
        self.assertEqual(self.run_source(text), "42\n")

    def test_tuple_alias_has_one_c_layout(self):
        text = '''type Coordinates = tuple[i32, i32];
        type Position = Coordinates;
        fn identity(value: Position) -> Coordinates { value }
        fn main() {
            let pair: Coordinates = tuple[20, 22];
            let tuple[first, second] = identity(pair);
            println("{}", first + second);
        }'''
        self.assertEqual(self.run_source(text), "42\n")
        self.assertEqual(lower_to_c(text).count("struct xe_tuple_5f_layout_5f_0 {"), 1)

    def test_struct_enum_and_standard_alias_constructors_and_methods(self):
        text = '''struct Point { value: i32, }
        impl Copy for Point;
        impl Point { fn make(value: i32) -> Self { Point { .value = value; } } }
        enum Event { Value[i32], End, }
        type P = Point;
        type E = Event;
        type Text = String;
        fn read(event: E) -> i32 {
            event? { E::Value :> value -> value, E::End :> _ -> 0, }
        }
        fn main() {
            let tuple[point, constructed] = tuple[P { .value = 20; }, P::make(22)];
            let text: Text << Text::from("alias");
            println("{} {} {} {}", point.value + constructed.value,
                    read(E::Value[42]), read(E::End), text@);
        }'''
        self.assertEqual(self.run_source(text), "42 42 0 alias\n")


if __name__ == "__main__":
    unittest.main()
