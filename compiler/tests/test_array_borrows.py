"""数组元素指针必须追踪数组存储风险；纯数值不能抹掉地址来源。

临时数组可以迭代，逃逸指针应标记 unsafe 并警告，但仍可编译。
只执行存储仍有效的正例并配合 ASan/UBSan，不运行已知悬垂程序。
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.source import Source

CC = shutil.which("cc")


class ArrayBorrowTests(unittest.TestCase):
    def assert_ok(self, text):
        errors = check_source(text)
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))

    def assert_error(self, text, code):
        errors = check_source(text)
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def pointer_check(self, text):
        checker = Checker(Source(text, "array.xe"), parse_source(text, "array.xe"))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        return checker

    def assert_pointer_warning(self, text, code):
        checker = self.pointer_check(text)
        self.assertIn(code, [warning.code for warning in checker.warnings])
        self.assertTrue(all(warning.severity == "warning" for warning in checker.warnings))

    def test_local_array_element_address_can_return_with_warning(self):
        self.assert_pointer_warning("fn bad() -> i32@ { let values = [1, 2]; values[0]@ }", "XE-PTR-0001")
        self.assert_pointer_warning('fn bad() -> String@ { let words << [String::from("owned")]; words[0]@ }', "XE-PTR-0001")
        self.assert_pointer_warning("fn bad() -> i32@[mut] { let[mut] values = [1, 2]; values[0]@[mut] }", "XE-PTR-0001")

    def test_pointer_parameter_array_element_address_may_be_returned(self):
        self.assert_ok("fn first(values: Array[i32, 2]@) -> i32@ { values[0]@ }")

    def test_mutable_array_index_can_be_written_but_shared_array_cannot(self):
        self.assert_ok("fn f() { let[mut] values = [1, 2]; values[0] = 3; let p: i32@[mut] = values[1]@[mut]; p# = 4; }")
        self.assert_error("fn f() { let values = [1, 2]; values[0] = 3; }", "XE-MUT-0001")
        self.assert_error("fn f(values: Array[i32, 2]@) { values[0] = 3; }", "XE-MUT-0001")
        self.assert_ok("fn f(values: Array[i32, 2]@[mut]) { values[0] = 3; }")

    def test_array_element_pointer_can_alias_owner_during_mutation(self):
        for text in (
                'fn f() { let[mut] values = [1, 2]; let p: i32@ = values[0]@; values[1] = 3; println("{}", p#); }',
                'fn f() { let[mut] values = [1, 2]; let p: i32@ = values[0]@; println("{}", p#); values[1] = 3; }'):
            with self.subTest(text=text):
                self.assertEqual(self.pointer_check(text).warnings, [])

    def test_temporary_array_iteration_return_warns_but_local_access_does_not(self):
        self.assert_pointer_warning('fn bad() -> String@ { for text: String@ in [String::from("owned")] { return text; } panic("empty") }', "XE-PTR-0001")
        self.assert_pointer_warning('fn bad() -> i32@ { for value: i32@ in [1, 2] { return value; } panic("empty") }', "XE-PTR-0001")
        self.assertEqual(self.pointer_check('fn f() { for value: i32@ in [1, 2] { println("{}", value#); } }').warnings, [])

    def test_temporary_iterator_pointer_used_after_loop_reports_risk(self):
        # 初始化一个有效默认地址：for 不保证运行，不能把未初始化错误
        # 与循环临时存储失效混淆。此例只检查诊断，不运行悬垂地址。
        self.assert_pointer_warning('fn f() { let initial = 0; let[mut] saved: i32@ = initial@; for value: i32@ in [1, 2] { saved = value; } println("{}", saved#); }', "XE-PTR-0001")

    def test_temporary_resource_views_escape_with_warning(self):
        self.assert_pointer_warning('fn f() -> u8@ { String::from("owned").as_str().data() }', "XE-PTR-0003")
        self.assert_pointer_warning('fn f() -> Slice[i32] { [1, 2].slice(..) }', "XE-PTR-0003")
        self.assert_pointer_warning('struct S { n: i32, } impl S { fn get(self: Self@) -> i32@ { self.n@ } } fn bad() -> i32@ { S { .n = 1; }.get() }', "XE-PTR-0003")

    def test_array_view_replacement_reports_expired_string_owner(self):
        self.assert_pointer_warning('fn main() { let[mut] views: Array[str, 1] = ["ok"]; { let s << String::from("bad"); views[0] = s.as_str(); }; println("{}", views[0]); }', "XE-PTR-0001")

    def test_copying_str_element_keeps_data_source_not_descriptor_storage(self):
        self.assert_ok('fn view() -> str { let words = ["hi"]; words[0] }')
        self.assert_pointer_warning('fn bad() -> str { let s << String::from("bad"); let views = [s.as_str()]; views[0] }', "XE-PTR-0001")
        self.assert_ok('fn view(source: str) -> str { let words = [source]; words[0] }')
        self.assert_pointer_warning('fn bad() -> str@ { let words = ["hi"]; words[0]@ }', "XE-PTR-0001")

    @unittest.skipUnless(CC, "需要系统 C 编译器")
    def test_array_mutation_resource_replacement_and_temporary_iteration_run(self):
        text = '''fn view() -> str { let words = ["hi"]; words[0] }
        fn main() {
            println("{}", view());
            let[mut] values = [1, 2];
            values[0] = 3;
            let p: i32@[mut] = values[1]@[mut];
            p# = 4;
            for value in values { println("{}", value#); }
            let[mut] words << [String::from("old"), String::from("kept")];
            words[0] << String::from("new");
            for word in words { println("{}", word); }
            for word in [String::from("temporary")] { println("{}", word); }
        }'''
        with tempfile.TemporaryDirectory() as directory:
            source, program = Path(directory) / "test.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, program, cc=CC,
                             extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "hi\n3\n4\nnew\nkept\ntemporary\n")


if __name__ == "__main__":
    unittest.main()
