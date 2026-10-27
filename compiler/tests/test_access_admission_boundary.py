"""Public access admission cannot be bypassed with handcrafted source ASTs."""
import copy
import unittest

from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend, lower_to_c
from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source


class AccessAdmissionBoundaryTests(unittest.TestCase):
    def source(self, expression="array⟨0..2⟩[@]⟦1⟧"):
        return Source(f"fn main()->Unit{{let[mut] array:Array[i32,3]=[1,2,3];{expression};}}",
                      "access.xe")

    def assert_rejection(self, action, code="XE-CORE-0001"):
        with self.assertRaises(Diagnostic) as caught:
            action()
        self.assertEqual(caught.exception.code, code, caught.exception.render())

    def test_reviewed_access_crosses_public_ast_checker_and_c_boundaries(self):
        for expression in ("array⟦0⟧", "array⟨0..2⟩[@]⟦1⟧", "array⟨..⟩[@]⟦2⟧",
                           "array⟨0..=2⟩[@]⟦2⟧", "array⟦0⟧@[mut]", "array⟨0..2⟩[@[mut]]⟦1⟧"):
            source = self.source(expression)
            with self.subTest(expression=expression):
                tree = parse_source(source.text, source.filename)
                before = copy.deepcopy(tree)
                self.assertEqual(document(source, tree)["ast"], tree)
                self.assertEqual(Checker(source, tree).check(), [])
                self.assertIn("int main(", lower_to_c(source.text, source.filename))
                self.assertEqual(tree, before)

    def test_source_syntax_rejects_old_point_and_discrete_selection(self):
        for expression in ("array⟨0⟩", "array⟨[1,2,1]⟩"):
            source = self.source(expression)
            with self.subTest(expression=expression):
                self.assert_rejection(lambda source=source: parse_source(source.text, source.filename), "XE-PARSE-0001")
                self.assertEqual([d.code for d in check_source(source.text, source.filename)], ["XE-PARSE-0001"])
                self.assert_rejection(lambda source=source: lower_to_c(source.text, source.filename), "XE-PARSE-0001")

    def test_direct_ast_clients_reject_selector_array_modifiers_and_future_fields(self):
        source = self.source()
        original = parse_source(source.text, source.filename)

        def slice_node(tree):
            return tree["items"][0]["body"]["statements"][-1]["expression"]["target"]["target"]

        def call_node(tree):
            return tree["items"][0]["body"]["statements"][-1]["expression"]

        def inject_list(tree):
            slice_node(tree)["range"] = copy.deepcopy(tree["items"][0]["body"]["statements"][0]["value"])

        def inject_missing_index_node(tree):
            slice_node(tree)["range"] = None

        def inject_index_list_not_a_node(tree):
            slice_node(tree)["range"] = []

        def inject_group_without_expression_node(tree):
            access = slice_node(tree)
            access["range"] = {"kind": "Group", "span": access["span"], "expression": None}

        def inject_call_modifiers(tree):
            call_node(tree)["modifiers"] = []

        def inject_unknown_modifier(tree):
            call = copy.deepcopy(call_node(tree))
            span = call["span"]
            tree["items"][0]["body"]["statements"][-1]["expression"] = {
                "kind": "ViewConstruct", "span": span, "target": call,
                "modifiers": [{"kind": "Modifier", "span": span, "name": "unsafe"}],
            }

        def inject_noncall_modifier_target(tree):
            span = call_node(tree)["span"]
            tree["items"][0]["body"]["statements"][-1]["expression"] = {
                "kind": "ViewConstruct", "span": span,
                "target": copy.deepcopy(slice_node(tree)["target"]),
                "modifiers": [{"kind": "Modifier", "span": span, "name": "mut"}],
            }

        def inject_modifier_future_field(tree):
            call = copy.deepcopy(call_node(tree))
            span = call["span"]
            tree["items"][0]["body"]["statements"][-1]["expression"] = {
                "kind": "ViewConstruct", "span": span, "target": call,
                "modifiers": [{"kind": "Modifier", "span": span, "name": "mut"}],
                "permission_proved": True,
            }

        def inject_future_field(tree):
            slice_node(tree)["origin_checked"] = True

        def inject_future_range_operator(tree):
            slice_node(tree)["range"]["operator"] = "future-range"

        def inject_array_nonlist(tree):
            array = tree["items"][0]["body"]["statements"][0]["value"]
            array["elements"] = tuple(array["elements"])

        for change in (inject_list, inject_missing_index_node, inject_index_list_not_a_node,
                       inject_group_without_expression_node, inject_call_modifiers, inject_unknown_modifier,
                       inject_noncall_modifier_target, inject_modifier_future_field, inject_future_field,
                       inject_future_range_operator, inject_array_nonlist):
            with self.subTest(change=change.__name__):
                tree = copy.deepcopy(original)
                change(tree)
                before = copy.deepcopy(tree)
                self.assert_rejection(lambda tree=tree: document(source, tree))
                self.assertEqual([d.code for d in Checker(source, tree).check()], ["XE-CORE-0001"])
                self.assert_rejection(lambda tree=tree: CBackend(AnnotatedChecker(source, tree)).generate())
                self.assertEqual(tree, before)

    def test_core_access_ast_is_not_a_legacy_ast_escape_hatch(self):
        for expression in ("array⟨0..2⟩[@]⟦1⟧", "array⟦1⟧@[mut]"):
            source = self.source(expression)
            tree = parse_source(source.text, source.filename)
            before = copy.deepcopy(tree)
            with self.subTest(expression=expression), language_profile("legacy"):
                self.assert_rejection(lambda source=source, tree=tree: document(source, tree), "XE-CORE-0002")
                self.assertEqual([d.code for d in Checker(source, tree).check()], ["XE-CORE-0002"])
            self.assertEqual(tree, before)


if __name__ == "__main__":
    unittest.main()
