"""Reviewed enum/handler syntax, independently of runtime admission.

Parser.module is intentionally a component entry point. Public else admission
is verified by test_core_else_*; these component tests alone do not prove it,
or enable the other still-gated branch extensions.
"""
import unittest

from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.lexer import Lexer
from compiler.xe_ast.parser import Parser
from compiler.xe_ast.source import Diagnostic, Source


def module(text, profile="core"):
    with language_profile(profile):
        return Parser(Source(text, "enum-parser.xe")).module()


def expression(text, profile="core"):
    return module(f"fn inspect()->Unit{{{text}}}", profile)["items"][0]["body"]["tail"]


class CoreEnumParserTests(unittest.TestCase):
    def test_enum_signatures_distinguish_zero_from_one_unit(self):
        node = module("enum Event {Number[i32], Tick[Unit], End,}")["items"][0]
        self.assertEqual(node["kind"], "Enum")
        self.assertEqual(node["name"], "Event")
        self.assertEqual(node["generics"], [])
        self.assertEqual([(v["name"], len(v["payload"])) for v in node["variants"]],
                         [("Number", 1), ("Tick", 1), ("End", 0)])
        self.assertEqual(node["variants"][1]["payload"][0]["path"]["parts"], ["Unit"])

    def test_optional_final_enum_comma_is_core_only(self):
        for text in ("enum Event {End}", "enum Event {Number[i32]}"):
            with self.subTest(text=text):
                self.assertEqual(len(module(text)["items"][0]["variants"]), 1)
                with self.assertRaises(Diagnostic):
                    module(text, "legacy")
        self.assertEqual(module("enum Event {End,}", "legacy")["items"][0]["name"], "Event")

    def test_payload_construction_remains_attachment_not_call(self):
        constructed = expression("Event::Number[42]")
        self.assertEqual(set(constructed), {"kind", "span", "object", "arguments"})
        self.assertEqual(constructed["kind"], "BracketApply")
        self.assertEqual(constructed["object"]["path"]["parts"], ["Event", "Number"])
        self.assertEqual(constructed["arguments"][0]["value"], 42)
        empty = expression("Event::End")
        self.assertEqual(empty["kind"], "Name")
        self.assertEqual(empty["path"]["parts"], ["Event", "End"])

    def test_empty_payload_attachment_is_not_zero_payload(self):
        for text in ("enum Event {End[],}", "fn inspect()->Unit{Event::End[]}"):
            with self.subTest(text=text), self.assertRaises(Diagnostic):
                module(text)

    def test_zero_input_handler_is_explicit_and_not_a_function_ast(self):
        for text in ("event? {Event::End :> () -> 0,}",
                     "event? {Event::End :> () -> {return 7;},}"):
            with self.subTest(text=text):
                handler = expression(text)["arms"][0]["handler"]
                self.assertEqual(handler["kind"], "HandlerBinding")
                self.assertEqual(handler["parameters"], [])
        body = expression("event? {Event::End :> () -> {return 7;},}")["arms"][0]["handler"]["body"]
        self.assertEqual(body["statements"][0]["kind"], "Return")

    def test_empty_input_head_does_not_enable_empty_expressions_or_direct_blocks(self):
        for text in ("()", "let value = ();", "event? {Event::End :> {0},}",
                     "event? {Event::End :> (),}", "event? {Event::End :> [] -> 0,}"):
            with self.subTest(text=text), self.assertRaises(Diagnostic):
                expression(text)

    def test_zero_input_head_can_parse_in_any_handler_position_but_arity_is_static(self):
        self.assertEqual(expression("value |> () -> 0")["handler"]["parameters"], [])
        branch = expression("value? 1> () -> 1 2> () -> 2")
        self.assertEqual([arm["handler"]["parameters"] for arm in branch["arms"]], [[], []])

    def test_zero_input_head_does_not_change_legacy(self):
        with self.assertRaises(Diagnostic):
            expression("event? {Event::End :> () -> 0,}", "legacy")
        handler = expression("source |> _ -> 0", "legacy")["handler"]
        self.assertEqual(handler["parameters"][0]["name"], "_")

    def test_whole_arrow_and_compatibility_form_have_same_distinct_ast(self):
        for text in ("event? {else => original: Event -> 0,}",
                     "event? {else :>[whole] original: Event -> 0,}"):
            with self.subTest(text=text):
                arm = expression(text)["arms"][0]
                self.assertEqual(set(arm), {"kind", "span", "selector", "handler"})
                self.assertEqual(arm["kind"], "WholeBranchArm")
                self.assertEqual(arm["selector"]["kind"], "ElseSelector")
                self.assertEqual(arm["handler"]["parameters"][0]["name"], "original")
        arm = expression("event? {Event::End :> () -> 0,}")["arms"][0]
        self.assertEqual(arm["kind"], "BranchArm")
        self.assertEqual(set(arm), {"kind", "span", "selector", "handler"})

    def test_whole_arrow_is_not_enabled_for_named_variants_or_bare_results(self):
        for text in ("event? {Event::End => _ -> 0,}",
                     "event? {Event::End :>[whole] _ -> 0,}",
                     "event? {else => 0,}", "event? {else :>[whole,mut] _ -> 0,}"):
            with self.subTest(text=text), self.assertRaises(Diagnostic):
                expression(text)

    def test_whole_arrow_longest_match_is_core_only(self):
        text = "=> >= -> :> == = >"
        with language_profile("core"):
            self.assertEqual([t.kind for t in Lexer(Source(text)).scan()],
                             ["=>", ">=", "->", ":>", "==", "=", ">", "EOF"])
        with language_profile("legacy"):
            self.assertEqual([t.kind for t in Lexer(Source("=>")).scan()], ["=", ">", "EOF"])
            with self.assertRaises(Diagnostic):
                expression("event? {else => fallback,}", "legacy")

    def test_handler_and_whole_arm_preserve_source_spans(self):
        text = "fn inspect()->Unit{event? {else :>[whole] original -> 0,}}"
        tree = module(text)
        arm = tree["items"][0]["body"]["tail"]["arms"][0]
        start = arm["span"]["start"]["offset"]
        end = arm["span"]["end"]["offset"]
        self.assertEqual(text[start:end], "else :>[whole] original -> 0,")


if __name__ == "__main__":
    unittest.main()
