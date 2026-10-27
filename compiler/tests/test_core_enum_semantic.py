"""Reviewed enum construction, resource transfer and exact branch joins.

These are default-core tests, not archived-language success evidence. A zero
payload is genuinely absent, a Unit payload is one value, and an inline handler
does not create a return boundary. Runtime cleanup is verified separately by
the executable backend suite, not inferred from these static tests.
"""
import copy
import unittest

from compiler.xe_ast.backend_c import AnnotatedChecker
from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.parser import Parser, parse_source
from compiler.xe_ast.semantic import Checker, Value, check_source
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.typesys import I32, NEVER, STRING, UNIT, Type


def nodes(value):
    if isinstance(value, dict):
        if "kind" in value:
            yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


class CoreEnumSemanticTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "core-enum.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(d.render() for d in diagnostics))
        self.assertEqual(tree, original, "type and ownership metadata belong in side tables")
        return checker, tree

    def rejected(self, text, code=None, message=None):
        diagnostics = check_source(text, "core-enum.xe")
        self.assertTrue(diagnostics, text)
        if code is not None:
            self.assertEqual(diagnostics[0].code, code, diagnostics[0].render())
        if message is not None:
            self.assertIn(message, diagnostics[0].message)
        return diagnostics

    def test_zero_one_and_unit_payloads_construct_the_nominal_enum(self):
        checker, _ = self.checked('''enum Event { Number[i32], Tick[Unit], End, }
        fn number()->Event { Event::Number[42] }
        fn tick()->Event { Event::Tick[unit] }
        fn end()->Event { Event::End }
        fn main()->Unit {
            let first:Event << number();
            let second:Event << tick();
            let third:Event << end();
        }''')
        self.assertFalse(checker.copyable(Type("Event")), "scalar payloads do not silently derive Copy")

    def test_transparent_alias_preserves_construction_and_selector_identity(self):
        self.checked('''enum Event { Value[i32], End, }
        type Alias=Event;
        type Count=i32;
        fn inspect(event:Alias)->Count {
            event ? { Alias::Value :> value:Count -> value, Alias::End :> () -> 0, }
        }
        fn make()->Alias { Alias::Value[42] }''')

    def test_nested_owned_enum_payload_is_a_single_value(self):
        self.checked('''enum Inner { Text[String], End, }
        enum Outer { Item[Inner], End, }
        fn make()->Outer { Outer::Item[Inner::Text[String::from("owned")]] }
        fn main()->Unit { let message:Outer << make(); }''')

    def test_resource_construction_moves_its_payload(self):
        self.rejected('''enum Message { Text[String], End, }
        fn main()->Unit {
            let text:String << String::from("owned");
            let message:Message << Message::Text[text];
            println("{}",text@);
        }''', "XE-MOVE-0001")

    def test_enum_move_invalidates_the_old_binding(self):
        self.rejected('''enum Message { End, }
        fn take(message:Message)->Unit { }
        fn main()->Unit {
            let first:Message << Message::End;
            let second:Message << first;
            take(first);
        }''', "XE-MOVE-0001")

    def test_equal_sign_does_not_copy_an_enum_without_copy(self):
        self.rejected('''enum Event { End, }
        fn main()->Unit { let event:Event = Event::End; }''', "XE-OWN-0001")

    def test_copy_payload_is_preserved_by_constructor_value_passing(self):
        self.checked('''enum Event { Number[i32], End, }
        fn main()->Unit {
            let value:i32=42;
            let event:Event << Event::Number[value];
            println("{}",value);
        }''')

    def test_constructor_type_and_arity_errors_are_rejected(self):
        cases = ("Event::Number[true]", "Event::Number", "Event::End[unit]",
                 "Event::Tick", "Event::Missing", "Event::Number[1,2]")
        for expression in cases:
            with self.subTest(expression=expression):
                self.rejected(f'''enum Event {{ Number[i32], Tick[Unit], End, }}
                fn make()->Event {{ {expression} }}''')

    def test_distinct_enums_do_not_mix_even_with_identical_variants(self):
        self.rejected('''enum First { End, }
        enum Second { End, }
        fn make()->First { Second::End }''', "XE-TYPE-0001")

    def test_duplicate_and_unknown_payload_declarations_are_rejected(self):
        for declaration in ("enum Event { End, End, }", "enum Event { Value[Missing], }"):
            with self.subTest(declaration=declaration):
                self.rejected(declaration)

    def test_exhaustive_branch_consumes_the_whole_owned_input(self):
        self.rejected('''enum Event { Number[i32], End, }
        fn take(event:Event)->Unit { }
        fn inspect(event:Event)->Unit {
            let result:i32=event ? {
                Event::Number :> number:i32 -> number,
                Event::End :> () -> 0,
            };
            take(event);
        }''', "XE-MOVE-0001")

    def test_resource_payload_can_be_returned_without_being_copied(self):
        self.checked('''enum Message { Text[String], End, }
        fn unpack(message:Message)->String {
            message ? {
                Message::Text :> text:String -> text,
                Message::End :> () -> String::from("empty"),
            }
        }''')

    def test_handler_return_exits_outer_function_and_skips_normal_join(self):
        checker, tree = self.checked('''enum Event { Number[i32], End, }
        fn inspect(event:Event)->str {
            let value:i32=event ? {
                Event::Number :> number:i32 -> number,
                Event::End :> () -> { return "end"; },
            };
            "number"
        }''')
        branch = next(node for node in nodes(tree) if node["kind"] == "Branch")
        self.assertEqual(checker.expression_types[id(branch)], I32)
        body = branch["arms"][1]["handler"]["body"]
        self.assertEqual(checker.expression_types[id(body)], NEVER)

    def test_all_returning_arms_make_a_never_result(self):
        checker, tree = self.checked('''enum Event { First, Second, }
        fn inspect(event:Event)->i32 {
            event ? {
                Event::First :> () -> { return 1; },
                Event::Second :> () -> { return 2; },
            }
        }''')
        branch = next(node for node in nodes(tree) if node["kind"] == "Branch")
        self.assertEqual(checker.expression_types[id(branch)], NEVER)

    def test_handler_return_is_checked_against_outer_function_result(self):
        self.rejected('''enum Event { Number[i32], End, }
        fn inspect(event:Event)->str {
            let result:i32=event ? {
                Event::Number :> number:i32 -> number,
                Event::End :> () -> { return 7; },
            };
            "after"
        }''')

    def test_zero_payload_does_not_supply_unit_to_an_underscore(self):
        for handler in ("_ -> 0", "_:Unit -> 0", "value -> 0"):
            with self.subTest(handler=handler):
                self.rejected(f'''enum Event {{ End, }}
                fn inspect(event:Event)->i32 {{ event ? {{ Event::End :> {handler}, }} }}''',
                              "XE-CALL-0001")

    def test_unit_payload_is_not_zero_inputs(self):
        self.rejected('''enum Event { Tick[Unit], }
        fn inspect(event:Event)->i32 { event ? { Event::Tick :> () -> 0, } }''',
                      "XE-CALL-0001")
        self.checked('''enum Event { Tick[Unit], }
        fn inspect(event:Event)->i32 { event ? { Event::Tick :> _:Unit -> 0, } }''')

    def test_missing_and_duplicate_variant_arms_are_errors(self):
        self.rejected('''enum Event { First, Second, }
        fn inspect(event:Event)->i32 { event ? { Event::First :> () -> 0, } }''',
                      "XE-MATCH-0003")
        self.rejected('''enum Event { First, Second, }
        fn inspect(event:Event)->i32 { event ? {
            Event::First :> () -> 0,
            Event::First :> () -> 1,
            Event::Second :> () -> 2,
        } }''', "XE-MATCH-0002")

    def test_branch_results_need_exact_type_equality_not_numeric_identity_erasure(self):
        self.rejected('''enum Event { First, Second, }
        fn inspect(event:Event,first:i8,second:u8)->Unit {
            event ? {
                Event::First :> () -> first,
                Event::Second :> () -> second,
            };
        }''', "XE-TYPE-0001")

    def test_literal_join_keeps_range_evidence_until_the_receiver_types_it(self):
        self.checked('''enum Event { First, Second, }
        fn inspect(event:Event)->u8 {
            event ? { Event::First :> () -> 254, Event::Second :> () -> 255, }
        }''')
        self.rejected('''enum Event { First, Second, }
        fn inspect(event:Event)->u8 {
            event ? { Event::First :> () -> 254, Event::Second :> () -> 256, }
        }''', "XE-TYPE-0004")

    def test_no_normal_result_is_not_an_implicit_unit_result(self):
        self.rejected('''enum Event { First, Second, }
        fn inspect(event:Event)->i32 {
            event ? {
                Event::First :> () -> unit,
                Event::Second :> () -> { return 7; },
            }
        }''', "XE-TYPE-0001")

    def test_a_later_typed_peer_can_supply_literal_context_without_a_default(self):
        self.checked('''enum Event { First, Second, }
        fn inspect(event:Event,value:u8)->Unit {
            event ? { Event::First :> () -> 42, Event::Second :> () -> value, };
        }''')
        self.rejected('''enum Event { First, Second, }
        fn inspect(event:Event,value:u8)->Unit {
            event ? { Event::First :> () -> 256, Event::Second :> () -> value, };
        }''', "XE-TYPE-0004")
        self.rejected('''enum Event { First, Second, }
        fn inspect(event:Event)->Unit {
            event ? { Event::First :> () -> 1, Event::Second :> () -> 2, };
        }''', "XE-CORE-0001")

    def test_branch_results_do_not_merge_pointer_permissions(self):
        self.rejected('''enum Event { First, Second, }
        fn inspect(event:Event,write:i32@[mut],read:i32@)->i32@ {
            event ? {
                Event::First :> () -> write,
                Event::Second :> () -> read,
            }
        }''', "XE-TYPE-0001")

    def test_identical_mut_pointer_results_can_weaken_at_the_outer_receiver(self):
        self.checked('''enum Event { First, Second, }
        fn inspect(event:Event,first:i32@[mut],second:i32@[mut])->i32@ {
            event ? {
                Event::First :> () -> first,
                Event::Second :> () -> second,
            }
        }''')

    def test_zero_or_one_named_targets_use_function_signatures(self):
        self.checked('''enum Event { Number[i32], End, }
        fn identity(value:i32)->i32 { value }
        fn zero()->i32 { 0 }
        fn inspect(event:Event)->i32 {
            event ? { Event::Number :> identity, Event::End :> zero, }
        }''')

    def test_unreviewed_branch_extensions_stay_closed(self):
        branches = (
            "Event::First | Event::Second :> () -> 0,",
        )
        for arms in branches:
            with self.subTest(arms=arms):
                self.rejected(f'''enum Event {{ First, Second, }}
                fn inspect(event:Event)->i32 {{ event ? {{ {arms} }} }}''')

    def test_reviewed_final_else_handles_the_remaining_zero_payload_variant(self):
        self.checked('''enum Event { First, Second, }
        fn inspect(event:Event)->i32 {
            event ? { Event::First :> () -> 0, else :> () -> 1, }
        }''')

    def test_enum_custom_drop_is_blocked_even_via_alias_in_direct_collection(self):
        text = '''enum Event { End, }
        type Alias=Event;
        impl Drop for Alias { fn drop(self:Self@[mut])->Unit { } }'''
        # Collect bypasses public admission for this component regression. The
        # checker must enforce the enum rule after expanding the target alias.
        with language_profile("legacy"):
            source = Source(text, "enum-drop.xe")
            tree = Parser(source).module()
        with self.assertRaises(Diagnostic) as caught:
            Checker(source, tree).collect()
        self.assertEqual(caught.exception.code, "XE-OWN-0002")
        self.assertIn("enum", caught.exception.message)

    def test_zero_handler_component_accepts_only_genuinely_zero_inputs(self):
        source = Source("fn probe()->i32 { 0 }", "zero-handler.xe")
        tree = Parser(source).module()
        checker = Checker(source, tree)
        checker.collect()
        checker.scopes = [{}]
        body = tree["items"][0]["body"]["tail"]
        handler = {"kind": "HandlerBinding", "span": body["span"],
                   "parameters": [], "body": body}
        self.assertEqual(checker.handle(handler, [], I32).type, I32)
        for type_ in (I32, UNIT, STRING):
            with self.subTest(type_=type_), self.assertRaises(Diagnostic) as caught:
                checker.handle(handler, [Value(type_, body)], I32)
            self.assertEqual(caught.exception.code, "XE-CALL-0001")


if __name__ == "__main__":
    unittest.main()
