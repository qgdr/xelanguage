"""Unreachable static checks must not invent executable enclosing-loop exits."""
import unittest

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source

DECLARATIONS = '''
fn sink(first: i32, second: i32) {}
fn[T] generic(first: i32, second: T) {}
struct Pair { first: i32, second: i32, }
impl Pair { fn sink(self: Self@, first: i32, second: i32) {} }
'''


class CompletionTargetTests(LegacyTestCase):
    def checked(self, source):
        checker = Checker(Source(source), parse_source(source))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        return checker

    def source(self, statement, *, result="Never", final="true"):
        return (DECLARATIONS + "fn inspect() -> " + result + " { while[label = outer] { "
                + statement + " " + final + " } {} } fn main() {}")

    def rejected(self, statement, code):
        errors = check_source(self.source(statement))
        self.assertTrue(errors, statement)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def test_never_if_conditions_do_not_record_transfers_from_either_arm(self):
        for transfer in ("break", "continue"):
            for statement in (
                'if panic("unused") { ' + transfer + "[outer]; };",
                'if panic("unused") {} else { ' + transfer + "[outer]; };",
                'if panic("unused") { ' + transfer + "[outer]; } else { " + transfer + "[outer]; };",
            ):
                with self.subTest(statement=statement):
                    self.checked(self.source(statement))

    def test_never_first_arguments_do_not_record_later_argument_transfers(self):
        for statement in (
            'sink(panic("unused"), { break[outer]; });',
            'let callback = sink; callback(panic("unused"), { continue[outer]; });',
            'generic[i32](panic("unused"), { break[outer]; });',
            'let pair << Pair { .first = 1; .second = 2; }; '
            'pair.sink(panic("unused"), { continue[outer]; });',
        ):
            with self.subTest(statement=statement):
                self.checked(self.source(statement))

    def test_later_argument_generic_inference_is_still_performed(self):
        checker = self.checked(self.source('''
            generic(panic("unused"), { if true { break[outer]; }; 1 });
        '''))
        self.assertEqual(len(checker.generic_instances), 1)
        signature = checker.functions[next(iter(checker.generic_instances.values()))]
        self.assertEqual(str(signature.type_substitutions["$T"]), "i32")

    def test_formatted_values_and_struct_fields_do_not_record_late_transfers(self):
        for statement in (
            'println("{} {}", panic("unused"), { break[outer]; });',
            'println("{} {} {}", 1, panic("unused"), { continue[outer]; });',
            'let pair << Pair { .first = panic("unused"); .second = { break[outer]; }; };',
        ):
            with self.subTest(statement=statement):
                self.checked(self.source(statement))

    def test_eager_and_boolean_right_operands_do_not_record_unreachable_transfers(self):
        for statement in (
            'let value: i32 = panic("unused") + { break[outer]; };',
            'let value: i32 = panic("unused") bitshl { break[outer]; };',
            'let value: i32 = panic("unused") bitshr { continue[outer]; };',
            'let value: i32 = panic("unused") bitand { continue[outer]; };',
            'let value = panic("unused") and { break[outer]; };',
            'let value = panic("unused") or { continue[outer]; };',
            'let interval = panic("unused")..{ break[outer]; };',
        ):
            with self.subTest(statement=statement):
                self.checked(self.source(statement))

    def test_comparison_suffix_transfers_are_ignored_after_an_earlier_never(self):
        for expression in (
            'panic("unused") < 1 < { break[outer]; }',
            '1 < panic("unused") < { continue[outer]; }',
            '1 < 2 < panic("unused") < { break[outer]; }',
        ):
            with self.subTest(expression=expression):
                # Even the comparison's false/skip path then reaches panic;
                # only the unreachable suffix could otherwise invent an exit.
                self.checked(self.source("let value = " + expression + ";", final='panic("after")'))

    def test_pipeline_and_wildcard_branch_handlers_do_not_run_after_never_input(self):
        for statement in (
            'panic("unused") |> _ -> { break[outer]; };',
            'panic("unused") ? { _ :> _ -> { continue[outer]; }, };',
            'panic("unused") ? { _ :> _ -> unit, };',
        ):
            with self.subTest(statement=statement):
                self.checked(self.source(statement))

    def test_unentered_inner_while_body_cannot_exit_an_enclosing_condition(self):
        for transfer in ("break", "continue"):
            with self.subTest(transfer=transfer):
                self.checked(self.source('while panic("unused") { ' + transfer + "[outer]; }"))

    def test_real_condition_transfers_still_produce_the_existing_loop_unit_type(self):
        for statement in (
            "break[outer];", "continue[outer];",
            "if true { break[outer]; };",
            "sink(1, { break[outer]; });",
            'println("{} {}", 1, { continue[outer]; });',
            "1 < 2 < { break[outer]; };",
            'true and { continue[outer]; };',
        ):
            with self.subTest(statement=statement):
                checker = self.checked(self.source(statement, result="Unit"))
                loop = checker.functions["inspect"].node["body"]["tail"]
                self.assertEqual(checker.inferred_types[id(loop)]["type"], "Unit")

    def test_dead_subtrees_preserve_an_exit_recorded_on_an_earlier_live_path(self):
        for transfer in ("break", "continue"):
            with self.subTest(transfer=transfer):
                checker = self.checked(self.source(
                    "if true { " + transfer + "[outer]; }; "
                    'if panic("unused") { break[outer]; };', result="Unit"))
                outer = checker.functions["inspect"].node["body"]["tail"]
                self.assertEqual(checker.inferred_types[id(outer)]["type"], "Unit")

    def test_unreachable_regions_still_compute_new_inner_loops_own_transfers(self):
        checker = self.checked(self.source('''
            if panic("unused") {
                let local: Unit = while[label = inner] { break[inner]; } {};
            };
        '''))
        outer = checker.functions["inspect"].node["body"]["tail"]
        conditional = outer["condition"]["statements"][0]["expression"]
        inner = conditional["then"]["statements"][0]["value"]
        self.assertEqual(checker.inferred_types[id(inner)]["type"], "Unit")
        self.assertEqual(checker.inferred_types[id(outer)]["type"], "Never")

    def test_dead_regions_do_not_hide_names_types_or_invalid_targets(self):
        for statement, code in (
            ('if panic("unused") { missing; };', "XE-NAME-0001"),
            ('if panic("unused") {} else { let bad: i32 = "bad"; };', "XE-TYPE-0001"),
            ('sink(panic("unused"), missing);', "XE-NAME-0001"),
            ('sink(panic("unused"), "bad");', "XE-TYPE-0001"),
            ('println("{} {}", panic("unused"), missing);', "XE-NAME-0001"),
            ('let pair << Pair { .first = panic("unused"); .second = missing; };', "XE-NAME-0001"),
            ('let value: i32 = panic("unused") + missing;', "XE-NAME-0001"),
            ('let value = panic("unused") < 1 < "bad";', "XE-TYPE-0001"),
            ('panic("unused") |> _ -> missing;', "XE-NAME-0001"),
            ('panic("unused") ? { _ :> _ -> missing, };', "XE-NAME-0001"),
            ('panic("unused") ? 1> _ -> unit 2> _ -> unit;', "XE-MATCH-0001"),
            ('if panic("unused") { break[missing]; };', "XE-FLOW-0001"),
            ('while panic("unused") { missing; }', "XE-NAME-0001"),
            ('''if panic("unused") {
                while[label = inner] { break[inner]; } {}
                let bad: i32 = "bad";
            };''', "XE-TYPE-0001"),
        ):
            with self.subTest(statement=statement):
                self.rejected(statement, code)

    def test_existing_block_array_and_tuple_unreachable_policies_are_preserved(self):
        for statement in (
            'let stopped: i32 = panic("unused"); missing;',
            'let values = [panic("unused"), missing];',
            'let values = tuple[panic("unused"), missing];',
        ):
            with self.subTest(statement=statement):
                self.checked(self.source(statement))


if __name__ == "__main__":
    unittest.main()
