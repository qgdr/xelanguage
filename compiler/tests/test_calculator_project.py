"""用真实 Xe 项目验收编译器，而非只对 AST 或生成 C 做字符串断言。"""
import json
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import check_source

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples/calculator/main.xe"
CC = shutil.which("cc") or ""


class CalculatorProjectTests(unittest.TestCase):
    def test_project_is_valid_under_the_normal_checks(self):
        errors = check_source(SOURCE.read_text(), str(SOURCE))
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))

    @unittest.skipUnless(CC, "项目运行测试需要系统 C 编译器")
    def test_project_runs_and_reports_errors_without_crashing(self):
        with tempfile.TemporaryDirectory(prefix="xe-calculator-") as directory:
            program = Path(directory) / "calculator"
            build_executable(SOURCE, program, cc=CC,
                             extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
            result = subprocess.run([str(program)], cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        expected = [
            "1 + 2 * 3 = 7", "(1 + 2) * 3 = 9", "20 / 3 = 6",
            "20 - 5 - 3 = 12", "-2 * (3 + 4) = -14", "answer + year = 2068",
            " 12 + 30  = 42", "1 / 0 -> error at byte 2: division by zero",
            "1 + ) -> error at byte 4: expected number, name or parenthesis",
            "(1 + 2 -> error at byte 6: expected closing parenthesis",
            "unknown + 1 -> error at byte 0: unknown constant",
            "1 @ 2 -> error at byte 2: unexpected character",
            "1 2 -> error at byte 2: unexpected token after expression",
            "1000000000000 * 2 -> error at byte 14: arithmetic result exceeds calculator limit",
            " -> error at byte 0: expected number, name or parenthesis",
            "Expressions loaded from examples/calculator/input.calc:",
            "answer * (7 - 2) = 210", "year - 2000 = 26", "(9 + 3) / 4 = 3",
            "8 / 0 -> error at byte 2: division by zero",
        ]
        self.assertEqual(result.stdout.splitlines()[1:], expected)

    @unittest.skipUnless(CC, "项目运行测试需要系统 C 编译器")
    def test_generated_arithmetic_and_product_limits(self):
        # 一起生成源码和独立的整数结果，不用 Xe 解释器自身当作期望值来源。
        # 固定种子保证失败可以重现；括号明确测试嵌套结构而非只测几个常量。
        random_ = random.Random(904)

        def expression(depth):
            if depth == 0:
                value = random_.randint(-9, 9)
                return str(value), value
            left, a = expression(depth - 1)
            right, b = expression(depth - 1)
            operator = random_.choice(("+", "-", "*"))
            value = a + b if operator == "+" else a - b if operator == "-" else a * b
            return f"({left} {operator} {right})", value

        generated = [expression(3) for _ in range(30)]
        valid_limits = [("(" * 64 + "1" + ")" * 64, 1),
                        ("+" * 64 + "1", 1), ("-1000000000000", -1_000_000_000_000)]
        failures = [("(" * 65 + "1" + ")" * 65, "expression nesting is too deep"),
                    ("-" * 65 + "1", "expression nesting is too deep"),
                    ("1000000000001", "integer exceeds calculator limit"),
                    ("1000000000000 + 1", "arithmetic result exceeds calculator limit"),
                    ("-1000000000000 - 1", "arithmetic result exceeds calculator limit"),
                    ("é + 1", "unexpected character")]
        inputs = [text for text, _ in generated + valid_limits + failures]
        # 使用原项目的全部定义，只替换演示入口；不维护第二套解释器实现。
        implementation = SOURCE.read_text().split("fn main()", 1)[0]
        entry = "fn main() {\n" + "\n".join(f"show({json.dumps(text, ensure_ascii=False)});"
                                               for text in inputs) + "\n}\n"
        with tempfile.TemporaryDirectory(prefix="xe-calculator-boundaries-") as directory:
            source = Path(directory) / "test.xe"
            source.write_text(implementation + entry, encoding="utf-8")
            program = Path(directory) / "program"
            build_executable(source, program, cc=CC,
                             extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        lines = result.stdout.splitlines()
        successes = generated + valid_limits
        self.assertEqual(lines[:len(successes)], [f"{text} = {value}" for text, value in successes])
        for line, (text, message) in zip(lines[len(successes):], failures):
            self.assertTrue(line.startswith(text + " -> error at byte "), line)
            self.assertTrue(line.endswith(": " + message), line)
        self.assertEqual(len(lines), len(inputs))
