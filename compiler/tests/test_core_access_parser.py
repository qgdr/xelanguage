"""DEC-035 syntax separates element boxes from coordinate restrictions.

Component parsing records syntax only: Array versus View place/value categories
are determined by the checker. The legacy grammar remains explicitly separate.
"""
import unittest

from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.lexer import Lexer
from compiler.xe_ast.parser import Parser
from compiler.xe_ast.source import Diagnostic, Source


def expression(text: str, profile="core"):
    with language_profile(profile):
        tree = Parser(Source(f"fn inspect()->Unit{{{text}}}", "access.xe")).module()
    return tree["items"][0]["body"]["tail"]


class CoreAccessParserTests(unittest.TestCase):
    def test_exact_unicode_tokens_do_not_replace_comparisons(self):
        self.assertEqual((ord("⟦"), ord("⟧")), (0x27E6, 0x27E7))
        self.assertEqual((ord("⟨"), ord("⟩")), (0x27E8, 0x27E9))
        tokens = Lexer(Source("a⟦2⟧ a⟨1..3⟩ a < b > c")).scan()
        self.assertEqual([token.kind for token in tokens], [
            "IDENT", "⟦", "INTEGER", "⟧", "IDENT", "⟨", "INTEGER", "..", "INTEGER", "⟩",
            "IDENT", "<", "IDENT", ">", "IDENT", "EOF",
        ])

    def test_element_index_has_its_own_source_ast(self):
        node = expression("values⟦index⟧")
        self.assertEqual(set(node), {"kind", "span", "target", "index"})
        self.assertEqual(node["kind"], "Index")
        self.assertEqual(node["target"]["path"]["parts"], ["values"])
        self.assertEqual(node["index"]["path"]["parts"], ["index"])

    def test_address_and_dereference_are_separate_operations(self):
        node = expression("values⟦2⟧@")
        self.assertEqual(node["kind"], "Borrow")
        self.assertEqual(node["operand"]["kind"], "Index")
        self.assertEqual(expression("view⟦2⟧#")["kind"], "Dereference")

    def test_ordinary_function_calls_keep_their_existing_shape(self):
        node = expression("inspect(index)")
        self.assertEqual(set(node), {"kind", "span", "callee", "arguments"})
        self.assertEqual(node["kind"], "Call")
        self.assertFalse(Parser.is_place(node))
        # Parsing alone never interprets an ordinary Call as Array indexing.
        self.assertEqual(expression("values(index)")["kind"], "Call")

    def test_range_reuses_range_ast_without_inventing_coordinate_fields(self):
        node = expression("view⟨lower..upper⟩")
        range_ = node["range"]
        self.assertEqual(node["kind"], "Restriction")
        self.assertEqual(set(node), {"kind", "span", "target", "range"})
        self.assertEqual(set(range_), {"kind", "span", "operator", "lower", "upper"})
        self.assertEqual(range_["operator"], "..")
        self.assertEqual(range_["lower"]["path"]["parts"], ["lower"])
        self.assertEqual(range_["upper"]["path"]["parts"], ["upper"])

    def test_full_range_keeps_both_omissions_explicit(self):
        range_ = expression("view⟨..⟩")["range"]
        self.assertIsNone(range_["lower"])
        self.assertIsNone(range_["upper"])

    def test_inclusive_range_keeps_explicit_bounds(self):
        for text in ("view⟨lower..=upper⟩", "view⟨(lower..=upper)⟩"):
            with self.subTest(text=text):
                range_ = expression(text)["range"]
                if range_["kind"] == "Group":
                    range_ = range_["expression"]
                self.assertEqual(range_["operator"], "..=")
                self.assertEqual(range_["lower"]["path"]["parts"], ["lower"])
                self.assertEqual(range_["upper"]["path"]["parts"], ["upper"])

    def test_readonly_array_mapping_has_explicit_constructor(self):
        node = expression("values⟨1..4⟩[@]")
        self.assertEqual(set(node), {"kind", "span", "target", "modifiers"})
        self.assertEqual(node["kind"], "ViewConstruct")
        self.assertEqual(node["target"]["kind"], "Restriction")
        self.assertEqual(node["modifiers"], [])

    def test_writable_array_mapping_attaches_mut_to_address_request(self):
        node = expression("values⟨1..4⟩[@[mut]]")
        self.assertEqual(node["kind"], "ViewConstruct")
        self.assertEqual([modifier["name"] for modifier in node["modifiers"]], ["mut"])
        self.assertEqual(node["target"]["target"]["path"]["parts"], ["values"])

    def test_nested_postfix_access_and_call_input(self):
        node = expression("inspect(values⟨1..4⟩[@]⟦2⟧#)")
        selected = node["arguments"][0]["operand"]
        self.assertEqual(selected["kind"], "Index")
        self.assertEqual(selected["target"]["kind"], "ViewConstruct")
        self.assertEqual(expression("values⟦indices⟦0⟧⟧")["index"]["kind"], "Index")

    def test_nested_ranges_remain_separate_restrictions(self):
        node = expression("view⟨1..6⟩⟨2..4⟩")
        self.assertEqual(node["kind"], "Restriction")
        self.assertEqual(node["target"]["kind"], "Restriction")
        self.assertEqual(node["range"]["lower"]["value"], 2)
        self.assertEqual(node["target"]["range"]["lower"]["value"], 1)

    def test_arithmetic_index_is_structure_not_type_directed_guessing(self):
        node = expression("values⟦start + offset⟧")
        self.assertEqual(node["index"]["kind"], "Binary")
        self.assertEqual(node["index"]["operator"], "+")

    def test_access_as_pipeline_input_keeps_handler_shape(self):
        node = expression("values⟦1⟧ |> inspect")
        self.assertEqual(node["kind"], "Pipeline")
        self.assertEqual(node["input"]["kind"], "Index")
        self.assertEqual(node["handler"]["kind"], "FunctionTarget")

    def test_address_mapping_whitespace_does_not_change_the_attachment(self):
        for text in ("a⟨1..6⟩[@[mut]]", "a⟨1..6⟩ [@[mut]]", "a ⟨1 .. 6⟩\n[ @ [ mut ] ]"):
            with self.subTest(text=text):
                node = expression(text)
                self.assertEqual(node["kind"], "ViewConstruct")
                self.assertEqual(node["modifiers"][0]["name"], "mut")

    def test_mapping_and_modifier_spans_are_source_code_point_offsets(self):
        text = "fn inspect()->Unit{前⟨1..6⟩ [@[mut]]}"
        node = Parser(Source(text, "access.xe")).module()["items"][0]["body"]["tail"]

        def raw(value):
            return text[value["span"]["start"]["offset"]:value["span"]["end"]["offset"]]

        self.assertEqual(raw(node), "前⟨1..6⟩ [@[mut]]")
        self.assertEqual(raw(node["target"]), "前⟨1..6⟩")
        self.assertEqual(raw(node["target"]["range"]), "1..6")
        self.assertEqual(raw(node["modifiers"][0]), "mut")

    def test_index_write_permission_uses_ordinary_address_operation(self):
        node = expression("values⟦2⟧@[mut]")
        self.assertEqual(node["kind"], "Borrow")
        self.assertEqual(node["operand"]["kind"], "Index")
        self.assertEqual([modifier["name"] for modifier in node["modifiers"]], ["mut"])

    def test_removed_index_attachment_is_rejected(self):
        for text in ("values⟦2⟧[@]", "values⟦2⟧[mut]", "function(2)[mut]"):
            with self.subTest(text=text), self.assertRaises(Diagnostic) as caught:
                expression(text)
            self.assertEqual(caught.exception.code, "XE-PARSE-0001")

    def test_array_mapping_does_not_add_arbitrary_modifiers(self):
        for suffix in ("[]", "[mut]", "[unsafe]", "[i32]", "[@[unsafe]]", "[@[mut,mut]]",
                       "[@,mut]", "[@[mut,unsafe]]", "[@][mut]", "[@[mut]][mut]"):
            with self.subTest(suffix=suffix), self.assertRaises(Diagnostic):
                expression("values⟨1..3⟩" + suffix)

    def test_old_point_delimiters_receive_a_migration_diagnostic(self):
        for text in ("values⟨2⟩", "values⟨index⟩", "values⟨(index)⟩", "values⟨start+offset⟩"):
            with self.subTest(text=text), self.assertRaises(Diagnostic) as caught:
                expression(text)
            self.assertIn("单点使用 a⟦i⟧", caught.exception.message)

    def test_index_lists_are_not_range_restrictions(self):
        for text in ("values⟨[1,2,1]⟩", "values⟨([1,2,1])⟩", "values⟨[]⟩"):
            with self.subTest(text=text), self.assertRaises(Diagnostic) as caught:
                expression(text)
            self.assertIn("离散索引列表", caught.exception.message)

    def test_range_and_list_cannot_be_single_element_indices(self):
        for text in ("values⟦1..3⟧", "values⟦[1,2]⟧"):
            with self.subTest(text=text), self.assertRaises(Diagnostic):
                expression(text)

    def test_group_attachment_does_not_guess_an_array_mapping(self):
        for text in ("view[mut]", "(values⟨1..6⟩)[mut]"):
            with self.subTest(text=text):
                self.assertEqual(expression(text)["kind"], "BracketApply")

    def test_legacy_call_result_attachment_keeps_historical_ast(self):
        node = expression("function(2)[mut]", "legacy")
        self.assertEqual(node["kind"], "BracketApply")
        self.assertEqual(node["object"]["kind"], "Call")

    def test_comparisons_are_still_unambiguous(self):
        node = expression("values⟦2⟧ < limit")
        self.assertEqual(node["kind"], "ComparisonChain")
        self.assertEqual(node["operators"], ["<"])
        self.assertEqual(node["operands"][0]["kind"], "Index")
        self.assertEqual(expression("a < b > c")["operators"], ["<", ">"])

    def test_range_whitespace_does_not_select_another_grammar(self):
        for text in ("a⟨1..4⟩", "a ⟨ 1 .. 4 ⟩", "a\n⟨1 ..\n4⟩"):
            with self.subTest(text=text):
                self.assertEqual(expression(text)["range"]["kind"], "Range")

    def test_omitted_one_sided_bounds_are_still_unconfirmed(self):
        for text in ("a⟨..3⟩", "a⟨3..⟩", "a⟨..=3⟩", "a⟨3..=⟩", "a⟨(..3)⟩",
                     "a⟨(3..)⟩", "a⟨[mut] 1..3⟩"):
            with self.subTest(text=text), self.assertRaises(Diagnostic):
                expression(text)

    def test_inclusive_full_range_does_not_invent_defaults(self):
        with self.assertRaises(Diagnostic) as caught:
            expression("a⟨..=⟩")
        self.assertIn("明确两个端点", caught.exception.message)

    def test_empty_multiple_or_mismatched_delimiters_are_rejected(self):
        for text in ("a⟨⟩", "a⟨1,2⟩", "a⟨1]", "a⟨1", "a⟨1..2..3⟩", "a⟦⟧", "a⟦1⟩"):
            with self.subTest(text=text), self.assertRaises(Diagnostic):
                expression(text)

    def test_ordinary_call_result_is_not_implicitly_addressable(self):
        with self.assertRaises(Diagnostic) as caught:
            expression("make()@")
        self.assertIn("稳定存储", caught.exception.message)

    def test_legacy_does_not_acquire_new_delimiters(self):
        for text in ("a⟨1..3⟩", "a⟦2⟧"):
            with self.subTest(text=text), self.assertRaises(Diagnostic) as caught:
                expression(text, "legacy")
            self.assertEqual(caught.exception.code, "XE-LEX-0001")
        self.assertEqual(expression("a[2]", "legacy")["kind"], "BracketApply")


if __name__ == "__main__":
    unittest.main()
