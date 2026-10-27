"""DEC-024: ignoring a source name does not erase type or ownership checks."""
import copy
import json
import unittest

from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import check_source
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.typesys import STRING, Type


def nodes(value):
    if isinstance(value, dict):
        if "kind" in value:
            yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


class CoreDiscardSemanticTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "discard-owned.xe")
        tree = parse_source(text, source.filename)
        # Metadata remains in side tables; JSON is neither a permit nor a
        # chance to quietly change the inferred type of an ignored input.
        tree = json.loads(json.dumps(document(source, tree)))["ast"]
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(error.render() for error in diagnostics))
        CBackend(checker).generate()
        self.assertEqual(tree, original)
        return checker, tree

    def rejected(self, text, code):
        errors = check_source(text, "discard-owned.xe")
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def test_typed_and_untyped_resource_inputs_retain_their_actual_type(self):
        for parameter in ("_", "_:String"):
            with self.subTest(parameter=parameter):
                checker, tree = self.checked(f'''fn main()->Unit {{
                    String::from("owned") |> {parameter} -> unit;
                }}''')
                ignored = next(n for n in nodes(tree) if n["kind"] == "Parameter" and n["name"] == "_")
                self.assertEqual(checker.binding_types[id(ignored)], STRING)
                self.assertEqual(checker.inferred_types[id(ignored)]["type"], "String")
                self.assertNotIn("name", checker.inferred_types[id(ignored)])

    def test_enum_input_is_a_whole_owned_value_even_when_its_variant_is_empty(self):
        checker, tree = self.checked('''enum Event { Text[String], End, }
        fn main()->Unit {
            Event::End |> _ -> unit;
            Event::Text[String::from("text")] |> _:Event -> unit;
        }''')
        parameters = [n for n in nodes(tree) if n["kind"] == "Parameter" and n["name"] == "_"]
        self.assertEqual([checker.binding_types[id(p)] for p in parameters], [Type("Event"), Type("Event")])

    def test_alias_annotations_do_not_change_resource_identity(self):
        self.checked('''type Text=String;
        enum Event { TextValue[Text], End, }
        fn inspect(event:Event)->Unit {
            event ? { Event::TextValue :> _:Text -> unit, Event::End :> () -> unit, }
        }
        fn main()->Unit { inspect(Event::TextValue[String::from("alias")]); }''')

    def test_owned_sources_are_moved_into_an_ignored_input(self):
        for handler in ("_", "_:String"):
            with self.subTest(handler=handler):
                self.rejected(f'''fn main()->Unit {{
                    let text:String << String::from("owned");
                    text |> {handler} -> unit;
                    println("{{}}",text@);
                }}''', "XE-MOVE-0001")

    def test_by_value_enum_match_still_consumes_its_source(self):
        self.rejected('''enum Event { Text[String], End, }
        fn take(event:Event)->Unit {}
        fn inspect(event:Event)->Unit {
            event ? { Event::Text :> _ -> unit, Event::End :> () -> unit, };
            take(event);
        }''', "XE-MOVE-0001")

    def test_ignoring_copy_values_and_nonowning_pointers_preserves_the_source(self):
        self.checked('''fn main()->Unit {
            let number:i32=42;
            number |> _ -> unit;
            println("{}",number);
            let text:String << String::from("owned");
            text@ |> _:String@ -> unit;
            println("{}",text@);
        }''')

    def test_underscore_cannot_acquire_ownership_from_a_resource_pointer(self):
        for handler in ("_", "_:String"):
            with self.subTest(handler=handler):
                self.rejected(f'''fn consume(pointer:String@)->Unit {{
                    pointer# |> {handler} -> unit;
                }}''', "XE-MOVE-0002")

    def test_type_annotations_are_checked_even_when_the_name_is_ignored(self):
        self.rejected('''fn main()->Unit {
            String::from("owned") |> _:bool -> unit;
        }''', "XE-TYPE-0001")
        self.rejected('''enum Event { Text[String], }
        fn inspect(event:Event)->Unit { event ? { Event::Text :> _:i32 -> unit, } }''', "XE-TYPE-0001")

    def test_zero_input_is_still_not_one_ignored_resource_or_unit(self):
        self.rejected('''enum Event { End, }
        fn inspect(event:Event)->Unit { event ? { Event::End :> _ -> unit, } }''', "XE-CALL-0001")
        self.rejected('''enum Event { Text[String], }
        fn inspect(event:Event)->Unit { event ? { Event::Text :> () -> unit, } }''', "XE-CALL-0001")

    def test_ignored_input_is_never_readable_even_from_a_handcrafted_ast(self):
        text = 'fn main()->Unit { String::from("owned") |> _ -> unit; }'
        tree = parse_source(text)
        handler = tree["items"][0]["body"]["statements"][0]["expression"]["handler"]
        span = handler["body"]["span"]
        handler["body"] = {"kind": "Name", "span": span,
                           "path": {"kind": "Path", "span": span, "parts": ["_"]}}
        with self.assertRaises(Diagnostic):
            document(Source(text), tree)


if __name__ == "__main__":
    unittest.main()
