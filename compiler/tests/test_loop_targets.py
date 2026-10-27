"""Loop labels select enclosing control targets, not variable/lifetime scopes."""
import unittest

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source


class LoopTargetTests(LegacyTestCase):
    def checked(self, source):
        checker = Checker(Source(source), parse_source(source))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        return checker

    def flow_error(self, source, text):
        errors = check_source(source)
        self.assertTrue(errors, source)
        self.assertEqual(errors[0].code, "XE-FLOW-0001", errors[0].render())
        self.assertIn(text, errors[0].message)

    def test_labels_select_enclosing_while_and_for(self):
        for transfer in ("break", "continue"):
            with self.subTest(transfer=transfer):
                self.checked("fn main() { while[label = outer] true { "
                             "for[label = inner] number in 0..2 { " + transfer + "[outer]; } } }")
                self.checked("fn main() { for[label = outer] number in 0..2 { "
                             "while true { " + transfer + "[outer]; } } }")

    def test_labels_do_not_lookup_or_change_value_names(self):
        self.checked('''
        fn main() {
            let outer = 42;
            while[label = outer] true { println("{}", outer); break[outer]; }
            println("{}", outer);
        }
        ''')

    def test_missing_non_enclosing_and_expired_targets_are_errors(self):
        for source in (
            "fn main() { break[missing]; }",
            "fn main() { while[label = other] true { break[missing]; } }",
            "fn main() { while[label = missing] false {} break[missing]; }",
            "fn main() { while true {} continue[missing]; }",
        ):
            with self.subTest(source=source):
                self.flow_error(source, "循环标签 missing")

    def test_labels_do_not_cross_named_functions(self):
        self.flow_error('''
        fn helper() { break[outer]; }
        fn main() { while[label = outer] true { helper(); break; } }
        ''', "循环标签 outer")

    def test_labels_do_not_cross_fn_closures(self):
        for transfer in ("break", "continue"):
            with self.subTest(transfer=transfer):
                self.flow_error("fn main() { while[label = outer] true { "
                                "let callback = fn() -> Unit { " + transfer + "[outer]; }; break; } }",
                                "循环标签 outer")

    def test_immediately_executed_pipeline_handlers_keep_the_target(self):
        for transfer in ("break", "continue"):
            with self.subTest(transfer=transfer):
                self.checked("fn main() { while[label = outer] true { "
                             "1 |> _ -> { " + transfer + "[outer]; }; } }")

    def test_while_label_is_available_in_its_condition(self):
        self.checked("fn main() { while[label = outer] { break[outer]; } {} }")
        self.checked("fn main() { while[label = outer] { continue[outer]; } {} }")

    def test_for_label_is_not_available_in_its_own_source(self):
        self.flow_error("fn main() { for[label = outer] value in { break[outer]; } {} }",
                        "循环标签 outer")

    def test_for_source_can_still_target_an_enclosing_loop(self):
        for transfer in ("break", "continue"):
            with self.subTest(transfer=transfer):
                # Even with the same spelling the inner for is not active yet.
                self.checked("fn main() { while[label = outer] true { "
                             "for[label = outer] value in { " + transfer + "[outer]; } {} } }")

    def test_shadowing_and_sequential_reuse_remain_lexical(self):
        self.checked('''
        fn main() {
            while[label = outer] true {
                while[label = outer] true { break[outer]; }
                break[outer];
            }
            while[label = outer] true { break[outer]; }
        }
        ''')

    def test_local_condition_transfer_does_not_make_whole_loop_never(self):
        for transfer in ("break", "continue"):
            with self.subTest(transfer=transfer):
                source = "fn main() { while[label = outer] { " + transfer + "[outer]; } {} }"
                checker = self.checked(source)
                loop = checker.functions["main"].node["body"]["tail"]
                self.assertEqual(checker.inferred_types[id(loop)]["type"], "Unit")

    def test_inner_condition_transfer_to_outer_is_not_a_local_exit(self):
        checker = self.checked('''
        fn main() {
            while[label = outer] true {
                while[label = inner] { break[outer]; } {}
                unreachable_after_nonlocal_exit;
            }
        }
        ''')
        outer = checker.functions["main"].node["body"]["tail"]
        inner = outer["body"]["statements"][0]["expression"]
        self.assertEqual(checker.inferred_types[id(inner)]["type"], "Never")
        self.assertEqual(checker.inferred_types[id(outer)]["type"], "Unit")

    def test_unlabelled_baseline_conditions_and_array_conditions_keep_their_meaning(self):
        self.checked("fn main() { while { break; } {} }")
        self.checked("fn main() { let ready = true; while[ready][0] { break; } }")

    def test_loop_unit_rule_is_not_changed_to_implicit_infinite_loop_analysis(self):
        errors = check_source("fn stopped() -> Never { while[label = outer] true {} } fn main() {}")
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-TYPE-0001", errors[0].render())

    def test_known_nonreturning_while_condition_leaves_the_function(self):
        self.checked('fn stopped() -> Never { while[label = outer] panic("unused") {} } fn main() {}')


class RemainingStrictCompletionTests(LegacyTestCase):
    def test_nonreturning_index_and_inline_pipeline_have_no_normal_result(self):
        for source in (
            'fn stopped() -> Never { let values = [1]; values[panic("unused")] } fn main() {}',
            'fn stopped() -> Never { panic("unused") |> value -> 1 } fn main() {}',
        ):
            with self.subTest(source=source):
                self.assertEqual(check_source(source), [])

    def test_nonreturning_input_does_not_hide_bad_handler_body(self):
        errors = check_source('fn stopped() -> Never { panic("unused") |> value -> missing } fn main() {}')
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-NAME-0001", errors[0].render())


if __name__ == "__main__":
    unittest.main()
