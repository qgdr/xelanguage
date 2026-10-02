"""数据声明的专门回归：既检查 AST 形状，也检查对应语义/能力边界。"""
import unittest
from pathlib import Path

from compiler.xe_ast import check_source, parse_source

ROOT = Path(__file__).resolve().parents[2]


def fixture(relative):
    path = ROOT / "tests" / relative
    return path.read_text(encoding="utf-8"), str(path)


class DataDeclarationTests(unittest.TestCase):
    def test_struct_creation_fields_preserve_copy_and_move(self):
        text, path = fixture("language/struct_create.xe")
        tree = parse_source(text, path)
        main = next(item for item in tree["items"] if item.get("name") == "main")
        bindings = {s["name"]: s for s in main["body"]["statements"] if s["kind"] == "Binding"}
        point = bindings["point"]["value"]
        self.assertEqual(point["kind"], "StructLiteral")
        self.assertEqual([(f["name"], f["operator"]) for f in point["fields"]],
                         [("y", "="), ("x", "=")])
        message = bindings["message"]["value"]
        self.assertEqual([(f["name"], f["operator"]) for f in message["fields"]],
                         [("point", "="), ("text", "<<")])
        self.assertEqual(bindings["empty"]["value"]["fields"], [])
        self.assertEqual(check_source(text, path), [])
        self.assertEqual(check_source(text, path, True), [])

    def test_method_receiver_shapes_and_associated_function(self):
        text, path = fixture("language/struct_methods.xe")
        tree = parse_source(text, path)
        implementations = {item["target"]["path"]["parts"][0]: item for item in tree["items"]
                           if item["kind"] == "Impl" and item["trait"] is None}
        counter = {m["name"]: m for m in implementations["Counter"]["methods"]}
        self.assertEqual(counter["new"]["parameters"][0]["name"], "count")
        shared = counter["get"]["parameters"][0]["type"]
        writable = counter["add"]["parameters"][0]["type"]
        self.assertEqual(shared["kind"], "PointerType")
        self.assertEqual(shared["target"]["path"]["parts"], ["Self"])
        self.assertEqual(shared["modifiers"], [])
        self.assertEqual([m["name"] for m in writable["modifiers"]], ["mut"])
        owning = next(m for m in implementations["Message"]["methods"] if m["name"] == "into_text")
        self.assertEqual(owning["parameters"][0]["type"]["kind"], "NamedType")
        self.assertEqual(check_source(text, path, True), [])

    def test_copy_and_drop_trait_implementation_shapes(self):
        text, path = fixture("language/trait_copy_drop.xe")
        tree = parse_source(text, path)
        implementations = [item for item in tree["items"] if item["kind"] == "Impl"]
        self.assertEqual([i["trait"]["path"]["parts"] for i in implementations],
                         [["Copy"], ["Copy"], ["Drop"]])
        self.assertEqual(implementations[0]["methods"], [])
        drop = implementations[-1]["methods"][0]
        self.assertEqual(drop["name"], "drop")
        self.assertEqual(drop["parameters"][0]["name"], "self")
        self.assertIsNone(drop["result"])
        self.assertEqual(check_source(text, path, True), [])

    def test_custom_trait_is_parsed_and_statically_checked(self):
        text, path = fixture("language/trait_dispatch.xe")
        tree = parse_source(text, path)
        trait = tree["items"][0]
        self.assertEqual(trait["kind"], "Trait")
        self.assertEqual(trait["name"], "Measure")
        self.assertIsNone(trait["methods"][0]["body"])
        implementation = tree["items"][2]
        self.assertEqual(implementation["trait"]["path"]["parts"], ["Measure"])
        self.assertEqual(implementation["target"]["path"]["parts"], ["Point"])
        self.assertEqual(implementation["methods"][0]["body"]["tail"]["kind"], "FieldAccess")
        self.assertEqual(check_source(text, path, True), [])
