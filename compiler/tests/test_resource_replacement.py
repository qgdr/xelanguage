"""所有合法写入位置使用同一资源替换规则：先求新值，再 Drop 旧值。"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import check_source


@unittest.skipUnless(shutil.which("cc"), "需要 C 编译器")
class ResourceReplacementTests(unittest.TestCase):
    def execute_xe(self, text, sanitize=False):
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.xe"
            executable = Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, executable, extra_flags=flags)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
        return result

    def run_xe(self, text, expected, sanitize=False):
        result = self.execute_xe(text, sanitize)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)

    def test_user_drop_replacement_through_pointer_array_and_vector(self):
        self.run_xe('''struct Trace { n: i32, text: String, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
            fn make(n: i32) -> Trace {
                println("make {}", n); Trace { .n = n; .text << String::from("owned"); }
            }
            fn replace(pointer: Trace@[mut], n: i32) { pointer# << make(n); }
            fn main() {
                { let[mut] value << make(1); replace(value@[mut], 2); println("value {}", value.n); };
                { let[mut] array << [make(3), make(4)]; array[0] << make(5); println("array {}", array[0].n); };
                { let[mut] vector << Vec[Trace]::new(); vector.push(make(6));
                  vector[0] << make(7); println("vector {}", vector[0].n); };
            }''', "make 1\nmake 2\ndrop 1\nvalue 2\ndrop 2\n"
                "make 3\nmake 4\nmake 5\ndrop 3\narray 5\ndrop 4\ndrop 5\n"
                "make 6\nmake 7\ndrop 6\nvector 7\ndrop 7\n")

    def test_nested_owned_containers_and_shared_replacement(self):
        self.run_xe('''struct Trace { n: i32, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
            fn boxed(n: i32) -> Box[Trace] { Box[Trace]::new(Trace { .n = n; })?[panic] }
            fn main() {
                { let[mut] boxes << [boxed(1), boxed(2)]; boxes[0] << boxed(3); };
                { let[mut] outer << Vec[Vec[Box[Trace]]]::new();
                  let[mut] first << Vec[Box[Trace]]::new(); first.push(boxed(4)); outer.push(first);
                  let[mut] second << Vec[Box[Trace]]::new(); second.push(boxed(5)); outer[0] << second; };
                { let[mut] shared << Shared[Trace]::new(Trace { .n = 6; })?[panic];
                  let pointer = shared@[mut]; pointer# << Shared[Trace]::new(Trace { .n = 7; })?[panic]; };
            }''', "drop 1\ndrop 2\ndrop 3\ndrop 4\ndrop 5\ndrop 6\ndrop 7\n")

    def test_return_during_replacement_keeps_old_value_owned(self):
        self.run_xe('''struct Trace { n: i32, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
            fn early() {
                let[mut] values << [Trace { .n = 1; }];
                values[0] << { let temporary << Trace { .n = 2; }; return; };
            }
            fn main() { early(); println("done"); }''', "drop 2\ndrop 1\ndone\n")

    def test_replacing_moved_field_does_not_drop_old_value_twice(self):
        self.run_xe('''struct Trace { n: i32, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
            struct Pair { left: Trace, right: Trace, }
            fn main() {
                let[mut] pair << Pair { .left << Trace { .n = 1; }; .right << Trace { .n = 2; }; };
                let old << pair.left;
                pair.left << Trace { .n = 3; };
                println("left {}", pair.left.n);
            }''', "left 3\ndrop 1\ndrop 3\ndrop 2\n")

    def test_generic_drop_replacement_in_container_and_through_pointer(self):
        self.run_xe('''struct[T] Holder { number: i32, value: T, }
            impl[T] Drop for Holder[T] { fn drop(self: Self@[mut]) { println("holder {}", self.number); } }
            fn make(number: i32) -> Holder[String] {
                Holder[String] { .number = number; .value << String::from("owned"); }
            }
            fn replace(pointer: Holder[String]@[mut]) { pointer# << make(2); }
            fn main() {
                let[mut] value << make(1); replace(value@[mut]);
                let[mut] array << [make(3)]; array[0] << make(4);
                let[mut] vector << Vec[Holder[String]]::new(); vector.push(make(5)); vector[0] << make(6);
            }''', "holder 1\nholder 3\nholder 5\nholder 6\nholder 4\nholder 2\n")

    def test_rhs_cannot_move_owner_of_nested_target(self):
        cases = (
            '''let[mut] array << [String::from("old")];
               array[0] << { let moved << array; String::from("new") };''',
            '''let[mut] pair << Pair { .left << String::from("old"); .right << String::from("other"); };
               pair.left << { let moved << pair; String::from("new") };''',
            '''let[mut] pair << tuple[String::from("old"), String::from("other")];
               pair.0 << { let moved << pair; String::from("new") };''',
            '''let[mut] pair << Bag { .items << Vec[String]::new(); .right << String::from("other"); };
               pair.items.push(String::from("old"));
               pair.items[0] << { let moved << pair.items; String::from("new") };''',
        )
        declarations = ("struct Pair { left: String, right: String, } "
                        "struct Bag { items: Vec[String], right: String, }")
        for body in cases:
            with self.subTest(body=body):
                errors = check_source(declarations + " fn main() { " + body + " }")
                self.assertTrue(errors, body)
                self.assertEqual(errors[0].code, "XE-MOVE-0001")

    def test_rhs_can_reinitialize_root_and_exact_moved_field(self):
        self.run_xe('''struct Trace { n: i32, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
            struct Pair { left: Trace, right: Trace, }
            fn main() {
                let[mut] array << [Trace { .n = 1; }];
                array[0] << { array << [Trace { .n = 2; }]; Trace { .n = 3; } };
                let[mut] pair << Pair { .left << Trace { .n = 4; }; .right << Trace { .n = 5; }; };
                pair.left << { let old << pair.left; Trace { .n = 6; } };
                println("{} {}", array[0].n, pair.left.n);
            }''', "drop 1\ndrop 2\ndrop 4\n3 6\ndrop 6\ndrop 5\ndrop 3\n")
        self.run_xe('''struct Trace { n: i32, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
            fn main() {
                let[mut] values << Vec[Trace]::new(); values.push(Trace { .n = 1; });
                values[0] << {
                    let moved << values;
                    values << Vec[Trace]::new(); values.push(Trace { .n = 2; });
                    Trace { .n = 3; }
                };
                println("first {}", values[0].n);
            }''', "drop 1\ndrop 2\nfirst 3\ndrop 3\n")

    def test_rhs_clear_rechecks_element_bounds_before_replacement(self):
        result = self.execute_xe('''fn main() {
            let[mut] values << Vec[i32]::new(); values.push(1);
            values[0] = { values.clear(); 2 };
        }''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("array index out of bounds", result.stderr)
        self.assertIn("main.xe:3:13", result.stderr)

    def test_inner_index_side_effect_rechecks_outer_vector(self):
        result = self.execute_xe('''fn main() {
            let[mut] outer << Vec[Vec[i32]]::new();
            let[mut] inner << Vec[i32]::new(); inner.push(1); outer.push(inner);
            outer[0][{ outer.clear(); 0 }] = 2;
        }''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("array index out of bounds", result.stderr)
        self.assertIn("main.xe:4:13", result.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie 需要 Linux")
    def test_rhs_growth_relocates_indexed_target_without_use_after_free(self):
        self.run_xe('''struct Trace { n: i32, text: String, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
            fn make(n: i32) -> Trace { Trace { .n = n; .text << String::from("owned"); } }
            fn index(calls: i32@[mut]) -> usize { calls# = calls# + 1; 0 }
            fn main() {
                let[mut] calls = 0;
                let[mut] values << Vec[Trace]::with_capacity(1);
                values.push(make(1)); values.push(make(2));
                values.push(make(3)); values.push(make(4));
                values[index(calls@[mut])] << { values.push(make(5)); make(6) };
                println("calls {} first {} len {}", calls, values[0].n, values.len());
            }''', "drop 1\ncalls 1 first 6 len 5\ndrop 5\ndrop 4\ndrop 3\ndrop 2\ndrop 6\n", sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie 需要 Linux")
    def test_rhs_growth_relocates_nested_indexed_target(self):
        self.run_xe('''struct Trace { n: i32, text: String, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
            fn make(n: i32) -> Trace { Trace { .n = n; .text << String::from("owned"); } }
            fn main() {
                let[mut] outer << Vec[Vec[Trace]]::new();
                let[mut] inner << Vec[Trace]::new(); inner.push(make(1)); outer.push(inner);
                outer.push(Vec[Trace]::new()); outer.push(Vec[Trace]::new());
                outer.push(Vec[Trace]::new());
                outer[0][0] << { outer.push(Vec[Trace]::new()); make(2) };
                println("first {} len {}", outer[0][0].n, outer.len());
            }''', "drop 1\nfirst 2 len 5\ndrop 2\n", sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie 需要 Linux")
    def test_all_replacement_paths_preserve_sanitizer_and_leak_checks(self):
        self.run_xe('''struct Trace { n: i32, text: String, }
            impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.n); } }
            fn make(n: i32) -> Box[Trace] {
                Box[Trace]::new(Trace { .n = n; .text << String::from("payload"); })?[panic]
            }
            fn main() {
                let[mut] array << [make(1)]; array[0] << make(2);
                let[mut] vector << Vec[Box[Trace]]::new(); vector.push(make(3)); vector[0] << make(4);
                let pointer = array[0]@[mut]; pointer# << make(5);
            }''', "drop 1\ndrop 3\ndrop 2\ndrop 4\ndrop 5\n", sanitize=True)
