"""资源的按值 self 不得由指针提供；Copy 值保留既有复制规则。"""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import check_source


CC = shutil.which("cc")


class OwnedMethodReceiverTests(unittest.TestCase):
    def assert_pointer_call_rejected(self, source):
        errors = check_source(source, "receiver.xe")
        self.assertTrue(errors, source)
        self.assertEqual(errors[0].code, "XE-MOVE-0002", errors[0].render())
        self.assertIn("不能通过", errors[0].message)
        self.assertIn("指针调用", errors[0].message)

    def test_copy_self_still_copies_through_pointer(self):
        source = """
            struct Point { n: i32, }
            impl Copy for Point;
            impl Point { fn take(self: Self) -> i32 { self.n } }
            fn main() {
                let point = Point { .n = 7; };
                let pointer: Point@ = point@;
                let value = pointer.take();
            }
        """
        self.assertEqual(check_source(source, "receiver.xe"), [])

    def test_resource_self_cannot_be_called_through_pointer(self):
        self.assert_pointer_call_rejected("""
            struct Boxed { value: String, }
            impl Boxed { fn take(self: Self) -> String { self.value } }
            fn main() {
                let boxed << Boxed { .value << String::from("hello"); };
                let pointer: Boxed@ = boxed@;
                let text << pointer.take();
            }
        """)

    def test_generic_copy_value_method_still_copies_through_pointer(self):
        source = """
            struct Point { n: i32, }
            impl Copy for Point;
            impl Point { fn[T] choose(self: Self, other: T) -> T { other } }
            fn main() {
                let point = Point { .n = 7; };
                let pointer: Point@ = point@;
                let value = pointer.choose[i32](42);
            }
        """
        self.assertEqual(check_source(source, "receiver.xe"), [])

    def test_expect_cannot_consume_resource_result_through_pointer(self):
        self.assert_pointer_call_rejected("""
            fn main() {
                let result: String? << Maybe::Yes[String::from("hello")];
                let pointer: String?@ = result@;
                let value << pointer.expect("success");
            }
        """)

    def test_expect_copy_result_still_copies_through_pointer(self):
        source = """
            fn main() {
                let result: i32? = Maybe::Yes[42];
                let pointer: i32?@ = result@;
                let value = pointer.expect("success");
            }
        """
        self.assertEqual(check_source(source, "receiver.xe"), [])

    @unittest.skipUnless(CC, "运行后端验收需要系统 C 编译器")
    def test_explicit_copy_and_pointer_receivers_still_work(self):
        source = """
            struct Point { n: i32, }
            impl Copy for Point;
            impl Point {
                fn take(self: Self) -> i32 { self.n }
                fn peek(self: Self@) -> i32 { self.n }
                fn advance(self: Self@[mut]) { self.n = self.n + 1; }
            }
            fn main() {
                let[mut] point = Point { .n = 7; };
                let pointer: Point@[mut] = point@[mut];
                println("{} {}", pointer.n, pointer.peek());
                pointer.advance();
                println("{} {} {}", pointer.take(), (pointer#).take(), point.take());
            }
        """
        self.assertEqual(check_source(source, "receiver.xe"), [])
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "receiver.xe"
            output_path = Path(directory) / "receiver"
            source_path.write_text(source, encoding="utf-8")
            build_executable(source_path, output_path, cc=CC)
            result = subprocess.run([str(output_path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "7 7\n8 8 8\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
