"""Normal continuation states differ between eager and short-circuit operands."""
import unittest

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source


class CompletionPathTests(LegacyTestCase):
    def checked(self, source):
        checker = Checker(Source(source), parse_source(source))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        return checker

    def rejected(self, source, code):
        errors = check_source(source)
        self.assertTrue(errors, source)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def assert_boolean_expression_type(self, expression, expected):
        checker = self.checked("fn inspect(ready: bool, first: i32, second: i32) -> bool { "
                               + expression + " } fn main() {}")
        tail = checker.functions["inspect"].node["body"]["tail"]
        self.assertEqual(checker.inferred_types[id(tail)]["type"], expected)

    def test_required_operations_end_the_enclosing_path(self):
        for type_, expression in (
            ("i32", 'panic("unused") + 1'),
            ("i32", '1 + panic("unused")'),
            ("i32", '1 - panic("unused")'),
            ("i32", '1 * panic("unused")'),
            ("i32", '1 / panic("unused")'),
            ("i32", '1 % panic("unused")'),
            ("i32", '-panic("unused")'),
            ("i32", '+panic("unused")'),
            ("bool", 'not panic("unused")'),
            ("i32", 'bitnot panic("unused")'),
            ("i32", 'panic("unused") bitshl 1'),
            ("i32", '1 bitshr panic("unused")'),
            ("i32", '1 bitand panic("unused")'),
            ("i32", 'panic("unused") bitor 1'),
            ("i32", '1 bitxor panic("unused")'),
            ("Range[i32]", 'panic("unused")..1'),
            ("Range[i32]", '1..panic("unused")'),
            ("i32", 'panic("unused") |> value: i32 -> value'),
        ):
            with self.subTest(expression=expression):
                checker = self.checked("fn inspect() -> i32 { let value: " + type_ + " = "
                                       + expression + "; unreachable_after; } fn main() {}")
                initializer = checker.functions["inspect"].node["body"]["statements"][0]["value"]
                self.assertEqual(checker.inferred_types[id(initializer)]["type"], "Never")

    def test_untyped_never_does_not_choose_a_unary_or_shift_operand_type(self):
        for expression, code in (
            ('-panic("unused")', "XE-TYPE-0001"),
            ('+panic("unused")', "XE-TYPE-0001"),
            ('bitnot panic("unused")', "XE-TYPE-0004"),
            ('panic("unused") bitshl 1', "XE-TYPE-0004"),
            ('panic("unused") bitshr 1', "XE-TYPE-0004"),
        ):
            with self.subTest(expression=expression):
                self.rejected("fn main() { " + expression + "; }", code)

    def test_required_operations_still_check_later_names_and_operand_domains(self):
        for type_, expression, code in (
            ("i32", 'panic("unused") + missing', "XE-NAME-0001"),
            ("i32", 'panic("unused") bitshl missing', "XE-NAME-0001"),
            ("Range[i32]", 'panic("unused")..missing', "XE-NAME-0001"),
            ("i32", 'panic("unused") + "bad"', "XE-TYPE-0001"),
            ("i32", 'panic("unused") bitshl "bad"', "XE-TYPE-0004"),
            ("Range[i32]", 'panic("unused").."bad"', "XE-TYPE-0001"),
        ):
            with self.subTest(expression=expression):
                self.rejected("fn main() { let value: " + type_ + " = " + expression + "; }", code)

    def test_first_and_second_comparison_operands_are_required(self):
        for expression in (
            'panic("unused") < first < second',
            'first < panic("unused") < second',
            'panic("unused") < first',
            'first < panic("unused")',
        ):
            with self.subTest(expression=expression):
                self.assert_boolean_expression_type(expression, "Never")

    def test_later_comparison_never_operands_preserve_a_boolean_skip_path(self):
        for expression in (
            'first < second < panic("unused")',
            'first < second < 3 < panic("unused")',
            'first < second < panic("unused") < 4',
        ):
            with self.subTest(expression=expression):
                self.assert_boolean_expression_type(expression, "bool")

    def test_first_two_comparison_operands_initialize_on_every_continuing_path(self):
        for expression in (
            "{ result = 42; first } < second < 3",
            "first < { result = 42; second } < 3",
        ):
            with self.subTest(expression=expression):
                self.checked("fn inspect(first: i32, second: i32) -> i32 { let result: i32; "
                             + expression + "; result } fn main() {}")

    def test_later_comparison_initialization_is_not_guaranteed(self):
        for expression in (
            "first < second < { result = 42; 3 }",
            "first < second < 3 < { result = 42; 4 }",
            "first < second < { result = 42; return 7; }",
        ):
            with self.subTest(expression=expression):
                self.rejected("fn inspect(first: i32, second: i32) -> i32 { let result: i32; "
                              + expression + "; result } fn main() {}", "XE-INIT-0001")

    def test_returning_comparison_operands_do_not_move_the_skipped_path_resource(self):
        for expression in (
            "first < second < { take(text); return 7; }",
            "first < second < 3 < { take(text); return 7; }",
            "first < second < { if ready { take(text); return 7; }; 3 }",
        ):
            with self.subTest(expression=expression):
                self.checked('fn take(value: String) {} fn inspect(first: i32, second: i32, ready: bool)'
                             ' -> usize { let text << String::from("owned"); '
                             + expression + "; text.len() } fn main() {}")

    def test_comparison_moves_on_normal_prefixes_still_prevent_later_use(self):
        for expression in (
            "first < second < take(text)",
            "first < second < 3 < take(text)",
            # Moving the third operand can be followed by a false comparison;
            # a return in the fourth operand does not remove that normal path.
            "first < second < take(text) < { return 7; }",
        ):
            with self.subTest(expression=expression):
                self.rejected('fn take(value: String) -> i32 { 3 } '
                              'fn inspect(first: i32, second: i32) -> usize { '
                              'let text << String::from("owned"); '
                              + expression + "; text.len() } fn main() {}", "XE-MOVE-0001")

    def test_required_comparison_return_does_not_move_a_surviving_if_path(self):
        for expression in (
            "{ take(text); return 7; } < first < second",
            "first < { take(text); return 7; } < second",
        ):
            with self.subTest(expression=expression):
                self.checked('fn take(value: String) {} '
                             'fn inspect(ready: bool, first: i32, second: i32) -> usize { '
                             'let text << String::from("owned"); if ready { '
                             + expression + "; } else {}; text.len() } fn main() {}")

    def test_comparison_static_checks_continue_after_a_never_operand(self):
        for expression, code in (
            ('panic("unused") < first < missing', "XE-NAME-0001"),
            ('first < panic("unused") < missing', "XE-NAME-0001"),
            ('first < second < panic("unused") < missing', "XE-NAME-0001"),
            ('panic("unused") < first < "bad"', "XE-TYPE-0001"),
            ('first < panic("unused") < "bad"', "XE-TYPE-0001"),
            ('first < second < panic("unused") < "bad"', "XE-TYPE-0001"),
        ):
            with self.subTest(expression=expression):
                self.rejected("fn inspect(first: i32, second: i32) -> bool { "
                              + expression + " } fn main() {}", code)

    def test_short_circuit_noncompletion_depends_on_which_operand_is_required(self):
        for operator in ("and", "or"):
            with self.subTest(operator=operator):
                self.assert_boolean_expression_type('panic("unused") ' + operator + " ready", "Never")
                self.assert_boolean_expression_type("ready " + operator + ' panic("unused")', "bool")

    def test_short_circuit_left_initialization_reaches_following_code(self):
        for operator in ("and", "or"):
            with self.subTest(operator=operator):
                self.checked("fn inspect(ready: bool) -> i32 { let result: i32; "
                             "{ result = 42; ready } " + operator + " ready; result } fn main() {}")

    def test_short_circuit_right_initialization_is_not_guaranteed(self):
        for operator in ("and", "or"):
            with self.subTest(operator=operator):
                self.rejected("fn inspect(ready: bool) -> i32 { let result: i32; ready "
                              + operator + " { result = 42; true }; result } fn main() {}", "XE-INIT-0001")

    def test_short_circuit_returning_right_operand_preserves_the_skipped_resource(self):
        for operator in ("and", "or"):
            with self.subTest(operator=operator):
                self.checked('fn take(value: String) {} fn inspect(ready: bool) -> usize { '
                             'let text << String::from("owned"); ready '
                             + operator + " { take(text); return 7; }; text.len() } fn main() {}")

    def test_short_circuit_normal_right_operand_can_move_the_resource(self):
        for operator in ("and", "or"):
            with self.subTest(operator=operator):
                self.rejected('fn take(value: String) -> bool { true } '
                              'fn inspect(ready: bool) -> usize { let text << String::from("owned"); ready '
                              + operator + " take(text); text.len() } fn main() {}", "XE-MOVE-0001")

    def test_short_circuit_still_checks_right_names_and_types(self):
        for expression, code in (
            ("false and missing", "XE-NAME-0001"),
            ("true or missing", "XE-NAME-0001"),
            ('panic("unused") and missing', "XE-NAME-0001"),
            ('panic("unused") or missing', "XE-NAME-0001"),
            ('false and "bad"', "XE-TYPE-0001"),
            ('true or "bad"', "XE-TYPE-0001"),
            ('panic("unused") and "bad"', "XE-TYPE-0001"),
            ('panic("unused") or "bad"', "XE-TYPE-0001"),
        ):
            with self.subTest(expression=expression):
                self.rejected("fn main() { " + expression + "; }", code)


if __name__ == "__main__":
    unittest.main()
