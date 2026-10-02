"""单词位运算：整数宽度、优先级、求值顺序与检查/生成的一致性。"""
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast import Diagnostic, check_source, parse_source
from compiler.xe_ast.build import build_executable

CC = shutil.which("cc") or ""


class WordBitTests(unittest.TestCase):
    def test_ast_precedence_is_arithmetic_then_bits_then_comparison(self):
        node = parse_source("fn main() { 1 bitor 2 bitand 3 + 4 == 7; }")["items"][0]["body"]["statements"][0]["expression"]
        self.assertEqual(node["kind"], "ComparisonChain")
        bits = node["operands"][0]
        self.assertEqual(bits["operator"], "bitor")
        self.assertEqual(bits["right"]["operator"], "bitand")
        self.assertEqual(bits["right"]["right"]["operator"], "+")

    def test_bitnot_is_prefix_and_follows_postfix_chain(self):
        expression = parse_source("fn main() { bitnot values[0]#; }")["items"][0]["body"]["statements"][0]["expression"]
        self.assertEqual(expression["operator"], "bitnot")
        self.assertEqual(expression["operand"]["kind"], "Dereference")

    def test_bit_operators_require_integer_not_bool_float_or_pointer(self):
        for expression in ("bitnot true", "bitnot 1.0", "true bitand false",
                           "1.0 bitor 2.0", "pointer bitand pointer", "bitnot pointer",
                           "1.0 bitxor 2.0", "true bitshl 1", "1 bitshr false"):
            with self.subTest(expression=expression):
                errors = check_source("fn main() { let value = 1; let pointer = value@; " + expression + "; }")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-TYPE-0004")

    def test_bit_operators_preserve_normal_type_and_literal_checks(self):
        for text in ("let a: u8 = bitnot 256;", "let a: u8 = 256 bitand 1;",
                     "let a: u8 = 1 bitor -1;", "let a: u8 = 1; let b: i32 = 2; a bitand b;"):
            with self.subTest(text=text):
                self.assertTrue(check_source("fn main() { " + text + " }"))

    def test_words_are_reserved_and_traditional_symbols_not_added(self):
        for word in ("bitand", "bitor", "bitxor", "bitnot", "bitshl", "bitshr"):
            with self.subTest(word=word), self.assertRaises(Diagnostic):
                parse_source("fn main() { let " + word + " = 1; }")
        with self.assertRaises(Diagnostic):
            parse_source("fn main() { 1 & 2; }")

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_all_integer_widths_execute_with_exact_width(self):
        statements = []
        expected = []
        for type_ in ("i8", "u8", "i16", "u16", "i32", "u32", "i64", "u64", "isize", "usize"):
            statements.append(f'''{{ let zero: {type_} = 0; let mask: {type_} = 15;
                let all: {type_} = bitnot zero;
                println("{{}} {{}} {{}}", all, all bitand mask, zero bitor mask); }};''')
            width = struct.calcsize("P") * 8 if type_.endswith("size") else int(type_[1:])
            expected.append(f"{-1 if type_.startswith('i') else 2**width - 1} 15 15\n")
        self.assert_output("fn main() { " + "\n".join(statements) + " }", "".join(expected))

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_literals_context_prefix_chains_and_comparison_execute(self):
        self.assert_output('''fn main() {
            let byte: u8 = bitnot 0;
            let min: i8 = -128;
            let array: Array[u8, 1] = [15];
            let p = array[0]@;
            println("{} {} {} {} {} {}", byte, bitnot min, bitnot array[0],
                bitnot p#, bitnot bitnot byte, 4 bitor 2 bitand 3 + 4 == 6);
        }''', "255 127 240 240 255 true\n")

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_left_operand_is_snapshotted_before_right_effects(self):
        self.assert_output('''let[mut] value: i32 = 1;
            fn next() -> i32 { value = value + 1; value }
            fn main() { let a = value bitor next(); let b = value bitand next();
                println("{} {} {}", a, b, value); }''', "3 2 3\n")

    def test_static_shift_counts_and_left_overflow_are_errors(self):
        for declaration, code in (
                ("let a: u8 = 1 bitshl 8;", "XE-BIT-0001"),
                ("let a = 1 bitshr -1;", "XE-BIT-0001"),
                ("let a = 1 bitshr (30 + 2);", "XE-BIT-0001"),
                ("let a: u8 = 128 bitshl 1;", "XE-BIT-0002"),
                ("let a: i8 = -65 bitshl 1;", "XE-BIT-0002"),
                ("let a: i8 = 1 bitshl 7;", "XE-BIT-0002")):
            with self.subTest(declaration=declaration):
                errors = check_source("fn main() { " + declaration + " }")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, code, errors[0].render())
        self.assertEqual(check_source("fn main() { let a: i8 = -1 bitshl 7; let b: u8 = 128 bitshr 7; }"), [])

    def test_arithmetic_shift_and_bit_precedence(self):
        node = parse_source("fn main() { 1 bitor 2 bitxor 3 bitand 4 bitshl 5 + 6; }")["items"][0]["body"]["statements"][0]["expression"]
        self.assertEqual(node["operator"], "bitor")
        node = node["right"]
        self.assertEqual(node["operator"], "bitxor")
        node = node["right"]
        self.assertEqual(node["operator"], "bitand")
        node = node["right"]
        self.assertEqual(node["operator"], "bitshl")
        self.assertEqual(node["right"]["operator"], "+")

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_shift_every_width_and_signed_minimum_without_undefined_behavior(self):
        statements, expected = [], []
        for type_ in ("i8", "u8", "i16", "u16", "i32", "u32", "i64", "u64", "isize", "usize"):
            width = struct.calcsize("P") * 8 if type_.endswith("size") else int(type_[1:])
            signed = type_.startswith("i")
            number = -(1 << (width - 1)) if signed else 1 << (width - 1)
            max_count = width - 1
            statements.append(f'''{{ let value: {type_} = {number}; let count: u8 = {max_count};
                let one: {type_} = {"-1" if signed else "1"};
                println("{{}} {{}} {{}}", value bitshr count, one bitshl count, value bitshr 0); }};''')
            expected.append(f"{-1 if signed else 1} {number} {number}\n")
        self.assert_output("fn main() { " + "\n".join(statements) + " }", "".join(expected),
                           flags=("-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-no-pie"))

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_xor_and_independent_shift_count_types_and_negative_rounding(self):
        self.assert_output('''fn main() { let value: u8 = 5; let count: usize = 2;
            let negative: i8 = -3;
            println("{} {} {} {}", value bitxor 3, value bitshl count,
                negative bitshr 1, negative bitshr 0); }''', "6 20 -2 -3\n")

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_runtime_shift_failures_are_panics_not_c_undefined_behavior(self):
        for expression, message in (("value bitshl count", "left shift overflow"),
                                    ("value bitshr count", "shift count out of range")):
            count = 1 if "shl" in expression else -1
            with self.subTest(expression=expression):
                result = self.run_source(f'''fn shift(value: u8, count: i32) -> u8 {{ {expression} }}
                    fn main() {{ println("{{}}", shift(128, {count})); }}''')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_static_shift_initializers_agree_with_local_operations(self):
        self.assert_output('''let TOP: u8 = 1 bitshl 7;
            let LOW: u8 = TOP bitshr 7;
            let NEG: i8 = -3 bitshr 1;
            let XOR: u8 = TOP bitxor 1;
            fn main() { let count: i64 = 7;
                println("{} {} {} {} {}", TOP, LOW, NEG, XOR, TOP bitshr count); }''', "128 1 -2 129 1\n")

    def assert_output(self, text, expected, flags=()):
        result = self.run_source(text, flags)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ""))

    def run_source(self, text, flags=()):
        with tempfile.TemporaryDirectory(prefix="xe-word-bits-") as temporary:
            root = Path(temporary)
            source, output = root / "main.xe", root / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, cc=CC, extra_flags=("-pedantic-errors", *flags))
            return subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
