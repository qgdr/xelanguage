"""Syntax-only evidence for the reviewed finite-type/union declaration slice.

Parser.module is intentional here: admission and elaboration have separate
tests, and accepting a literal token here does not approve its static meaning.
All new syntax is parsed in the core profile, never through a legacy bypass.
"""
import unittest

from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.lexer import Lexer
from compiler.xe_ast.parser import Parser, parse_source
from compiler.xe_ast.source import Diagnostic, Source


def module(text: str):
    with language_profile("core"):
        return Parser(Source(text, "types.xe")).module()


def alias(expression: str):
    return module(f"type Selected = {expression};")["items"][0]["type"]


def raw_span(text: str, node: dict) -> str:
    return text[node["span"]["start"]["offset"]:node["span"]["end"]["offset"]]


class ScalarTypeParserTests(unittest.TestCase):
    def test_empty_set(self):
        self.assertEqual(alias("set {}")["kind"], "SetType")
        self.assertEqual(alias("set {}")["members"], [])

    def test_members_keep_raw_and_explicit_type(self):
        node = alias("set { 0xff:u8, -128:i8, 1_000:i32, }")
        self.assertEqual(node["kind"], "SetType")
        first, second, third = node["members"]
        self.assertEqual(first["value"]["raw"], "0xff")
        self.assertEqual(first["value"]["value"], 255)
        self.assertEqual(first["type"]["path"]["parts"], ["u8"])
        self.assertEqual(second["value"]["kind"], "Unary")
        self.assertEqual(second["value"]["operator"], "-")
        self.assertEqual(second["value"]["operand"]["raw"], "128")
        self.assertEqual(second["type"]["path"]["parts"], ["i8"])
        self.assertEqual(third["value"]["raw"], "1_000")

    def test_unannotated_bool_and_unit_are_structurally_distinct(self):
        node = alias("set {false, true, unit}")
        self.assertEqual([m["value"]["literal_kind"] for m in node["members"]],
                         ["false", "true", "unit"])
        self.assertTrue(all(m["type"] is None for m in node["members"]))

    def test_literal_candidate_syntax_is_not_static_approval(self):
        # Elaboration, not this token parser, rejects unsupported carriers.
        node = alias("set { 1, 1.5, 'x', b'x', \"text\" }")
        self.assertEqual([m["value"]["literal_kind"] for m in node["members"]],
                         ["INTEGER", "FLOAT", "CHAR", "BYTE", "STRING"])

    def test_member_and_annotation_spans(self):
        text = "type Edge = set {\n    -0x80 : i8,\n};"
        node = module(text)["items"][0]["type"]
        member = node["members"][0]
        self.assertEqual(raw_span(text, node), "set {\n    -0x80 : i8,\n}")
        self.assertEqual(raw_span(text, member), "-0x80 : i8")
        self.assertEqual(raw_span(text, member["value"]), "-0x80")
        self.assertEqual(raw_span(text, member["value"]["operand"]), "0x80")
        self.assertEqual(raw_span(text, member["type"]), "i8")
        self.assertEqual(member["span"]["start"]["line"], 2)
        self.assertEqual(member["span"]["start"]["column"], 5)

    def test_union_spellings_are_left_associative(self):
        node = alias("A union B | C")
        self.assertEqual(node["kind"], "UnionType")
        self.assertEqual(node["operator"], "|")
        self.assertEqual(node["left"]["kind"], "UnionType")
        self.assertEqual(node["left"]["operator"], "union")
        self.assertEqual(node["left"]["left"]["path"]["parts"], ["A"])
        self.assertEqual(node["left"]["right"]["path"]["parts"], ["B"])
        self.assertEqual(node["right"]["path"]["parts"], ["C"])

    def test_pointer_suffix_binds_more_tightly(self):
        node = alias("A@[mut] | B@")
        self.assertEqual(node["kind"], "UnionType")
        self.assertEqual(node["left"]["kind"], "PointerType")
        self.assertEqual(node["left"]["modifiers"][0]["name"], "mut")
        self.assertEqual(node["right"]["kind"], "PointerType")
        grouped = alias("(A | B)@")
        self.assertEqual(grouped["kind"], "PointerType")
        self.assertEqual(grouped["target"]["kind"], "UnionType")

    def test_grouping_changes_union_tree(self):
        node = alias("A | (B union C)")
        self.assertEqual(node["left"]["path"]["parts"], ["A"])
        self.assertEqual(node["right"]["kind"], "UnionType")
        self.assertEqual(node["right"]["operator"], "union")

    def test_union_span_includes_spelling_and_parentheses(self):
        text = "type Combined = (A | B) union C@;"
        node = module(text)["items"][0]["type"]
        self.assertEqual(raw_span(text, node), "(A | B) union C@")

    def test_function_result_consumes_complete_type(self):
        node = alias("fn() -> A | B")
        self.assertEqual(node["kind"], "FunctionType")
        self.assertEqual(node["result"]["kind"], "UnionType")
        grouped = alias("(fn() -> A) | B")
        self.assertEqual(grouped["kind"], "UnionType")
        self.assertEqual(grouped["left"]["kind"], "FunctionType")
        self.assertEqual(grouped["left"]["result"]["kind"], "NamedType")

    def test_set_member_annotation_consumes_complete_type(self):
        node = alias("set { 1:A | B, true:bool }")
        self.assertEqual(node["members"][0]["type"]["kind"], "UnionType")
        self.assertEqual(node["members"][1]["type"]["kind"], "NamedType")

    def test_set_and_union_remain_ordinary_names(self):
        tree = module("type set = i32; type union = set; "
                      "fn union(set: union) -> (set) { set }")
        self.assertEqual(tree["items"][0]["name"], "set")
        self.assertEqual(tree["items"][1]["name"], "union")
        self.assertEqual(tree["items"][2]["name"], "union")
        self.assertEqual(alias("set@ union union")["right"]["path"]["parts"], ["union"])

    def test_pipe_keeps_its_longest_token(self):
        tokens = Lexer(Source("A | B |> handler union set")).scan()
        self.assertEqual([(t.kind, t.text) for t in tokens[:-1]],
                         [("IDENT", "A"), ("|", "|"), ("IDENT", "B"),
                          ("|>", "|>"), ("IDENT", "handler"),
                          ("IDENT", "union"), ("IDENT", "set")])
        tree = module("fn f() { item |> handler }")
        self.assertEqual(tree["items"][0]["body"]["tail"]["kind"], "Pipeline")

    def test_malformed_set_members_are_parse_errors(self):
        for expression in ("set {,}", "set {1:i32 2:i32}", "set {1:i32,,}",
                           "set {name:i32}", "set {call():i32}", "set {1+2:i32}",
                           "set {-true}", "set {-1.5}", "set {1:}", "set {1:i32"):
            with self.subTest(expression=expression), self.assertRaises(Diagnostic) as caught:
                alias(expression)
            self.assertEqual(caught.exception.code, "XE-PARSE-0001")

    def test_missing_union_operand_is_a_parse_error(self):
        for expression in ("A |", "A union", "| A", "A | | B", "A union | B"):
            with self.subTest(expression=expression), self.assertRaises(Diagnostic) as caught:
                alias(expression)
            self.assertEqual(caught.exception.code, "XE-PARSE-0001")

    def test_legacy_explicitly_rejects_new_type_syntax(self):
        with language_profile("legacy"):
            for expression in ("set {}", "set {1:i32}", "i32 | u8", "i32 union u8"):
                with self.subTest(expression=expression), self.assertRaises(Diagnostic) as caught:
                    parse_source(f"type New = {expression};")
                self.assertEqual(caught.exception.code, "XE-PARSE-0001")
                if "set" not in expression:
                    self.assertIn("core", caught.exception.message)

    def test_legacy_named_types_and_existing_ast_shape_are_preserved(self):
        with language_profile("legacy"):
            tree = parse_source("type set = i32; type union = set; "
                                "fn f(pointer: i32@[mut], result: i32?[Error]) {} "
                                "fn old() -> set { 1 }")
        self.assertEqual(tree["items"][1]["type"]["path"]["parts"], ["set"])
        self.assertEqual(tree["items"][3]["result"]["path"]["parts"], ["set"])
        pointer, result = tree["items"][2]["parameters"]
        self.assertEqual(set(pointer["type"]), {"kind", "span", "target", "modifiers"})
        self.assertEqual(set(result["type"]), {"kind", "span", "value", "error"})


if __name__ == "__main__":
    unittest.main()
