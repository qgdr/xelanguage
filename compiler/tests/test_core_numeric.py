"""DEC-041 integers: one checked arithmetic contract in every build mode.

Failures below are defined panic executions, not C undefined-behavior probes.
The sanitizer matrix deliberately exercises successful and failing boundaries.
"""
import copy
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import check_source
from compiler.xe_ast.source import Source

CC = shutil.which(os.environ.get("CC", "gcc"))
TYPES = [f"{sign}{width}" for sign in ("i", "u") for width in (8, 16, 32, 64)] + ["isize", "usize"]


def bounds(name):
    width = struct.calcsize("P") * 8 if name.endswith("size") else int(name[1:])
    return (-(1 << (width - 1)), (1 << (width - 1)) - 1) if name.startswith("i") else (0, (1 << width) - 1)


class CoreNumericTests(unittest.TestCase):
    def accepted(self, text):
        errors = check_source(text, "numeric.xe")
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        tree = parse_source(text)
        before = copy.deepcopy(tree)
        document(Source(text), tree)
        warnings = []
        c = lower_to_c(text, warnings=warnings)
        self.assertEqual(warnings, [])
        self.assertEqual(tree, before)
        return c

    def rejected(self, text, code):
        errors = check_source(text)
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def run_program(self, text, *, optimize="-O0", sanitize=False):
        assert CC is not None
        self.accepted(text)
        flags = [optimize]
        if sanitize:
            flags += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"]
        with tempfile.TemporaryDirectory(prefix="xe-core-numeric-") as directory:
            source, output = Path(directory) / "main.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, output, cc=CC, extra_flags=flags)
            return subprocess.run([str(output)], capture_output=True, text=True, timeout=10)

    def test_static_failures_for_every_integer_carrier(self):
        for type_ in TYPES:
            low, high = bounds(type_)
            expressions = [f"{high}+1", f"{low}-1", f"{high}*2", "1/0", "1%0"]
            if low < 0:
                expressions += [f"({low})/-1", f"({low})%-1", f"-({low})"]
            else:
                expressions += ["-1"]
            for expression in expressions:
                with self.subTest(type=type_, expression=expression):
                    errors = check_source(f"fn main()->Unit{{let x:{type_}={expression};}}")
                    self.assertTrue(errors)
                    self.assertIn(errors[0].code, {"XE-NUM-0001", "XE-NUM-0002", "XE-TYPE-0004"})

    def test_intermediate_overflow_is_not_erased_by_later_arithmetic(self):
        self.rejected("fn main()->Unit{let x:i8=(127+1)-1;}", "XE-NUM-0001")
        self.rejected("fn main()->Unit{let x:i8=2*(100-1);}", "XE-NUM-0001")
        self.rejected("fn div(x:i32)->i32{x/0} fn main()->Unit{}", "XE-NUM-0002")

    def test_no_integer_widening_and_no_unreviewed_operator_defaults(self):
        self.rejected("fn main()->Unit{let a:u8=1;let b:i32=2;let c:i32=a+b;}", "XE-TYPE-0001")
        self.rejected("fn main()->Unit{let a:u8=1;let b:i32=2;let c:bool=a==b;}", "XE-TYPE-0001")
        for declaration in ("let x:bool=1<2;", "let x:bool=true==false;",
                            "let x:i32=+1;", "let x:i32=1 bitand 2;",
                            "let x:bool=1<2<3;", "let x:f32=1.0+2.0;"):
            with self.subTest(declaration=declaration):
                self.assertTrue(check_source("fn main()->Unit{" + declaration + "}"))

    @unittest.skipUnless(CC, "integer runtime requires a C compiler")
    def test_all_widths_boundaries_comparisons_and_boolean_short_circuit(self):
        functions, statements = [], []
        for index, type_ in enumerate(TYPES):
            low, high = bounds(type_)
            functions.append(f'''fn calc{index}(a:{type_},b:{type_})->Unit {{
                println("{{}} {{}} {{}} {{}} {{}}",a+b,a-b,a*b,a/b,a%b);
                println("{{}} {{}} {{}} {{}} {{}} {{}}",a==b,a!=b,a<b,a<=b,a>b,a>=b);
            }}''')
            statements += [f"calc{index}(7,3);", f"let min{index}:{type_}=({low});",
                           f"let max{index}:{type_}={high};",
                           f'println("{{}} {{}}",min{index},max{index});']
        functions += ['fn forbidden()->bool{panic("must be skipped");}',
                      'fn mark(p:i32@[mut],value:i32)->bool{p#=p#*10+value;true}']
        statements += ['let a:bool=false and forbidden();', 'let b:bool=true or forbidden();',
                       'let[mut] order:i32=0;',
                       'let c:bool=mark(order@[mut],1) and mark(order@[mut],2);',
                       'let d:bool=not false;', 'println("{} {} {} {} {}",a,b,c,d,order);',
                       'let n:i32=-7;', 'let p:i32=3;', 'println("{} {}",n/p,n%p);']
        text = "\n".join(functions) + "fn main()->Unit{" + "\n".join(statements) + "}"
        expected = "".join("10 4 21 2 1\nfalse true false false true true\n" +
                           f"{bounds(type_)[0]} {bounds(type_)[1]}\n" for type_ in TYPES)
        expected += "false true true true 12\n-2 -1\n"
        result = self.run_program(text, sanitize=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(CC, "integer panic requires a C compiler")
    def test_dynamic_failures_are_checked_in_debug_and_optimized_builds(self):
        for optimize in ("-O0", "-O2"):
            for type_ in TYPES:
                low, high = bounds(type_)
                # One runtime input per fixture keeps the failure dynamic.
                expressions = [("x+1", high), ("x-1", low), ("x*2", high), ("1/x", 0), ("1%x", 0)]
                if low < 0:
                    expressions += [("x/-1", low), ("x%-1", low), ("-x", low)]
                for expression, value in expressions:
                    with self.subTest(optimize=optimize, type=type_, expression=expression):
                        text = (f"fn fail(x:{type_})->{type_}{{{expression}}}"
                                f'fn main()->Unit{{println("{{}}",fail({value}));}}')
                        result = self.run_program(text, optimize=optimize)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("Xe runtime error:", result.stderr)
                        self.assertTrue("integer overflow" in result.stderr or "division by zero" in result.stderr)
                        self.assertNotIn("AddressSanitizer", result.stderr)


if __name__ == "__main__":
    unittest.main()
