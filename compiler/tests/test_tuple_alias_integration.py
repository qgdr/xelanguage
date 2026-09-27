"""元组、透明别名和既有所有权/泛型规则必须能够组合。

语法入口的测试不足以证明实现完成：正常地址和资源程序真正编译运行，
悬垂地址只检查风险信息。这里不修改源码 AST 来伪装别名展开或隐式转换。
"""
import copy
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source

CC = shutil.which("cc")


class TupleAliasIntegrationTests(unittest.TestCase):
    def check(self, text):
        tree = parse_source(text)
        original = copy.deepcopy(tree)
        checker = Checker(Source(text), tree)
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(e.render() for e in errors))
        self.assertEqual(tree, original, "别名展开不能改写用户 AST")
        return checker

    def run_text(self, text):
        if not CC:
            self.skipTest("需要系统 C 编译器")
        with tempfile.TemporaryDirectory() as directory:
            source, program = Path(directory) / "source.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, program, cc=CC,
                             extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            return result.stdout

    def test_aliases_function_tuple_and_generic_instance(self):
        text = '''type Pair = tuple[i32, handler];
        type handler = fn(i32) -> i32;
        fn[T] identity(value: T) -> T { value }
        fn next(value: i32) -> i32 { value + 1 }
        fn foo() -> Pair { tuple[41, next] }
        fn main() {
            let tuple[number, callback] = identity[Pair](foo());
            println("{}", callback(number));
        }'''
        self.assertEqual(self.run_text(text), "42\n")

    def test_alias_constructor_and_methods_use_original_type(self):
        text = '''type P = Point;
        struct Point { number: i32, }
        impl Copy for Point;
        impl Point {
            fn new(number: i32) -> Self { Self { .number = number; } }
            fn read(self: Self@) -> i32 { self.number }
        }
        fn main() { let a = P { .number = 42; }; let b = P::new(43);
            println("{} {}", a.read(), b.read()); }'''
        self.assertEqual(self.run_text(text), "42 43\n")

    def test_aliases_builtin_and_instantiated_enum_owners(self):
        text = '''type Byte = u8;
        type Text = String;
        type Message = Event[String];
        enum[T] Event { Value[T], Empty, }
        fn main() {
            let number = Byte::try_from(42)?[panic];
            let message << Message::Value[Text::from("hello")];
            message? { Message::Value :> text -> println("{} {}", number, text@),
                       Message::Empty :> _ -> {}, };
        }'''
        self.assertEqual(self.run_text(text), "42 hello\n")

    def test_destructure_shadowing_reads_outer_values_before_declaring_new_names(self):
        text = '''fn main() { let a = 1; let b = 2;
            { let tuple[a, b] = tuple[b, a]; println("{} {}", a, b); };
            println("{} {}", a, b);
        }'''
        self.assertEqual(self.run_text(text), "2 1\n1 2\n")

    def test_resource_swap_snapshots_rhs_before_replacing_either_target(self):
        text = '''fn main() { let[mut] a << String::from("left");
            let[mut] b << String::from("right");
            tuple[a, b] << tuple[b, a];
            println("{} {}", a@, b@);
        }'''
        self.assertEqual(self.run_text(text), "right left\n")

    def test_ignored_resource_drops_once_after_destructure(self):
        text = '''struct Trace { number: i32, }
        impl Drop for Trace { fn drop(self: Self@[mut]) { println("drop {}", self.number); } }
        fn foo() -> tuple[Trace, Trace] {
            tuple[Trace { .number = 1; }, Trace { .number = 2; }]
        }
        fn main() { let tuple[kept, _] << foo(); println("kept {}", kept.number); }'''
        self.assertEqual(self.run_text(text), "drop 2\nkept 1\ndrop 1\n")

    def test_alias_pointer_permissions_survive_typed_tuple_binding(self):
        text = '''type Read = i32@;
        type Write = i32@[mut];
        fn read(p: Read) -> i32 { p# }
        fn main() { let[mut] value = 42; let pointer: Write = value@[mut];
            let tuple[p: Read, number: i32] = tuple[pointer, 0];
            println("{} {}", read(p), number);
        }'''
        self.assertEqual(self.run_text(text), "42 0\n")

    def test_destructure_weakens_each_pointer_without_container_covariance(self):
        text = '''fn pair(p: i32@[mut]) -> tuple[i32@[mut], i32] { tuple[p, 0] }
        fn main() { let[mut] value = 42;
            let tuple[p: i32@, number] = pair(value@[mut]);
            let[mut] existing: i32@ = value@;
            let[mut] other = 0;
            tuple[existing, other] = pair(value@[mut]);
            println("{} {} {}", p#, existing#, number + other);
        }'''
        self.assertEqual(self.run_text(text), "42 42 0\n")
        invalid = '''fn f(value: tuple[i32@[mut], i32]) {
            let readonly: tuple[i32@, i32] = value;
        }'''
        checker = Checker(Source(invalid), parse_source(invalid))
        self.assertEqual(checker.check()[0].code, "XE-TYPE-0001")

    def test_alias_and_destructure_cannot_wash_unsafe_return(self):
        text = '''type Read = i32@;
        type Pair = tuple[Read, i32];
        fn bad() -> Pair { let local = 42; tuple[local@, 0] }
        fn main() { let tuple[p: Read, _] = bad(); }'''
        checker = self.check(text)
        entry = next(e for e in checker.inferred_types.values() if e.get("name") == "p")
        self.assertTrue(entry["unsafe"])
        self.assertIn("XE-PTR-0001", [warning.code for warning in checker.warnings])

    def test_aliases_keep_resource_and_nonowning_pointer_rules(self):
        texts = (
            ('type Text = String; fn f(value: Text) { let copied: Text = value; }', "XE-OWN-0001"),
            ('type Pair = tuple[String, i32]; fn f(p: Pair@) { let tuple[text, n] << p#; }', "XE-MOVE-0002"),
        )
        for text, code in texts:
            with self.subTest(text=text):
                checker = Checker(Source(text), parse_source(text))
                errors = checker.check()
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, code, errors[0].render())

    def test_alias_to_nominal_recursive_pointer_is_finite(self):
        self.check('type Link = Node@; struct Node { next: Link, value: i32, } fn main() {}')


if __name__ == "__main__":
    unittest.main()
