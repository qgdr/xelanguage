"""Loop label syntax extends schema 1 without changing unlabelled AST nodes."""
import hashlib
import json
import unittest

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.ast import document
from compiler.xe_ast.lexer import Lexer
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Diagnostic, Source


class LoopLabelParserTests(LegacyTestCase):
    def loop(self, expression):
        return parse_source("fn main() { " + expression + " }")["items"][0]["body"]["tail"]

    def rejected(self, statement, message):
        with self.assertRaises(Diagnostic) as caught:
            parse_source("fn main() { " + statement + " }")
        self.assertEqual(caught.exception.code, "XE-PARSE-0001")
        self.assertIn(message, caught.exception.message)
        return caught.exception

    def test_while_label_is_an_optional_string(self):
        loop = self.loop("while[label = outer] ready {}")
        self.assertEqual(loop["kind"], "While")
        self.assertEqual(loop["label"], "outer")
        self.assertEqual(loop["condition"]["path"]["parts"], ["ready"])
        self.assertEqual(set(loop), {"kind", "span", "condition", "body", "label"})

    def test_for_label_keeps_the_element_annotation_and_source(self):
        loop = self.loop("for[label = outer] value: i32 in 1..4 {}")
        self.assertEqual(loop["kind"], "For")
        self.assertEqual(loop["label"], "outer")
        self.assertEqual(loop["name"], "value")
        self.assertEqual(loop["type"]["path"]["parts"], ["i32"])
        self.assertEqual(loop["source"]["kind"], "Range")
        self.assertEqual(set(loop), {"kind", "span", "name", "type", "source", "body", "label"})

    def test_break_and_continue_targets_are_optional_strings(self):
        loop = self.loop("while[label = outer] ready { break[outer]; continue[outer]; }")
        for node, kind in zip(loop["body"]["statements"], ("Break", "Continue"), strict=True):
            with self.subTest(kind=kind):
                self.assertEqual(node["kind"], kind)
                self.assertEqual(node["target"], "outer")
                self.assertIsNone(node["value"])
                self.assertEqual(set(node), {"kind", "span", "value", "target"})

    def test_label_remains_an_ordinary_identifier_outside_the_attachment(self):
        source = "fn label(label: i32) -> i32 { label } fn main() { let label = 1; label; }"
        tokens = [token for token in Lexer(Source(source)).scan() if token.text == "label"]
        self.assertGreater(len(tokens), 3)
        self.assertTrue(all(token.kind == "IDENT" for token in tokens))
        self.assertEqual(parse_source(source)["items"][0]["name"], "label")
        loop = self.loop("for[label = label] value in label { break[label]; }")
        self.assertEqual(loop["label"], "label")
        self.assertEqual(loop["source"]["path"]["parts"], ["label"])

    def test_labels_accept_the_existing_unicode_and_identifier_rules(self):
        for label in ("outer2", "_outer", "外层"):
            with self.subTest(label=label):
                loop = self.loop("while[label = " + label + "] ready { continue[" + label + "]; }")
                self.assertEqual(loop["label"], label)
                self.assertEqual(loop["body"]["statements"][0]["target"], label)

    def test_nested_loop_nodes_preserve_the_written_target(self):
        outer = self.loop("while[label = outer] ready { "
                          "for[label = inner] value in values { break[outer]; continue[inner]; } }")
        inner = outer["body"]["tail"]
        self.assertEqual((outer["label"], inner["label"]), ("outer", "inner"))
        self.assertEqual([node["target"] for node in inner["body"]["statements"]], ["outer", "inner"])

    def test_label_target_resolution_is_left_to_the_semantic_phase(self):
        # Pure parsing records names; it does not establish their scope.
        loop = self.loop("while[label = same] ready { "
                         "while[label = same] ready { break[unknown]; } }")
        inner = loop["body"]["tail"]
        self.assertEqual(inner["label"], "same")
        self.assertEqual(inner["body"]["statements"][0]["target"], "unknown")

    def test_labelled_condition_blocks_can_contain_targeted_jumps(self):
        loop = self.loop("while[label = outer] { break[outer]; } {}")
        self.assertEqual(loop["condition"]["kind"], "Block")
        self.assertEqual(loop["condition"]["statements"][0]["target"], "outer")

    def test_array_conditions_are_not_mistaken_for_label_attachments(self):
        for source, name in (("while[ready][0] {}", "ready"),
                             ("while[label][0] {}", "label")):
            with self.subTest(source=source):
                loop = self.loop(source)
                self.assertNotIn("label", loop)
                condition = loop["condition"]
                self.assertEqual(condition["kind"], "BracketApply")
                self.assertEqual(condition["object"]["kind"], "Array")
                self.assertEqual(condition["object"]["elements"][0]["path"]["parts"], [name])

    def test_labelled_loops_can_still_use_array_conditions_or_sources(self):
        loop = self.loop("while[label = outer][ready][0] { break[outer]; }")
        self.assertEqual(loop["label"], "outer")
        self.assertEqual(loop["condition"]["kind"], "BracketApply")
        loop = self.loop("for[label = outer] value in [1, 2] { continue[outer]; }")
        self.assertEqual(loop["source"]["kind"], "Array")

    def test_unlabelled_loops_and_jumps_have_exactly_the_old_fields(self):
        body = parse_source("fn main() { while ready { break; continue; } "
                            "for value in values { break; continue; } }")["items"][0]["body"]
        loops = [body["statements"][0]["expression"], body["tail"]]
        for loop in loops:
            with self.subTest(kind=loop["kind"]):
                self.assertNotIn("label", loop)
                for jump in loop["body"]["statements"]:
                    self.assertEqual(set(jump), {"kind", "span", "value"})
                    self.assertIsNone(jump["value"])

    def test_unlabelled_ast_serialization_matches_the_prechange_snapshots(self):
        # Captured from the parser before adding label/target handling. This
        # includes field insertion order, spans, and condition/source shapes.
        for source, digest in (
            ("fn main() { while ready { break; continue; } for value in values { break; continue; } }",
             "4bf2b5c02f358774e56825c92a46290a9190266e0369be5f305cea2f1533b4ed"),
            ("fn main() { while[ready][0] { break; } while[label][0] { continue; } }",
             "4f35ad1d2ee86c23a36e2e08a11926151a4dba4197c19f7aa498bf05fac39547"),
            ("fn main() { while { break; } {} for value: i32 in values { continue; } }",
             "a714815b41ee04320b416d98de09a88d1b33aa8f54ecd0f4d0b1c6eec2dfae6f"),
        ):
            with self.subTest(source=source):
                encoded = json.dumps(parse_source(source), ensure_ascii=False, separators=(",", ":")).encode()
                self.assertEqual(hashlib.sha256(encoded).hexdigest(), digest)

    def test_schema_one_serializes_only_present_label_and_target_strings(self):
        source = "fn main() { while[label = outer] ready { while ready { break; } break[outer]; } }"
        tree = parse_source(source)
        envelope = document(Source(source), tree)
        self.assertEqual(envelope["schema_version"], 1)
        decoded = json.loads(json.dumps(envelope))
        loop = decoded["ast"]["items"][0]["body"]["tail"]
        self.assertEqual(loop["label"], "outer")
        inner = loop["body"]["statements"][0]["expression"]
        self.assertNotIn("label", inner)
        self.assertNotIn("target", inner["body"]["statements"][0])
        self.assertEqual(loop["body"]["statements"][1]["target"], "outer")

    def test_unicode_spans_include_label_and_target_attachments(self):
        source = "// 标记\r\nfn main() { while[label = 外层] ready { break[外层]; } }"
        loop = parse_source(source)["items"][0]["body"]["tail"]
        jump = loop["body"]["statements"][0]
        self.assertEqual(source[loop["span"]["start"]["offset"]:loop["span"]["end"]["offset"]],
                         "while[label = 外层] ready { break[外层]; }")
        self.assertEqual(source[jump["span"]["start"]["offset"]:jump["span"]["end"]["offset"]], "break[外层];")
        self.assertEqual(jump["span"]["start"]["offset"], source.index("break"))
        self.assertEqual(jump["span"]["start"]["line"], 2)

    def test_malformed_label_attachments_produce_parse_diagnostics(self):
        for attachment in (
            "[label = ]", "[label = 1]", '[label = "outer"]', "[label = 'x']",
            "[label = _]", "[label = self]", "[label = while]",
            "[label = outer, inner]", "[label = outer,]",
            "[label = outer, label = inner]", "[label = outer + inner]",
            "[label = outer::inner]", "[label = outer][label = inner]",
        ):
            for kind, suffix in (("while", " ready {}"), ("for", " value in values {}")):
                with self.subTest(kind=kind, attachment=attachment):
                    self.rejected(kind + attachment + suffix, "循环标签")

    def test_malformed_target_attachments_produce_parse_diagnostics(self):
        for attachment in (
            "[]", "[1]", '["outer"]', "['x']", "[_]", "[self]", "[while]",
            "[outer, inner]", "[outer,]", "[outer + inner]", "[outer::inner]",
            "[label = outer]", "[outer][inner]",
        ):
            for kind in ("break", "continue"):
                with self.subTest(kind=kind, attachment=attachment):
                    self.rejected(kind + attachment + ";", "循环跳转")

    def test_truncated_attachments_and_missing_semicolons_are_diagnostics(self):
        for statement in (
            "while[label =", "for[label =", "while[label = outer", "for[label = outer",
            "break[", "continue[", "break[outer", "continue[outer", "break[outer]", "continue[outer]",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(Diagnostic) as caught:
                    parse_source("fn main() { " + statement)
                self.assertEqual(caught.exception.code, "XE-PARSE-0001")


if __name__ == "__main__":
    unittest.main()
