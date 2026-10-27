"""Reviewed else interfaces, coverage and ownership under the default core.

These tests exercise public parsing/checking and the direct AST boundary. They
do not infer runtime cleanup correctness from a successful type check; the
separate executable suite verifies generated code and destruction order.
"""
import copy
import json
import unittest

from compiler.xe_ast.admission import validate_core_source
from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import AnnotatedChecker
from compiler.xe_ast.parser import Parser, parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.typesys import I32, NEVER, Type


def nodes(value):
    if isinstance(value, dict):
        if "kind" in value:
            yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


class CoreElseSemanticTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "core-else.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(d.render() for d in diagnostics))
        self.assertEqual(tree, original, "coverage/type plans must remain in side tables")
        self.assertEqual(check_source(text, source.filename), [])
        return checker, tree

    def rejected(self, text, code):
        diagnostics = check_source(text, "core-else.xe")
        self.assertTrue(diagnostics, text)
        self.assertEqual(diagnostics[0].code, code, diagnostics[0].render())
        self.assertEqual(diagnostics[0].severity, "error")
        # Parser.module alone is not admission evidence. A caller that already
        # holds its AST must receive the same contract error from Checker.
        source = Source(text, "core-else-direct.xe")
        tree = Parser(source).module()
        original = copy.deepcopy(tree)
        direct = Checker(source, tree).check()
        self.assertTrue(direct, text)
        self.assertEqual(direct[0].code, code, direct[0].render())
        self.assertEqual(tree, original)
        return diagnostics[0]

    def test_same_payload_type_and_transparent_alias_share_one_input(self):
        for handler in ("number -> number", "number:i32 -> number", "identity"):
            with self.subTest(handler=handler):
                self.checked(f'''type Count=i32;
                enum Event {{ Number[Count], Code[i32], End, }}
                fn identity(value:i32)->i32 {{ value }}
                fn inspect(event:Event)->i32 {{ event ? {{
                    Event::End :> () -> 0,
                    else :> {handler},
                }} }}''')

    def test_zero_payload_remainder_uses_zero_handler_or_zero_function(self):
        for handler in ("() -> 0", "zero"):
            with self.subTest(handler=handler):
                self.checked(f'''enum Event {{ Number[i32], End, Closed, }}
                fn zero()->i32 {{ 0 }}
                fn inspect(event:Event)->i32 {{ event ? {{
                    Event::Number :> number:i32 -> number,
                    else :> {handler},
                }} }}''')

    def test_unit_payload_remains_one_input_not_a_zero_payload(self):
        self.checked('''enum Event { Tick[Unit], Tock[Unit], End, }
        fn inspect(event:Event)->i32 { event ? {
            Event::End :> () -> 0,
            else :> _:Unit -> 1,
        } }''')
        self.rejected('''enum Event { Tick[Unit], Tock[Unit], }
        fn inspect(event:Event)->i32 { event ? { else :> () -> 0, } }''',
                      "XE-CALL-0001")

    def test_mixed_machine_types_reject_before_handler_can_ignore_or_filter(self):
        for handler in ("_ -> 0", "_:i32 -> 0", "number:i32 -> number", "identity"):
            with self.subTest(handler=handler):
                error = self.rejected(f'''enum Event {{ Byte[u8], Number[i32], }}
                fn identity(value:i32)->i32 {{ value }}
                fn inspect(event:Event)->i32 {{ event ? {{ else :> {handler}, }} }}''',
                                      "XE-MATCH-0004")
                self.assertIn("Byte", error.render())
                self.assertIn("Number", error.render())

    def test_different_nominal_payloads_do_not_merge_on_identical_layout(self):
        self.rejected('''enum First { End, }
        enum Second { End, }
        enum Event { Left[First], Right[Second], }
        fn inspect(event:Event)->i32 { event ? { else :> _ -> 0, } }''',
                      "XE-MATCH-0004")

    def test_zero_and_unit_signatures_do_not_merge_even_when_ignored(self):
        for handler in ("_ -> 0", "_:Unit -> 0", "() -> 0", "zero"):
            with self.subTest(handler=handler):
                self.rejected(f'''enum Event {{ End, Tick[Unit], }}
                fn zero()->i32 {{ 0 }}
                fn inspect(event:Event)->i32 {{ event ? {{ else :> {handler}, }} }}''',
                              "XE-MATCH-0004")

    def test_single_signature_still_checks_handler_type_and_arity(self):
        for handler, code in (("_:bool -> 0", "XE-TYPE-0001"),
                              ("() -> 0", "XE-CALL-0001"),
                              ("zero", "XE-CALL-0001"),
                              ("wrong", "XE-TYPE-0001")):
            with self.subTest(handler=handler):
                self.rejected(f'''enum Event {{ First[i32], Second[i32], }}
                fn zero()->i32 {{ 0 }}
                fn wrong(value:bool)->i32 {{ 0 }}
                fn inspect(event:Event)->i32 {{ event ? {{ else :> {handler}, }} }}''', code)
        for handler in ("_ -> 0", "value -> 0", "identity"):
            with self.subTest(zero_handler=handler):
                self.rejected(f'''enum Event {{ First, Second, }}
                fn identity(value:i32)->i32 {{ value }}
                fn inspect(event:Event)->i32 {{ event ? {{ else :> {handler}, }} }}''',
                              "XE-CALL-0001")

    def test_all_three_spellings_are_admitted_with_a_nonempty_remainder(self):
        for arm in ("else :> () -> 0", "else => original:Event -> 0",
                    "else :>[whole] original:Event -> 0"):
            with self.subTest(arm=arm):
                self.checked(f'''enum Event {{ First, Second, }}
                fn inspect(event:Event)->i32 {{ event ? {{
                    Event::First :> () -> 1, {arm},
                }} }}''')

    def test_else_must_be_last_regardless_of_delivery_mode(self):
        for arm in ("else :> () -> 0", "else => _:Event -> 0", "else :>[whole] _ -> 0"):
            with self.subTest(arm=arm):
                self.rejected(f'''enum Event {{ First, Second, }}
                fn inspect(event:Event)->i32 {{ event ? {{
                    {arm}, Event::First :> () -> 1,
                }} }}''', "XE-MATCH-0002")

    def test_different_else_spellings_share_one_slot(self):
        forms = ("else :> () -> 0", "else => _:Event -> 0", "else :>[whole] _ -> 0")
        for first in forms:
            for second in forms:
                with self.subTest(first=first, second=second):
                    self.rejected(f'''enum Event {{ First, Second, }}
                    fn inspect(event:Event)->i32 {{ event ? {{ {first}, {second}, }} }}''',
                                  "XE-MATCH-0002")

    def test_nested_branches_and_if_else_do_not_share_the_outer_else_slot(self):
        self.checked('''enum Inner { Number[i32], End, Closed, }
        enum Outer { Item[Inner], End, }
        fn inspect(outer:Outer,flag:bool)->i32 {
            outer ? {
                Outer::End :> () -> 0,
                else :> inner:Inner -> {
                    inner ? {
                        Inner::Number :> value:i32 -> if flag { value } else { 0 },
                        else :> () -> 1,
                    }
                },
            }
        }''')

    def test_all_three_spellings_reject_an_empty_remainder(self):
        for arm in ("else :> () -> 0", "else => original:Event -> 0",
                    "else :>[whole] original:Event -> 0"):
            with self.subTest(arm=arm):
                self.rejected(f'''enum Event {{ First, Second, }}
                fn inspect(event:Event)->i32 {{ event ? {{
                    Event::First :> () -> 1,
                    Event::Second :> () -> 2,
                    {arm},
                }} }}''', "XE-MATCH-0002")

    def test_empty_remainder_cannot_invent_a_signature_from_handler_annotations(self):
        for handler in ("value:i32 -> value", "_ -> 0", "() -> 0", "zero"):
            with self.subTest(handler=handler):
                self.rejected(f'''enum Event {{ End, }}
                fn zero()->i32 {{ 0 }}
                fn inspect(event:Event)->i32 {{ event ? {{
                    Event::End :> () -> 1,
                    else :> {handler},
                }} }}''', "XE-MATCH-0002")

    def test_returning_named_arm_still_covers_its_declared_variant(self):
        self.rejected('''enum Event { End, }
        fn inspect(event:Event)->i32 { event ? {
            Event::End :> () -> { return 1; },
            else => _:Event -> 0,
        } }''', "XE-MATCH-0002")

    def test_whole_receives_original_nominal_enum_even_with_one_remaining_variant(self):
        for arrow in ("=>", ":>[whole]"):
            with self.subTest(arrow=arrow):
                checker, tree = self.checked(f'''enum Event {{ Number[i32], End, }}
                type Alias=Event;
                fn retain(event:Alias)->Event {{ event ? {{
                    Event::End :> () -> Event::End,
                    else {arrow} original:Alias -> original,
                }} }}''')
                parameter = next(node for node in nodes(tree)
                                 if node["kind"] == "Parameter" and node["name"] == "original")
                self.assertEqual(checker.binding_types[id(parameter)], Type("Event"))
        self.rejected('''enum Event { Number[i32], End, }
        fn inspect(event:Event)->i32 { event ? {
            Event::End :> () -> 0,
            else => _:i32 -> 0,
        } }''', "XE-TYPE-0001")

    def test_whole_function_target_uses_an_ordinary_one_enum_parameter(self):
        self.checked('''enum Event { Number[i32], End, }
        fn read(event:Event)->i32 { event ? {
            Event::Number :> value:i32 -> value,
            Event::End :> () -> 0,
        } }
        fn inspect(event:Event)->i32 { event ? {
            Event::End :> () -> 0,
            else => read,
        } }''')
        self.rejected('''enum Event { Number[i32], End, }
        fn identity(value:i32)->i32 { value }
        fn inspect(event:Event)->i32 { event ? {
            Event::End :> () -> 0, else => identity,
        } }''', "XE-TYPE-0001")

    def test_whole_can_receive_heterogeneous_variants_without_inventing_a_common_payload(self):
        for arrow in ("=>", ":>[whole]"):
            with self.subTest(arrow=arrow):
                self.checked(f'''enum Event {{ Number[i32], Text[String], End, }}
                fn retain(event:Event)->Event {{
                    event ? {{ else {arrow} original:Event -> original, }}
                }}''')

    def test_coverage_uses_declarations_not_the_current_constructor(self):
        self.checked('''enum Event { Number[i32], End, }
        fn inspect()->i32 {
            let event:Event << Event::End;
            event ? {
                Event::End :> () -> 0,
                else :> value:i32 -> value,
            }
        }''')

    def test_else_return_exits_outer_function_and_does_not_join_normal_results(self):
        checker, tree = self.checked('''enum Event { Number[i32], End, }
        fn inspect(event:Event)->str {
            let value:i32=event ? {
                Event::Number :> value:i32 -> value,
                else :> () -> { return "end"; },
            };
            "number"
        }''')
        branch = next(node for node in nodes(tree) if node["kind"] == "Branch")
        self.assertEqual(checker.expression_types[id(branch)], I32)
        self.assertEqual(checker.expression_types[id(branch["arms"][-1]["handler"]["body"])], NEVER)
        self.rejected('''enum Event { End, }
        fn inspect(event:Event)->str {
            event ? { else :> () -> { return true; }, };
            "after"
        }''', "XE-TYPE-0001")

    def test_normal_results_require_exact_type_equality(self):
        self.rejected('''enum Event { First, Second, }
        fn inspect(event:Event,first:i8,second:u8)->Unit { event ? {
            Event::First :> () -> first,
            else :> () -> second,
        }; }''', "XE-TYPE-0001")
        self.rejected('''enum Event { First, Second, }
        fn inspect(event:Event,write:i32@[mut],read:i32@)->i32@ { event ? {
            Event::First :> () -> write,
            else :> () -> read,
        } }''', "XE-TYPE-0001")

    def test_all_returning_arms_including_else_have_no_normal_result(self):
        checker, tree = self.checked('''enum Event { First, Second, }
        fn inspect(event:Event)->i32 { event ? {
            Event::First :> () -> { return 1; },
            else :> () -> { return 2; },
        } }''')
        branch = next(node for node in nodes(tree) if node["kind"] == "Branch")
        self.assertEqual(checker.expression_types[id(branch)], NEVER)

    def test_owned_common_payload_can_be_returned_or_ignored(self):
        for handler, result in (("text:String -> text", "String"),
                                ("_:String -> unit", "Unit")):
            with self.subTest(handler=handler):
                self.checked(f'''enum Event {{ First[String], Second[String], }}
                fn inspect(event:Event)->{result} {{ event ? {{ else :> {handler}, }} }}''')

    def test_branch_and_whole_inputs_do_not_leave_a_second_owner(self):
        for arm in ("else :> text:String -> unit", "else => original:Event -> unit",
                    "else :>[whole] _ -> unit"):
            with self.subTest(arm=arm):
                self.rejected(f'''enum Event {{ First[String], Second[String], }}
                fn take(event:Event)->Unit {{}}
                fn inspect(event:Event)->Unit {{
                    event ? {{ {arm}, }};
                    take(event);
                }}''', "XE-MOVE-0001")

    def test_pointer_input_cannot_acquire_enum_ownership_through_else(self):
        # The raw-pointer spelling is admitted, but dereferencing it is not an
        # owning place. No dangling or otherwise unsafe program is executed.
        diagnostics = check_source('''enum Event { End, }
        fn inspect(pointer:Event@)->Unit {
            pointer# ? { else => original:Event -> unit, };
        }''', "pointer-else.xe")
        self.assertTrue(diagnostics)
        self.assertTrue(diagnostics[0].code.startswith(("XE-MOVE-", "XE-OWN-")),
                        diagnostics[0].render())

    def test_public_json_roundtrip_preserves_ast_and_recomputes_side_tables(self):
        text = '''enum Event { Number[i32], End, }
        fn inspect(event:Event)->i32 { event ? {
            Event::End :> () -> 0,
            else :> value:i32 -> value,
        } }'''
        source = Source(text, "else-roundtrip.xe")
        tree = parse_source(text, source.filename)
        envelope = json.loads(json.dumps(document(source, tree)))
        self.assertEqual(envelope["schema_version"], 1)
        restored = envelope["ast"]
        original = copy.deepcopy(restored)
        checker = Checker(source, restored)
        self.assertEqual(checker.check(), [])
        self.assertEqual(restored, original)
        for node in nodes(restored):
            self.assertFalse(any(name.startswith("_else") for name in node))

    def test_handcrafted_handler_type_annotation_is_not_trusted_as_payload_evidence(self):
        text = '''enum Event { Number[i32], Code[i32], }
        fn inspect(event:Event)->i32 { event ? { else :> value:i32 -> 0, } }'''
        source = Source(text, "forged-else-type.xe")
        tree = Parser(source).module()
        parameter = next(node for node in nodes(tree)
                         if node["kind"] == "Parameter" and node["name"] == "value")
        parameter["type"]["path"]["parts"] = ["bool"]
        restored = json.loads(json.dumps(tree))
        diagnostics = Checker(source, restored).check()
        self.assertTrue(diagnostics)
        self.assertEqual(diagnostics[0].code, "XE-TYPE-0001", diagnostics[0].render())

    def test_handcrafted_branch_plans_and_whole_nonelse_nodes_cannot_bypass_checks(self):
        text = '''enum Event { Number[i32], End, }
        fn inspect(event:Event)->i32 { event ? {
            Event::End :> () -> 0, else :> value:i32 -> value,
        } }'''
        source = Source(text, "forged-else.xe")
        for forgery in ("payload", "coverage", "whole-variant", "wildcard"):
            tree = Parser(source).module()
            branch = next(node for node in nodes(tree) if node["kind"] == "Branch")
            if forgery == "payload":
                branch["arms"][-1]["_payload_types"] = ["i32"]
            elif forgery == "coverage":
                branch["_else_remainder"] = ["Number"]
            elif forgery == "whole-variant":
                branch["arms"][0]["kind"] = "WholeBranchArm"
            else:
                branch["arms"][-1]["selector"]["kind"] = "WildcardSelector"
            with self.subTest(forgery=forgery):
                for validate in (validate_core_source, document):
                    with self.assertRaises(Diagnostic):
                        validate(source, tree)
                self.assertTrue(Checker(source, tree).check())


if __name__ == "__main__":
    unittest.main()
