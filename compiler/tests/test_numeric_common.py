"""共同类型推断不能绕过整数字面量的目标类型边界。"""
import unittest

from compiler.xe_ast.semantic import check_source


class NumericCommonTests(unittest.TestCase):
    def assert_range_error(self, expression):
        source = f"fn main() {{ let byte: u8 = 1; let result = {expression}; }}"
        errors = check_source(source)
        self.assertTrue(errors, source)
        self.assertEqual(errors[0].code, "XE-TYPE-0004", errors[0].render())

    def test_array_element_range(self):
        self.assert_range_error("[300, byte]")
        self.assert_range_error("[byte, 300]")

    def test_arithmetic_operand_range(self):
        self.assert_range_error("300 + byte")
        self.assert_range_error("byte + 300")

    def test_branch_result_range(self):
        self.assert_range_error("if true { 300 } else { byte }")
        self.assert_range_error("if true { byte } else { 300 }")
        self.assert_range_error("if true { 300 + 0 } else { byte }")
        self.assert_range_error("[if true { 300 } else { 400 }, byte]")

    def test_default_i32_range(self):
        for expression in ("3000000000 + 1", "[3000000000]", "tuple[3000000000]",
                           "3000000000 == 3000000000"):
            with self.subTest(expression=expression):
                errors = check_source(f"fn main() {{ {expression}; }}")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-TYPE-0004", errors[0].render())

    def test_signed_minimum_is_checked_after_negation(self):
        self.assertEqual(check_source("fn main() { let small: i8 = -128; }"), [])
        self.assertEqual(check_source("fn main() { let small: i8 = if true { -128 } else { -1 }; }"), [])

    def test_in_range_literal_is_accepted(self):
        self.assertEqual(check_source("fn main() { let byte: u8 = 1; let values = [255, byte]; }"), [])


if __name__ == "__main__":
    unittest.main()
