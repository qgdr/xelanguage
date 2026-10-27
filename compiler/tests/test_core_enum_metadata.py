"""Shared enum metadata validates signatures without deriving capabilities."""
import copy
import unittest
from typing import Any, NoReturn

from compiler.xe_ast.core_enums import validate_enum_declaration
from compiler.xe_ast.parser import Parser
from compiler.xe_ast.source import Diagnostic, Source

FIELDS = {"Enum": {"public", "name", "generics", "variants"},
          "VariantDeclaration": {"name", "payload"}}


class CoreEnumMetadataTests(unittest.TestCase):
    def setUp(self):
        self.source = Source("enum Event {Number[i32], Tick[Unit], End,}", "enum-metadata.xe")
        self.node = Parser(self.source).module()["items"][0]
        self.resolved = []

    def reject(self, node, message) -> NoReturn:
        raise Diagnostic(self.source, 0, 1, message, "XE-CORE-0001")

    def shape(self, node: Any, expected: str | None = None) -> str:
        if not isinstance(node, dict) or node.get("kind") != expected:
            self.reject(node, "AST 节点类别错误")
        assert expected is not None
        fields = FIELDS[expected]
        if fields - set(node) or set(node) - fields - {"kind", "span"}:
            self.reject(node, "AST 字段不匹配")
        return expected

    def name(self, node):
        value = node["name"]
        if not isinstance(value, str) or not value.isascii() or not value.isidentifier() or value == "_":
            self.reject(node, "需要普通名称")
        return value

    def resolve_payload(self, node):
        self.resolved.append(node)
        return node["path"]["parts"][0]

    def validate(self, node=None):
        return validate_enum_declaration(
            self.node if node is None else node, shape=self.shape, name=self.name,
            resolve_payload=self.resolve_payload, fail=self.reject)

    def test_signatures_keep_zero_and_unit_distinct_without_capability_flags(self):
        before = copy.deepcopy(self.node)
        self.assertEqual(self.validate(), {"Number": ("i32",), "Tick": ("Unit",), "End": ()})
        self.assertEqual(len(self.resolved), 2)
        self.assertEqual(self.node, before)
        self.assertNotIn("copy", self.node)
        self.assertNotIn("drop", self.node)

    def test_parent_controls_which_payload_types_are_resolvable(self):
        node = Parser(Source("enum Message {Text[String], End,}")).module()["items"][0]
        self.assertEqual(self.validate(node), {"Text": ("String",), "End": ()})

    def test_unreviewed_empty_generic_or_multiple_payload_shapes_are_rejected(self):
        for text in ("enum Empty {}", "enum[T] Generic {Value[T],}",
                     "enum Pair {Value[i32, i32],}", "enum Dup {End, End,}"):
            with self.subTest(text=text):
                node = Parser(Source(text)).module()["items"][0]
                with self.assertRaises(Diagnostic):
                    self.validate(node)

    def test_non_list_or_missing_fields_are_not_defaulted(self):
        mutations = [lambda n: n.pop("generics"), lambda n: n.update(generics=None),
                     lambda n: n.update(variants=None),
                     lambda n: n["variants"][0].pop("payload"),
                     lambda n: n["variants"][0].update(payload=None),
                     lambda n: n["variants"][0].update(copy=True),
                     lambda n: n.update(drop=False),
                     lambda n: n["variants"][0].update(kind="FutureVariant"),
                     lambda n: n["variants"][0].update(name="_")]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                node = copy.deepcopy(self.node)
                mutation(node)
                with self.assertRaises(Diagnostic):
                    self.validate(node)


if __name__ == "__main__":
    unittest.main()
