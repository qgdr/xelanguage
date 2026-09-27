"""泛型跨阶段的边界回归，不以“生成了一段 C”当作实现完成。

这里集中测试类型附件、具体所有权、缓存和风险传播之间的交互。
未使用的模板不生成机器代码；已实例化的程序必须重新检查并实际执行。
"""
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


class GenericIntegrationTests(unittest.TestCase):
    def check(self, text):
        checker = Checker(Source(text), parse_source(text))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(e.render() for e in errors))
        return checker

    def run_text(self, text):
        if not CC:
            self.skipTest("需要系统 C 编译器")
        with tempfile.TemporaryDirectory() as directory:
            source, program = Path(directory) / "source.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, program, cc=CC,
                             extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
            completed = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed.stdout

    def test_instance_cache_does_not_move_unsafe_from_one_address_to_another(self):
        text = '''fn[T] identity(value: T) -> T { value }
        fn bad() -> i32@ { let local = 1; local@ }
        fn main() { let dangerous = identity(bad()); let local = 2; let ordinary = identity(local@); }'''
        # 不运行：bad 返回悬垂地址。只比较数据流信息和实例缓存。
        checker = self.check(text)
        entries = {e.get("name"): e for e in checker.inferred_types.values() if "name" in e}
        self.assertTrue(entries["dangerous"]["unsafe"])
        self.assertFalse(entries["ordinary"]["unsafe"])
        self.assertEqual(len(checker.generic_instances), 1)

    def test_source_ast_is_not_rewritten_into_hidden_instances(self):
        text = "fn[T] identity(x: T) -> T { x } fn main() { let x = identity(1); }"
        tree = parse_source(text)
        import copy
        before = copy.deepcopy(tree)
        checker = Checker(Source(text), tree)
        self.assertEqual(checker.check(), [])
        self.assertEqual(tree, before)
        self.assertIn("identity", checker.functions)
        self.assertTrue(checker.generic_instances)

    def test_explicit_unsafe_type_argument_survives_function_value_without_poisoning_cache(self):
        text = '''fn[T] identity(value: T) -> T { value }
        fn main() {
            let local = 42;
            let risky_function = identity[i32@[unsafe]];
            let dangerous = risky_function(local@);
            let direct = identity[i32@[unsafe]](local@);
            let ordinary = identity(local@);
            dangerous#;
        }'''
        checker = self.check(text)
        entries = {e.get("name"): e for e in checker.inferred_types.values() if "name" in e}
        self.assertTrue(entries["dangerous"]["unsafe"])
        self.assertTrue(entries["direct"]["unsafe"])
        self.assertFalse(entries["ordinary"]["unsafe"])
        self.assertEqual(len(checker.generic_instances), 1)
        self.assertIn("XE-PTR-0003", [warning.code for warning in checker.warnings])

    def test_pointer_dereference_does_not_acquire_generic_resource_ownership(self):
        text = 'fn[T] copy_from(p: T@) -> T { p# } fn main() { let s << String::from("owned"); let copy << copy_from(s@); }'
        checker = Checker(Source(text), parse_source(text))
        errors = checker.check()
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-MOVE-0002")

    def test_value_name_equal_to_type_parameter_is_not_blindly_substituted(self):
        text = '''fn[T] inspect(T: usize, value: T) -> T {
            let values = [10, 20]; println("{}", values[T]); value
        }
        fn main() { let n = inspect(1, 42); println("{}", n); }'''
        self.assertEqual(self.run_text(text), "20\n42\n")

    def test_type_parameters_do_not_capture_names_in_other_declarations(self):
        text = '''struct T { number: i32, }
        impl Copy for T;
        struct Envelope { item: T, }
        impl Copy for Envelope;
        fn[T] identity(value: T) -> T { value }
        fn main() {
            let envelope = Envelope { .item = T { .number = 42; }; };
            let text << identity(String::from("hello"));
            println("{} {}", envelope.item.number, text@);
        }'''
        self.assertEqual(self.run_text(text), "42 hello\n")

    def test_explicit_result_and_function_type_arguments_execute(self):
        text = '''fn[T] identity(value: T) -> T { value }
        fn plus_one(value: i32) -> i32 { value + 1 }
        fn main() {
            let result: i32? = identity[i32?](Maybe::Yes[41]);
            let callback: fn(i32) -> i32 = identity[fn(i32) -> i32](plus_one);
            println("{}", callback(result?[panic]));
        }'''
        self.assertEqual(self.run_text(text), "42\n")

    def test_explicit_readonly_type_argument_accepts_writable_pointer(self):
        text = '''fn[T] identity(value: T) -> T { value }
        fn main() { let[mut] value = 42; let p: i32@ = identity[i32@](value@[mut]); println("{}", p#); }'''
        self.assertEqual(self.run_text(text), "42\n")

    def test_concrete_implementation_is_not_used_for_wrong_type_argument(self):
        text = '''struct[T] Holder { value: T, }
        impl Holder[i32] { fn read(self: Self@) -> i32 { self.value } }
        fn main() { let h << Holder[String] { String::from("wrong") >> .value; }; h.read(); }'''
        checker = Checker(Source(text), parse_source(text))
        errors = checker.check()
        self.assertTrue(errors)
        self.assertIn(errors[0].code, {"XE-TYPE-0001", "XE-GENERIC-0001", "XE-SEM-0001"})

    def test_generic_struct_remains_move_only_without_explicit_copy(self):
        text = "struct[T] Holder { value: T, } fn main() { let h = Holder[i32] { 42 >> .value; }; }"
        checker = Checker(Source(text), parse_source(text))
        self.assertEqual(checker.check()[0].code, "XE-OWN-0001")

    def test_explicit_function_instances_are_values_and_pipe_targets(self):
        text = '''fn[T] identity(value: T) -> T { value }
        fn main() {
            let function: fn(i32) -> i32 = identity[i32];
            let a = 42 |> identity[i32];
            let maybe: i32? = Maybe::Yes[42];
            let b = maybe? 1> identity[i32] 2> _ -> 0;
            println("{} {} {}", function(42), a, b);
        }'''
        self.assertEqual(self.run_text(text), "42 42 42\n")

    def test_recursive_instance_is_cached_instead_of_expanding_forever(self):
        text = '''fn[T] repeat(value: T, count: i32) -> T {
            if count == 0 { value } else { repeat(value, count - 1) }
        }
        fn main() { println("{}", repeat(42, 5)); }'''
        self.assertEqual(self.run_text(text), "42\n")

    def test_type_qualified_operations_use_concrete_type_parameter(self):
        text = '''fn[T] checked(value: i64) -> T?[ConversionError] {
            T::try_from(value)
        }
        fn main() { let byte = checked[u8](42)?[panic]; println("{}", byte); }'''
        self.assertEqual(self.run_text(text), "42\n")

    def test_capture_free_functions_inside_instances_keep_distinct_signatures(self):
        text = '''fn[T] function() -> fn(T) -> T {
            fn(value: T) -> T { value }
        }
        fn main() {
            let number = function[i32]();
            let text = function[String]();
            println("{} {}", number(42), text(String::from("hello")));
        }'''
        self.assertEqual(self.run_text(text), "42 hello\n")

    def test_concrete_instances_keep_evaluation_order_and_cleanup_on_return(self):
        text = '''fn[T] consume(first: T, second: T) -> T { first }
        fn finish() -> String {
            consume(String::from("discard"), { return String::from("kept"); })
        }
        fn main() { println("{}", finish()); }'''
        self.assertEqual(self.run_text(text), "kept\n")


if __name__ == "__main__":
    unittest.main()
