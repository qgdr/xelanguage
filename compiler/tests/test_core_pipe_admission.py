"""Keep new ordinary-pipe admission closed around unreviewed constructs."""
import copy
import unittest

from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source

TEXT = '''fn identity(value:i32)->i32 { value }
fn main()->Unit {
    let original:i32=42;
    let answer:i32=original |> item:i32 -> item;
    println("{} {}", original, answer);
}'''


class CorePipeAdmissionTests(unittest.TestCase):
    def test_ordinary_pipe_admission_does_not_rewrite_the_source_ast(self):
        tree = parse_source(TEXT)
        original = copy.deepcopy(tree)
        self.assertEqual(Checker(Source(TEXT), tree).check(), [])
        self.assertEqual(tree, original)

    def test_unknown_pipe_target_handler_and_parameter_fields_fail_closed(self):
        def pipe(tree):
            return tree["items"][-1]["body"]["statements"][1]["value"]

        changes = (
            lambda tree: pipe(tree).update(force_move=True),
            lambda tree: pipe(tree)["handler"].update(implicit_closure=True),
            lambda tree: pipe(tree)["handler"]["parameters"][0].update(default=42),
            lambda tree: pipe(tree)["handler"]["parameters"][0].update(mutable=True),
        )
        for change in changes:
            with self.subTest(change=change):
                tree = parse_source(TEXT)
                change(tree)
                before = copy.deepcopy(tree)
                self.assertEqual([d.code for d in Checker(Source(TEXT), tree).check()], ["XE-CORE-0001"])
                for boundary in (lambda tree=tree: document(Source(TEXT), tree),
                                 lambda tree=tree: CBackend(AnnotatedChecker(Source(TEXT), tree)).generate()):
                    with self.assertRaises(Diagnostic) as caught:
                        boundary()
                    self.assertEqual(caught.exception.code, "XE-CORE-0001")
                self.assertEqual(tree, before)

    def test_literal_input_is_not_typed_from_outer_binding_or_handler_output(self):
        for expression in ("42 |> item -> item", '42 |> _ -> "text"',
                           "{42} |> item -> item"):
            with self.subTest(expression=expression):
                text = f"fn main()->Unit {{ let answer:i32={expression}; }}"
                self.assertEqual([d.code for d in check_source(text)], ["XE-CORE-0001"])

    def test_target_or_parameter_shadowing_is_not_silently_approved(self):
        for body in ("let original:i32=42;original |> original -> original;",
                     "let original:i32=42;original |> identity -> identity;"):
            text = "fn identity(value:i32)->i32{value}fn main()->Unit{" + body + "}"
            with self.subTest(body=body):
                self.assertEqual([d.code for d in check_source(text)], ["XE-CORE-0001"])

    def test_discard_never_becomes_a_readable_name_even_in_a_handcrafted_ast(self):
        tree = parse_source(TEXT)
        handler = tree["items"][-1]["body"]["statements"][1]["value"]["handler"]
        handler["parameters"][0]["name"] = "_"
        handler["body"]["path"]["parts"] = ["_"]
        self.assertEqual([d.code for d in Checker(Source(TEXT), tree).check()], ["XE-CORE-0001"])


if __name__ == "__main__":
    unittest.main()
