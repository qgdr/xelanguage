"""Runtime failures identify the Xe operation, not its last evaluated operand.

These are native executions, including an imported module and a closure. No
pointer-risk example is executed: warnings deliberately do not prove safety.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.source import Source

CC = shutil.which("cc") or ""


@unittest.skipUnless(CC, "运行位置验收需要 C 编译器")
class RuntimeLocationTests(unittest.TestCase):
    def assert_location(self, text, expression, message, *, files=None, filename="main.xe"):
        with tempfile.TemporaryDirectory(prefix="xe-runtime-location-") as temporary:
            root = Path(temporary)
            entry = root / filename
            entry.write_text(text, encoding="utf-8")
            for relative, contents in (files or {}).items():
                (root / relative).write_text(contents, encoding="utf-8")
            output = root / "program"
            build_executable(entry, output, cc=CC, extra_flags=("-pedantic-errors",))
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"Xe runtime error: {message}\n", result.stderr)
            source = entry
            contents = text
            if files:
                name, contents = next(iter(files.items()))
                source = root / name
            position = Source(contents).position(contents.index(expression))
            self.assertIn(f"  at {source}:{position['line']}:{position['column']}\n", result.stderr)

    def test_parent_arithmetic_after_child_call(self):
        self.assert_location('''fn zero() -> i32 { 0 }
fn divide() -> i32 {
    1 / zero()
}
fn main() { println("{}", divide()); }
''', "1 / zero()", "division by zero")

    def test_checked_shift(self):
        self.assert_location('''fn shift(value: u8, count: i32) -> u8 {
    value bitshl count
}
fn main() { println("{}", shift(128, 1)); }
''', "value bitshl count", "left shift overflow")

    def test_dynamic_array_index(self):
        self.assert_location('''fn get(index: usize) -> i32 {
    let values = [10, 20];
    values[index]
}
fn main() { println("{}", get(2)); }
''', "values[index]", "array index out of bounds")

    def test_panic_extraction(self):
        self.assert_location('''fn absent() -> i32? { None }
fn main() {
    let value = absent()?[panic];
    println("{}", value);
}
''', "absent()?[panic]", "result extraction failed")

    def test_captured_closure_operation(self):
        self.assert_location('''fn main() {
    let divisor = 0;
    let divide << fn[divisor](value: i32) -> i32 {
        value / divisor
    };
    println("{}", divide(3));
}
''', "value / divisor", "division by zero")

    def test_imported_generic_operation(self):
        self.assert_location('''use crate::math::divide;
fn main() { println("{}", divide[i32](1, 0)); }
''', "left / right", "division by zero", files={"math.xe": '''pub fn[T] divide(left: T, right: T) -> T {
    left / right
}
'''})

    def test_unusual_filename_cannot_inject_generated_code(self):
        self.assert_location('''fn divide(value: i32) -> i32 { 1 / value }
fn main() { divide(0); }
''', "1 / value", "division by zero", filename='源"码\\.xe')
