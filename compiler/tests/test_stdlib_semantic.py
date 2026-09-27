"""标准 IO 使用既有类型和所有权规则；公开签名只有一个登记入口。"""
import copy
import unittest

from compiler.xe_ast import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source
from compiler.xe_ast.stdlib_io import READLINE_RESULT, io_function, normalize_io_name
from compiler.xe_ast.typesys import IO_ERROR, NONE, STRING, callable_type


class StandardIoSemanticTests(unittest.TestCase):
    def check(self, text):
        tree = parse_source(text, "io.xe")
        original = copy.deepcopy(tree)
        checker = Checker(Source(text, "io.xe"), tree)
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        self.assertEqual(tree, original, "标准库类型注记不能修改源码 AST")
        return checker

    def assert_error(self, text, code):
        errors = check_source(text, "io.xe")
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())
        self.assertIn("io.xe:", errors[0].render())

    def test_prelude_and_qualified_paths_share_registry(self):
        for name in ("print", "println", "eprintln", "readline"):
            with self.subTest(name=name):
                self.assertIs(io_function(name), io_function("std::io::" + name))
                self.assertEqual(normalize_io_name("std::io::" + name), name)
        self.assertIsNone(io_function("std::io::read_line"))
        self.assertEqual(normalize_io_name("other::readline"), "other::readline")

    def test_readline_result_is_error_then_optional_owned_string(self):
        self.assertEqual(READLINE_RESULT.args[1], IO_ERROR)
        self.assertEqual(READLINE_RESULT.args[0].args, (STRING, NONE))
        checker = self.check('''fn read() -> String??[io::Error] { readline() }
            fn qualified() -> String??[std::io::Error] { std::io::readline() }''')
        self.assertEqual(checker.functions["read"].result, READLINE_RESULT)
        self.assertEqual(checker.functions["qualified"].result, READLINE_RESULT)

    def test_fixed_signature_readline_can_be_a_function_value(self):
        checker = self.check('''type Reader = fn() -> String??[std::io::Error];
            fn f() -> String??[io::Error] {
                let read: Reader = std::io::readline; read()
            }
            fn g() -> String??[io::Error] { let read = readline; read() }''')
        self.assertEqual(checker.alias_types["Reader"], callable_type([], READLINE_RESULT))

    def test_formatted_output_uses_normal_explicit_pointer_arguments(self):
        self.check('''fn main() {
            let text << String::from("hello");
            print("{}", text@); std::io::println("{} {}", text@, 42);
            std::io::eprintln("{}", text@); println("{}", text@);
        }''')

    def test_formatted_output_does_not_implicitly_borrow_resources(self):
        for name in ("print", "println", "std::io::eprintln"):
            with self.subTest(name=name):
                self.assert_error('fn f() {let text << String::from("hello"); '
                                  + name + '("{}", text); println("{}", text@);}',
                                  "XE-MOVE-0001")

    def test_read_result_cannot_be_copied(self):
        self.assert_error('fn f() {let result = readline();}', "XE-OWN-0001")

    def test_read_result_cannot_be_silently_unwrapped_or_lose_error(self):
        for expected in ("String", "String?", "String?[io::Error]"):
            with self.subTest(expected=expected):
                self.assert_error(f'fn f() {{let result: {expected} << readline();}}',
                                  "XE-TYPE-0001")

    def test_readline_rejects_arguments(self):
        self.assert_error('fn f() {let result << readline(1);}', "XE-CALL-0001")
        self.assert_error('fn f() {let result << std::io::readline("prompt");}',
                          "XE-CALL-0001")

    def test_format_literal_and_argument_count_are_checked_for_all_paths(self):
        for name in ("print", "std::io::println", "std::io::eprintln"):
            with self.subTest(name=name):
                self.assert_error('fn f() {' + name + '();}', "XE-CALL-0001")
                self.assert_error('fn f() {' + name + '("{}", 1, 2);}', "XE-FORMAT-0001")
                self.assert_error('fn f(format: str) {' + name + '(format);}', "XE-SEM-0001")

    def test_formatted_callable_values_are_an_explicit_stage0_limit(self):
        for name in ("print", "std::io::println", "eprintln"):
            with self.subTest(name=name):
                self.assert_error('fn f() {let output = ' + name + ';}', "XE-SEM-0001")
        self.check('''fn main() {
            let output: fn(str) -> Unit = fn(text: str) { println("{}", text); };
            output("hello");
        }''')

    def test_user_function_and_local_function_shadow_prelude(self):
        self.check('''fn readline() -> i32 {42}
            fn main() {let number = readline();
                let actual << std::io::readline();}''')
        self.check('''fn main() {let readline = fn() -> i32 {42};
            let number = readline(); let actual << std::io::readline();}''')

    def test_unknown_names_do_not_get_arbitrary_namespace_stripping(self):
        self.assert_error('fn f() {std::io::read_line();}', "XE-NAME-0001")
        self.assert_error('fn f() {other::println("hello");}', "XE-NAME-0001")

    def test_standard_error_path_is_a_transparent_alias(self):
        checker = self.check('type Error = std::io::Error; fn f(error: Error)->io::Error{error}')
        self.assertEqual(checker.alias_types["Error"], IO_ERROR)
        self.assert_error('type Error = std::io::Error[i32];', "XE-GENERIC-0001")


if __name__ == "__main__":
    unittest.main()
