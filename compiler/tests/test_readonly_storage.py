"""模块只读 let 是静态对象，@ 保持同一地址且不会制造悬垂来源。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast import check_source, parse_source
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source

CC = shutil.which("cc") or ""


class ReadonlyStorageTests(unittest.TestCase):
    def test_readonly_addresses_are_static_and_cannot_be_writable(self):
        text = '''let NUMBER: i32 = 42;
            let LABEL: str = "label";
            fn number() -> i32@ { NUMBER@ }
            fn label() -> str@ { LABEL@ }
            fn main() { let pointer = number(); println("{}", pointer#); }'''
        checker = Checker(Source(text), parse_source(text))
        self.assertEqual(checker.check(), [])
        self.assertEqual(checker.warnings, [])
        self.assertFalse(checker.globals["NUMBER"].mutable)
        self.assertIn(checker.globals["NUMBER"].uid, checker.static_roots)
        for action in ("NUMBER@[mut];", "NUMBER = 0;", "let p = NUMBER@; p# = 0;"):
            with self.subTest(action=action):
                errors = check_source("let NUMBER: i32 = 42; fn main() { " + action + " }")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-MUT-0001")

    def test_static_scalar_expressions_and_cycles_have_friendly_errors(self):
        self.assertEqual(check_source('''let A: i32 = 1 + 2 * 3;
            let B: u8 = bitnot 0;
            let C: bool = not false and true;
            let[mut] D: i32 = A bitor 8;
            fn main() {}'''), [])
        for declarations in ("let A: i32 = A;", "let A: i32 = B; let B: i32 = A;",
                             "let A: i32 = 1 / 0;", "let A: i8 = 100 + 100;",
                             "let A: i8 = (100 + 100) - 100;",
                             "let A: i64 = -9223372036854775808 / -1;",
                             "let A: f32 = 3e38 * 2.0;",
                             "let A: Array[i32, 2] = [1, 2]; let P: i32@ = A[1 + 1]@;"):
            with self.subTest(declarations=declarations):
                errors = check_source(declarations + " fn main() {}")
                self.assertTrue(errors)
                self.assertIn(errors[0].code, {"XE-GLOBAL-0002", "XE-TYPE-0004"})
                self.assertTrue(errors[0].message)

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_readonly_storage_fields_arrays_aliases_functions_and_captures_execute(self):
        self.assert_output('''struct Point { x: i32, y: i32, } impl Copy for Point;
            let POINT: Point = Point { .x = 4; .y = 5; };
            let VALUES: Array[i32, 2] = [10, 20];
            let PAIR: tuple[i32, bool] = tuple[7, true];
            let LABEL: str = "hello";
            let VALUE: i32 = 40 + 2;
            let ALIAS: i32@ = VALUE@;
            let ELEMENT: i32@ = VALUES[1]@;
            fn address() -> i32@ { VALUE@ }
            fn add(x: i32) -> i32 { x + 1 }
            let HANDLER: fn(i32) -> i32 = add;
            fn main() {
                let capture << fn[VALUE@]() -> i32 { VALUE };
                println("{} {} {} {} {} {} {} {}", address()#, ALIAS#, POINT.x@#,
                    ELEMENT#, PAIR.0@#, LABEL@#, capture(), HANDLER(3));
            }''', "42 42 4 20 7 hello 42 4\n")

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_static_folded_bits_signed_division_and_aggregate_width_execute(self):
        self.assert_output('''let BYTE: u8 = bitnot 0;
            let NEGATIVE: i32 = -7 / 3;
            let REMAINDER: i32 = -7 % 3;
            let WIDE: u64 = bitnot 0;
            let MASK: u8 = 1 bitor 4 bitand 6;
            let READY: bool = not false;
            let BYTES: Array[u8, 2] = [bitnot 0, 2 bitor 4];
            let[mut] OTHER: u8 = BYTE bitand 15;
            fn main() { println("{} {} {} {} {} {} {} {} {}", BYTE, NEGATIVE,
                REMAINDER, WIDE, MASK, READY, BYTES[0], BYTES[1], OTHER); }''',
            "255 -2 -1 18446744073709551615 5 true 255 6 15\n")

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_static_float_rounds_each_step_and_min_remainder_agrees_with_runtime(self):
        self.assert_output('''let ROUND: f32 = 16777216.0 + 1.0 - 16777216.0;
            let REM: i32 = -2147483648 % -1;
            fn main() { let n: f32 = 16777216.0;
                let min: i32 = -2147483648;
                println("{} {} {} {}", ROUND, n + 1.0 - n, REM, min % -1); }''', "0 0 0 0\n")

    @unittest.skipUnless(CC, "运行验收需要 C 编译器")
    def test_cross_module_addresses_and_reexports_execute(self):
        with tempfile.TemporaryDirectory(prefix="xe-readonly-modules-") as temporary:
            root = Path(temporary)
            (root / "xe.toml").write_text('[package]\nname = "readonly"\n', encoding="utf-8")
            (root / "src").mkdir()
            (root / "src/settings.xe").write_text("pub let LIMIT: i32 = 42; pub fn limit() -> i32@ { LIMIT@ }", encoding="utf-8")
            (root / "src/api.xe").write_text("pub use crate::settings::LIMIT as EXPORTED;", encoding="utf-8")
            source = root / "src/main.xe"
            source.write_text('''use crate::settings::{LIMIT, limit}; use crate::api::EXPORTED;
                let POINTER: i32@ = EXPORTED@;
                fn main() { println("{} {} {} {}", LIMIT@#, EXPORTED@#, POINTER#, limit()#); }''', encoding="utf-8")
            self.run_file(source, root / "program", "42 42 42 42\n")

    def assert_output(self, text, expected):
        with tempfile.TemporaryDirectory(prefix="xe-readonly-storage-") as temporary:
            root = Path(temporary)
            source = root / "main.xe"
            source.write_text(text, encoding="utf-8")
            self.run_file(source, root / "program", expected)

    def run_file(self, source, output, expected):
        warnings = []
        build_executable(source, output, cc=CC, extra_flags=("-pedantic-errors",), warnings=warnings)
        self.assertEqual(warnings, [])
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ""))
