"""Step[T] 迭代协议的类型、所有权和旧写法诊断。"""
import unittest

from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source


class StepSemanticTests(unittest.TestCase):
    def check_source(self, text, expected_code=None, message_part=None):
        checker = Checker(Source(text, "step.xe"), parse_source(text, "step.xe"))
        errors = checker.check()
        if expected_code is None:
            self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        else:
            self.assertTrue(errors, "预期语义错误，但程序通过")
            self.assertEqual(errors[0].code, expected_code, errors[0].render())
            if message_part:
                self.assertIn(message_part, errors[0].message)
        return checker

    def test_item_stop_and_match_use_enum_rules(self):
        self.check_source("""
            fn make(flag: bool) -> Step[i32] {
                if flag { Step::Item[3] } else { Step::Stop }
            }
            fn main() {
                let step = make(true);
                let number = step ? {
                    Step::Item :> value: i32 -> value,
                    Step::Stop :> _ -> 0,
                };
                println("{}", number);
            }
        """)

    def test_explicit_type_attachment(self):
        self.check_source("""
            fn main() {
                let item = Step[i32]::Item[7];
                let stop = Step[i32]::Stop;
                let a: Step[i32] = item;
                let b: Step[i32] = stop;
            }
        """)

    def test_nullable_element_is_not_stop(self):
        self.check_source("""
            fn next() -> Step[i32?] {
                Step::Item[Maybe::None]
            }
            fn main() {
                let item = next() ? {
                    Step::Item :> value: i32? -> value,
                    Step::Stop :> _ -> Maybe::None,
                };
                let final: i32? = item;
            }
        """)

    def test_result_element_with_arbitrary_error_payload(self):
        self.check_source("""
            fn next(flag: bool) -> Step[i32?[bool]] {
                if flag {
                    Step::Item[Maybe::Yes[7]]
                } else {
                    Step::Item[Maybe::No[true]]
                }
            }
            fn main() { let item = next(false); }
        """)

    def test_error_payload_type_is_not_implicitly_constrained(self):
        self.check_source("""
            fn classify(flag: bool) -> i32?[bool] {
                if flag { Maybe::Yes[7] } else { Maybe::No[true] }
            }
            fn main() {
                let yes = classify(true)?
                    1> number -> number
                    2> _ -> 0;
                let no = classify(false)?
                    1> _ -> false
                    2> error -> error;
                println("{} {}", yes, no);
            }
        """)

    def test_generic_step_and_copy_depend_on_payload(self):
        self.check_source("""
            fn[T] once(value: T) -> Step[T] { Step::Item[value] }
            fn main() {
                let first = once(7);
                let second = first;
                let resource << once(String::from("text"));
            }
        """)
        self.check_source("""
            fn main() {
                let first: Step[String] << Step::Item[String::from("text")];
                let second = first;
            }
        """, "XE-OWN-0001")

    def test_stop_requires_context_or_type_attachment(self):
        self.check_source("fn main() { let step = Step::Stop; }", "XE-GENERIC-0001")
        self.check_source("fn main() { let step: Step[i32, bool] = Step::Stop; }", "XE-TYPE-0001")

    def test_none_is_not_step_stop(self):
        self.check_source("fn next() -> Step[i32] { None } fn main() {}",
                          "XE-ITER-0001", "Step::Stop")

    def test_reserved_name_and_recursive_layout(self):
        self.check_source("enum[T] Step { Item[T], Stop, } fn main() {}", "XE-NAME-0002")
        self.check_source("type Step = i32; fn main() {}", "XE-NAME-0002")
        self.check_source("struct Cycle { next: Step[Cycle], } fn main() {}", "XE-TYPE-0006")

    def test_old_optional_next_has_migration_diagnostic(self):
        self.check_source("""
            struct Old {}
            impl Old {
                fn next(self: Self@[mut]) -> i32? { None }
            }
            fn main() {
                let iterator << Old {};
                for value in iterator {}
            }
        """, "XE-ITER-0001", "Step[T]")

    def test_old_optional_from_fn_has_migration_diagnostic(self):
        self.check_source("""
            fn main() {
                let iterator << std::iter::from_fn(fn() -> i32? { None });
            }
        """, "XE-ITER-0001", "Step[T]")

    def test_placeholder_iterator_has_a_frontend_capability_diagnostic(self):
        self.check_source("""
            fn inspect(iterator: Iterator[i32]) {
                for value in iterator {}
            }
            fn main() {}
        """, "XE-SEM-0001", "当前版本尚未实现内建 Iterator")

    def test_custom_next_and_from_fn_accept_step(self):
        self.check_source("""
            struct Counter { value: i32, }
            impl Counter {
                fn next(self: Self@[mut]) -> Step[i32] {
                    if self.value >= 2 { Step::Stop } else {
                        let value = self.value;
                        self.value = self.value + 1;
                        Step::Item[value]
                    }
                }
            }
            fn main() {
                let iterator << Counter { .value = 0; };
                for value: i32 in iterator { println("{}", value); }
                let from << std::iter::from_fn(fn() -> Step[i32] { Step::Stop });
                for value in from {}
            }
        """)


if __name__ == "__main__":
    unittest.main()
