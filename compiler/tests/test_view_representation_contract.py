"""Check the source/representation correspondence on actual core programs.

The independent oracle enumerates original coordinates, rather than reproducing
the backend's rebased pointer formula. Dangerous lifetime cases stay static.
This is bounded evidence alongside, not instead of, the separate Lean model.
"""

import unittest
from pathlib import Path

from compiler.tests import test_core_access_backend as backend
from compiler.tests.documentation_support import private_document
from compiler.tests.test_repository import fenced_blocks
from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source

ROOT = Path(__file__).resolve().parents[2]


class ViewRepresentationContractTests(unittest.TestCase):
    def checked(self, text):
        checker = Checker(Source(text), parse_source(text))
        self.assertEqual(checker.check(), [])
        self.assertEqual(checker.warnings, [])

    def test_chinese_theory_example_is_counted_and_executes_with_default_sanitizers(self):
        blocks = [code for info, _, code in fenced_blocks(private_document("docs/theory.md").read_text(encoding="utf-8"))
                  if info == "xe-core"]
        self.assertEqual(len(blocks), 1)
        self.checked(blocks[0])
        backend.CoreAccessBackendTests().execute(blocks[0], "99 99 99\n", sanitize=True)

    def test_all_small_nested_ranges_preserve_original_coordinate_reads(self):
        values = [17, 29, 43, 61]
        lines = ["fn bound(value:usize)->usize{value}",
                 "fn main()->Unit{let values:Array[i32,4]=[17,29,43,61];"]
        expected = []
        cases = 0
        for lower in range(5):
            for upper in range(lower, 5):
                lines.append(f"let parent{cases}:View[i32@]=values⟨bound({lower})..bound({upper})⟩[@];")
                for child_lower in range(lower, upper + 1):
                    for child_upper in range(child_lower, upper + 1):
                        name = f"child{cases}_{child_lower}_{child_upper}"
                        lines.append(f"let {name}:View[i32@]=parent{cases}⟨bound({child_lower})..bound({child_upper})⟩;")
                        for coordinate in range(child_lower, child_upper):
                            lines.append(f'println("{{}}",{name}⟦bound({coordinate})⟧#);')
                            expected.append(str(values[coordinate]))
                cases += 1
        self.assertEqual(cases, 15)  # Includes every possible empty anchor.
        lines.append("}")
        text = "\n".join(lines)
        self.checked(text)
        backend.CoreAccessBackendTests().execute(text, "\n".join(expected) + "\n", sanitize=True)

    def test_array_copy_gets_new_boxes_while_view_copies_share_original_boxes(self):
        text = '''fn main()->Unit{
            let[mut] original:Array[i32,3]=[10,20,30];
            let copied:Array[i32,3]=original;
            let view:View[i32@[mut]]=original⟨1..3⟩[@[mut]];
            let alias:View[i32@[mut]]=view;
            alias⟦1⟧#=99;
            println("{} {} {}",original⟦1⟧,copied⟦1⟧,view⟦1⟧#);
        }'''
        self.checked(text)
        backend.CoreAccessBackendTests().execute(text, "99 20 99\n", sanitize=True)

    def test_empty_set_inclusion_does_not_allow_outside_empty_anchor(self):
        # These are defined boundary panics, not invalid-pointer experiments.
        text = '''fn bound(value:usize)->usize{value}fn main()->Unit{
            let values:Array[i32,4]=[10,20,30,40];
            let parent:View[i32@]=values⟨1..3⟩[@];
            let empty:View[i32@]=parent⟨bound(0)..bound(0)⟩;
        }'''
        operation = "parent⟨bound(0)..bound(0)⟩"
        backend.CoreAccessBackendTests().runtime_failure(text, operation, "slice range out of bounds", sanitize=True)

    def test_readonly_permission_and_known_source_escape_are_static_errors(self):
        invalid = (
            ('''fn main()->Unit{let[mut] values:Array[i32,2]=[1,2];
                let view:View[i32@]=values⟨0..2⟩[@];view⟦0⟧#=3;}''', "XE-MUT-0001"),
            ('''fn wrong()->i32@{let values:Array[i32,2]=[1,2];
                let view:View[i32@]=values⟨0..2⟩[@];view⟦0⟧}fn main()->Unit{}''', "XE-PTR-0004"),
        )
        for text, code in invalid:
            with self.subTest(code=code):
                errors = check_source(text)
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, code)

    def test_maybe_remains_closed_in_core_and_old_marker_is_legacy_only(self):
        for type_ in ("i32?", "i32?[None]", "i32?[Unit]"):
            with self.subTest(type=type_):
                errors = check_source(f"type A={type_};fn main()->Unit{{unit}}")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-CORE-0001")
        with language_profile("legacy"):
            text = "type A=i32?;type B=i32?[None];type C=i32?[Unit];fn main()->Unit{unit}"
            checker = Checker(Source(text), parse_source(text))
            self.assertEqual(checker.check(), [])
            self.assertEqual(checker.alias_types["A"], checker.alias_types["B"])
            self.assertNotEqual(checker.alias_types["A"], checker.alias_types["C"])


if __name__ == "__main__":
    unittest.main()
