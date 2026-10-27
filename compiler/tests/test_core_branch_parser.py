"""Parser-only tests for handlers and the accepted selector spelling.

Parser.module deliberately bypasses core admission: these assertions establish
AST structure and explicit syntax rejection, not executable branch semantics.
Else ordering/input, view coordinates and new transfer rules remain separate.
"""
import unittest

from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.parser import Parser
from compiler.xe_ast.source import Diagnostic, Source


def expression(text: str, profile="core"):
    with language_profile(profile):
        tree = Parser(Source(f"fn inspect()->Unit{{{text}}}", "branch.xe")).module()
    return tree["items"][0]["body"]["tail"]


class CoreBranchParserTests(unittest.TestCase):
    def test_named_function_target_is_not_an_implicit_value_result(self):
        handler = expression("source |> transform")["handler"]
        self.assertEqual(handler["kind"], "FunctionTarget")
        self.assertEqual(handler["target"]["path"]["parts"], ["transform"])

    def test_single_parameter_binding_keeps_optional_type(self):
        handler = expression("source |> value: i32 -> value")["handler"]
        self.assertEqual(handler["kind"], "HandlerBinding")
        self.assertEqual(len(handler["parameters"]), 1)
        parameter = handler["parameters"][0]
        self.assertEqual(parameter["name"], "value")
        self.assertEqual(parameter["type"]["path"]["parts"], ["i32"])
        self.assertIsNone(expression("source |> value -> value")["handler"]["parameters"][0]["type"])

    def test_ignore_input_is_parameter_not_a_selector_or_value(self):
        handler = expression("source |> _ -> replacement")["handler"]
        self.assertEqual(handler["parameters"][0]["name"], "_")
        self.assertEqual(handler["body"]["path"]["parts"], ["replacement"])
        for text in ("_", "source |> _", "source ? { _ :> transform, }"):
            with self.subTest(text=text), self.assertRaises(Diagnostic):
                expression(text)

    def test_zero_payload_can_target_a_zero_parameter_function_name(self):
        # Arity belongs to the checker. This is not evidence that absent is
        # zero-argument or that a specific result type has been admitted.
        node = expression("source? 1> present 2> absent")
        self.assertEqual(node["kind"], "Branch")
        self.assertEqual([arm["channel"] for arm in node["arms"]], [1, 2])
        self.assertTrue(all(arm["handler"]["kind"] == "FunctionTarget" for arm in node["arms"]))

    def test_parameter_list_handlers_are_rejected_for_all_pipe_positions(self):
        for text in ("source |> [] -> 0", "source |> [x] -> x", "source |> [x,y] -> x",
                     "source? 1> [x,y] -> x 2> fallback",
                     "source? {Token::Value :> [x,y] -> x,}"):
            with self.subTest(text=text), self.assertRaises(Diagnostic) as caught:
                expression(text)
            self.assertEqual(caught.exception.code, "XE-PARSE-0001")
            self.assertIn("整体输入", caught.exception.message)

    def test_selector_is_a_path_without_payload_filter(self):
        node = expression("token? {Token::Integer :> value: i64 -> value, Token::End :> empty,}")
        self.assertEqual(node["style"], "named")
        selector = node["arms"][0]["selector"]
        self.assertEqual(selector["kind"], "VariantSelector")
        self.assertEqual(selector["path"]["parts"], ["Token", "Integer"])
        self.assertIsNone(selector["filters"])
        self.assertEqual(node["arms"][0]["handler"]["parameters"][0]["name"], "value")

    def test_selector_payload_filter_is_rejected_even_for_underscore(self):
        for selector in ("Token::Value[_]", "Token::Value[1]", "Token::Value[]",
                         "Token::Value[Inner::Value]"):
            with self.subTest(selector=selector), self.assertRaises(Diagnostic) as caught:
                expression(f"token? {{{selector} :> value -> value,}}")
            self.assertIn("不附带 []", caught.exception.message)

    def test_else_is_a_distinct_node_not_a_wildcard_alias(self):
        node = expression("token? {Token::Value :> value -> value, else :> _ -> 0,}")
        selector = node["arms"][1]["selector"]
        self.assertEqual(selector["kind"], "ElseSelector")
        self.assertEqual(set(selector), {"kind", "span"})
        self.assertEqual(node["arms"][1]["handler"]["parameters"][0]["name"], "_")

    def test_branch_input_and_handler_preserve_pointer_annotations(self):
        node = expression("token?[@] {Token::Value :> value: i64@ -> value#, Token::End :> empty,}")
        self.assertEqual(node["modifiers"], ["borrow"])
        self.assertEqual(node["arms"][0]["handler"]["parameters"][0]["type"]["kind"], "PointerType")

    def test_named_pipeline_chain_is_left_associative(self):
        node = expression("source |> first |> second")
        self.assertEqual(node["kind"], "Pipeline")
        self.assertEqual(node["input"]["kind"], "Pipeline")
        self.assertEqual(node["handler"]["target"]["path"]["parts"], ["second"])

    def test_binding_body_owns_a_following_pipeline_unless_grouped(self):
        node = expression("source |> value -> first(value) |> second")
        self.assertEqual(node["handler"]["kind"], "HandlerBinding")
        self.assertEqual(node["handler"]["body"]["kind"], "Pipeline")
        grouped = expression("(source |> value -> first(value)) |> second")
        self.assertEqual(grouped["input"]["kind"], "Group")
        self.assertEqual(grouped["handler"]["kind"], "FunctionTarget")

    def test_ordinary_expression_or_direct_call_is_not_a_handler(self):
        for text in ("source |> 42", "source |> transform()", "source? 1> value 2> 0"):
            with self.subTest(text=text), self.assertRaises(Diagnostic):
                expression(text)

    def test_parameter_handler_is_not_an_anonymous_function_ast(self):
        handler = expression("source |> value -> {value}")["handler"]
        self.assertEqual(handler["kind"], "HandlerBinding")
        self.assertEqual(handler["body"]["kind"], "Block")

    def test_legacy_retains_archived_handler_lists_and_wildcard_selectors(self):
        node = expression("source |> [first, second] -> first", "legacy")
        self.assertEqual([p["name"] for p in node["handler"]["parameters"]], ["first", "second"])
        branch = expression("token? {Token::Value[_] :> value -> value, _ :> _ -> 0,}", "legacy")
        self.assertEqual(branch["arms"][0]["selector"]["filters"][0]["kind"], "WildcardSelector")
        self.assertEqual(branch["arms"][1]["selector"]["kind"], "WildcardSelector")
        with self.assertRaises(Diagnostic):
            expression("token? {else :> fallback,}", "legacy")


if __name__ == "__main__":
    unittest.main()
