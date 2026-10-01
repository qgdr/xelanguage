"""闭包环境的所有权、写权限、身份和返回来源：不依赖生成 C 的细节。"""
import unittest
from copy import deepcopy

from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Diagnostic, Source


class ClosureSemanticTests(unittest.TestCase):
    def check(self, text, code=None):
        tree = parse_source(text, "closure.xe")
        original = deepcopy(tree)
        checker = Checker(Source(text, "closure.xe"), tree)
        errors = checker.check()
        self.assertEqual(tree, original, "语义元数据不应写入用户 AST")
        if code is None:
            self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        else:
            self.assertTrue(errors)
            self.assertEqual(errors[0].code, code, errors[0].render())
        return checker

    def test_owned_copy_capture_is_still_noncopy(self):
        self.check("fn main() { let n = 1; let f = fn[n]() -> i32 { n }; }", "XE-OWN-0001")

    def test_once_call_consumes_closure(self):
        self.check('fn main() { let text << String::from("x"); let f << fn[text]() { println("{}", text); }; f(); f(); }', "XE-MOVE-0001")

    def test_default_read_call_can_repeat(self):
        checker = self.check("fn main() { let n = 1; let f << fn[n]() -> i32 { n }; f(); f(); }")
        self.assertEqual(set(checker.closure_call_modes.values()), {"readonly"})

    def test_read_pointer_calls_can_repeat(self):
        checker = self.check("fn main() { let n = 1; let f << fn[n]() -> i32 { n }; (f@)(); (f@)(); f(); }")
        self.assertEqual(next(iter(checker.closures.values())).mode, "read")
        self.assertEqual(sorted(checker.closure_call_modes.values()), ["readonly", "readonly", "readonly"])
        self.assertEqual(checker.warnings, [])

    def test_mutable_pointer_capture_can_repeat_through_readonly_environment(self):
        checker = self.check("fn main() { let[mut] n = 0; let f << fn[n@[mut]]() { n = n + 1; }; (f@)(); (f@)(); }")
        info = next(iter(checker.closures.values()))
        self.assertEqual(info.mode, "read")
        self.assertEqual(info.captures[0][1].name, "ptr")
        self.assertTrue(info.captures[0][1].mutable)
        self.assertEqual(checker.warnings, [])

    def test_borrowed_capture_name_has_original_type_and_address(self):
        checker = self.check("fn main() { let n = 42; let f << fn[n@]() -> i32@ { n@ }; let address = (f@)(); println(\"{}\", address#); }")
        info = next(iter(checker.closures.values()))
        self.assertEqual(info.captures[0][1].name, "ptr")
        self.assertEqual(checker.inferred_types[id(info.node["body"]["tail"])]["type"], "i32@")
        self.assertEqual(info.borrow_captures, frozenset({0}))
        self.assertEqual(info.storage_captures, frozenset())
        self.assertEqual(checker.warnings, [])
        self.check("fn main() { let n = 42; let f << fn[n@]() -> i32 { n }; f(); }")
        self.check("fn main() { let n = 42; let f << fn[n@]() -> i32 { n# }; }", "XE-TYPE-0001")

    def test_readonly_borrowed_capture_cannot_write(self):
        self.check("fn main() { let[mut] n = 0; let f << fn[n@]() { n = 1; }; }", "XE-MUT-0001")

    def test_borrowing_a_pointer_variable_preserves_its_pointer_type(self):
        checker = self.check("fn main() { let n = 42; let p = n@; let f << fn[p@]() -> i32 { p# }; f(); }")
        self.assertEqual(next(iter(checker.closures.values())).captures[0][1].args[0].name, "ptr")
        self.check("fn main() { let n = 42; let p = n@; let f << fn[p]() -> i32 { p }; }", "XE-TYPE-0001")
        self.check("fn main() { let n = 42; let p = n@; let f << fn[p]() -> i32 { p# }; f(); }")

    def test_copy_capture_owns_an_independently_mutable_field(self):
        checker = self.check("fn main() { let n = 0; let[mut] f << fn[n]() -> i32 { n = n + 1; n }; (f@[mut])(); (f@[mut])(); println(\"{}\", n); }")
        self.assertEqual(next(iter(checker.closures.values())).mode, "mut")
        self.assertEqual(checker.warnings, [])

    def test_environment_write_rejected_through_readonly_pointer(self):
        self.check("fn main() { let n = 0; let f << fn[n]() { n = 1; }; (f@)(); }", "XE-MUT-0001")

    def test_environment_mutation_requires_mutable_closure_binding(self):
        self.check("fn main() { let n = 0; let f << fn[n]() { n = 1; }; (f@[mut])(); }", "XE-MUT-0001")

    def test_default_mutating_call_requires_mutable_closure_binding(self):
        self.check("fn main() { let n = 0; let f << fn[n]() -> i32 { n = 1; n }; f(); }", "XE-MUT-0001")

    def test_default_mutating_call_can_repeat(self):
        checker = self.check("fn main() { let n = 0; let[mut] f << fn[n]() -> i32 { n = n + 1; n }; f(); f(); }")
        self.assertEqual(set(checker.closure_call_modes.values()), {"mutable"})

    def test_temporary_environment_can_use_mutable_receiver(self):
        checker = self.check("fn main() { let n = 0; (fn[n]() -> i32 { n = n + 1; n })(); }")
        self.assertEqual(set(checker.closure_call_modes.values()), {"mutable"})

    def test_owned_resource_capture_moves_source(self):
        self.check('fn main() { let text << String::from("x"); let f << fn[text]() { println("{}", text@); }; println("{}", text@); }', "XE-MOVE-0001")

    def test_reading_owned_resource_allows_pointer_calls(self):
        checker = self.check('fn main() { let text << String::from("x"); let f << fn[text]() -> usize { text.len() }; (f@)(); (f@)(); }')
        self.assertEqual(next(iter(checker.closures.values())).mode, "read")

    def test_borrowed_resource_is_not_moved_by_capture_or_call(self):
        checker = self.check('fn main() { let text << String::from("x"); let f << fn[text@]() -> usize { text.len() }; f(); f(); println("{}", text); }')
        self.assertEqual(next(iter(checker.closures.values())).mode, "read")
        self.check('fn main() { let text << String::from("x"); let f << fn[text@]() -> String { text }; }', "XE-MOVE-0002")
        self.check('fn main() { let text << String::from("x"); let f << fn[text@]() { println("{}", text); }; }', "XE-MOVE-0002")
        self.check('fn take(value: String) {} fn main() { let text << String::from("x"); let f << fn[text@]() { take(text); }; }', "XE-MOVE-0002")
        self.check('fn main() { let text << String::from("x"); let f << fn[text@]() { let inner << fn[text]() {}; }; }', "XE-MOVE-0002")

    def test_resource_replacement_through_alias_invalidates_external_views(self):
        self.check('fn main() { let[mut] text << String::from("x"); let f << fn[text@]() { text << String::from("new"); }; }', "XE-MUT-0001")
        checker = self.check('fn main() { let[mut] text << String::from("x"); let f << fn[text@[mut]]() -> str { text << String::from("new"); text.as_str() }; let old = text.as_str(); let fresh = f(); println("{}", old); println("{}", fresh); }')
        info = next(iter(checker.closures.values()))
        self.assertEqual(info.mode, "read")
        self.assertEqual(info.storage_captures, frozenset())
        self.assertEqual(info.invalidated_external_captures, frozenset({0}))
        entries = {entry.get("name"): entry for entry in checker.inferred_types.values() if entry.get("name")}
        self.assertTrue(entries["old"]["unsafe"])
        self.assertFalse(entries["fresh"]["unsafe"])

    def test_moving_owned_capture_is_once_only(self):
        checker = self.check('fn main() { let text << String::from("x"); let f << fn[text]() -> String { text }; let result << f(); println("{}", result); }')
        self.assertEqual(next(iter(checker.closures.values())).mode, "once")

    def test_pointer_cannot_move_environment_resource(self):
        for pointer in ("f@", "f@[mut]"):
            with self.subTest(pointer=pointer):
                self.check('fn main() { let text << String::from("x"); let[mut] f << fn[text]() -> String { text }; (' + pointer + ')(); }', "XE-MOVE-0002")

    def test_mutating_owned_string_requires_mutable_environment(self):
        checker = self.check('fn main() { let text << String::from("x"); let[mut] f << fn[text]() { text.push_str("y"); }; (f@[mut])(); (f@[mut])(); }')
        self.assertEqual(next(iter(checker.closures.values())).mode, "mut")
        self.check('fn main() { let text << String::from("x"); let f << fn[text]() { text.push_str("y"); }; (f@)(); }', "XE-MUT-0001")

    def test_no_implicit_capture(self):
        self.check("fn main() { let n = 1; let f = fn() -> i32 { n }; }", "XE-NAME-0001")

    def test_nested_mutable_capture_requires_mutable_outer_environment(self):
        checker = self.check("fn main() { let n = 0; let[mut] f << fn[n]() { let inner << fn[n@[mut]]() { n = 1; }; inner(); }; (f@[mut])(); }")
        self.assertEqual(sorted(info.mode for info in checker.closures.values()), ["mut", "read"])
        self.check("fn main() { let n = 0; let f << fn[n]() { let inner << fn[n@[mut]]() { n = 1; }; inner(); }; (f@)(); }", "XE-MUT-0001")

    def test_nested_borrowed_alias_uses_external_source(self):
        checker = self.check("fn main() { let n = 42; let f << fn[n@]() -> i32@ { let inner << fn[n@]() -> i32@ { n@ }; inner() }; let p = f(); println(\"{}\", p#); }")
        infos = list(checker.closures.values())
        self.assertEqual([info.storage_captures for info in infos], [frozenset(), frozenset()])
        self.assertEqual([info.borrow_captures for info in infos], [frozenset({0}), frozenset({0})])
        self.assertEqual(checker.warnings, [])
        self.check("fn main() { let[mut] n = 0; let f << fn[n@]() { let inner << fn[n@[mut]]() { n = 1; }; }; }", "XE-MUT-0001")

    def test_nested_writable_alias_propagates_external_invalidation(self):
        checker = self.check('fn main() { let[mut] text << String::from("x"); let outer << fn[text@[mut]]() { let inner << fn[text@[mut]]() { text.push_str("more"); }; inner(); }; let old = text.as_str(); outer(); println("{}", old); }')
        infos = list(checker.closures.values())
        self.assertEqual([info.mode for info in infos], ["read", "read"])
        self.assertEqual([info.invalidated_external_captures for info in infos],
                         [frozenset({0}), frozenset({0})])
        self.assertTrue(checker.warnings)

    def test_local_writes_do_not_require_mutable_environment(self):
        checker = self.check("fn main() { let n = 1; let f << fn[n]() -> i32 { let[mut] local = n; local = 42; local }; (f@)(); }")
        self.assertEqual(next(iter(checker.closures.values())).mode, "read")

    def test_mutable_parameter_target_is_not_environment_mutation(self):
        checker = self.check("fn main() { let n = 1; let[mut] target = 0; let f << fn[n](out: i32@[mut]) { out# = n; }; (f@)(target@[mut]); }")
        self.assertEqual(next(iter(checker.closures.values())).mode, "read")

    def test_plain_function_pointer_is_repeatedly_callable(self):
        self.check("fn increment(value: i32) -> i32 { value + 1 } fn main() { let f = increment; (f@)(41); (f@)(41); }")

    def test_closure_can_be_used_as_pipeline_function_target(self):
        checker = self.check("fn main() { let n = 1; let f << fn[n](value: i32) -> i32 { value + n }; 41 |> f; 41 |> f; }")
        self.assertEqual(set(checker.closure_call_modes.values()), {"readonly"})

    def test_captured_read_callback_does_not_consume_outer_environment(self):
        checker = self.check("fn main() { let n = 1; let inner << fn[n]() -> i32 { n }; let outer << fn[inner]() -> i32 { inner() + inner() }; outer(); outer(); }")
        self.assertEqual([info.mode for info in checker.closures.values()], ["read", "read"])

    def test_captured_mut_callback_marks_outer_environment_mutable(self):
        checker = self.check("fn main() { let n = 0; let inner << fn[n]() -> i32 { n = n + 1; n }; let[mut] outer << fn[inner]() -> i32 { inner() }; outer(); outer(); }")
        self.assertEqual([info.mode for info in checker.closures.values()], ["mut", "mut"])

    def test_closure_behind_readonly_struct_pointer_can_repeat(self):
        self.check("struct[F] Holder { callback: F, } fn[F] hold(callback: F) -> Holder[F] { Holder[F] { .callback << callback; } } fn main() { let n = 1; let f << fn[n]() -> i32 { n }; let holder << hold(f); let pointer = holder@; (pointer.callback)(); (pointer.callback)(); }")

    def test_closure_array_element_can_repeat_without_moving_element(self):
        self.check("fn main() { let n = 1; let f << fn[n]() -> i32 { n }; let functions << [f]; functions[0](); functions[0](); }")

    def test_mutable_closure_array_requires_writable_array(self):
        self.check("fn main() { let n = 0; let f << fn[n]() -> i32 { n = n + 1; n }; let[mut] functions << [f]; functions[0](); functions[0](); }")
        self.check("fn main() { let n = 0; let f << fn[n]() -> i32 { n = n + 1; n }; let functions << [f]; functions[0](); }", "XE-MUT-0001")

    def test_duplicate_capture_rejected_before_capture_processing(self):
        self.check('fn main() { let text << String::from("x"); let f << fn[text, text]() {}; }', "XE-NAME-0002")

    def test_capture_cannot_take_partially_moved_object(self):
        self.check('struct Pair { a: String, b: String, } fn main() { let pair << Pair { .a << String::from("a"); .b << String::from("b"); }; let a << pair.a; let f << fn[pair]() {}; }', "XE-MOVE-0001")

    def test_distinct_closures_do_not_merge_even_with_same_signature(self):
        self.check("fn main() { let n = 1; let f << if true { fn[n]() -> i32 { n } } else { fn[n]() -> i32 { n } }; }", "XE-TYPE-0001")

    def test_generic_callback_can_take_concrete_closure(self):
        checker = self.check("fn[F] call(callback: F) -> i32 { callback() } fn main() { let n = 42; let f << fn[n]() -> i32 { n }; println(\"{}\", call(f)); }")
        self.assertEqual(len(checker.generic_instances), 1)

    def test_passing_closure_to_ordinary_owned_parameter_still_moves(self):
        self.check("fn[F] accept(callback: F) { callback(); } fn main() { let n = 1; let f << fn[n]() -> i32 { n }; accept(f); f(); }", "XE-MOVE-0001")

    def test_callee_receiver_does_not_consume_environment_before_arguments(self):
        self.check("fn[F] change(callback: F@[mut]) -> i32 { callback(10); 1 } fn main() { let n = 0; let[mut] f << fn[n](increment: i32) -> i32 { n = n + increment; n }; f(change(f@[mut])); f(0); }")

    def test_generic_callback_pointer_can_repeat(self):
        self.check("fn[F] twice(callback: F@) -> i32 { callback() + callback() } fn main() { let n = 21; let f << fn[n]() -> i32 { n }; println(\"{}\", twice(f@)); }")

    def test_generic_factory_instances_do_not_share_environment_layout(self):
        checker = self.check('fn[T] inspect(value: T) -> usize { let f << fn[value]() -> usize { 1 }; f() } fn main() { inspect[i32](1); inspect[String](String::from("s")); }')
        infos = list(checker.closures.values())
        self.assertEqual(len(infos), 2)
        self.assertEqual({info.captures[0][1].name for info in infos}, {"i32", "String"})
        self.assertEqual(len({type_.identity for type_ in checker.closures}), 2)

    def test_once_call_returning_view_of_captured_string_warns(self):
        checker = self.check('fn main() { let text << String::from("x"); let f << fn[text]() -> str { let view = text.as_str(); println("{}", text); view }; let view = f(); println("{}", view); }')
        self.assertTrue(checker.warnings)
        self.assertEqual(next(iter(checker.closures.values())).storage_captures, frozenset({0}))

    def test_default_call_returning_view_of_environment_keeps_environment_alive(self):
        checker = self.check('fn main() { let text << String::from("x"); let f << fn[text]() -> str { text.as_str() }; let view = f(); println("{}", view); f(); }')
        self.assertEqual(checker.warnings, [])

    def test_temporary_read_environment_returning_view_warns(self):
        checker = self.check('fn main() { let text << String::from("x"); let view = (fn[text]() -> str { text.as_str() })(); println("{}", view); }')
        self.assertTrue(checker.warnings)

    def test_environment_append_invalidates_old_view_but_not_new_returned_view(self):
        checker = self.check('fn main() { let text << String::from("x"); let[mut] f << fn[text](append: bool) -> str { if append { text.push_str("more"); } text.as_str() }; let old = f(false); let fresh = f(true); println("{}", old); println("{}", fresh); }')
        info = next(iter(checker.closures.values()))
        self.assertEqual(info.invalidated_captures, frozenset({0}))
        entries = {entry.get("name"): entry for entry in checker.inferred_types.values() if entry.get("name")}
        self.assertTrue(entries["old"]["unsafe"])
        self.assertFalse(entries["fresh"]["unsafe"])
        self.assertTrue(checker.warnings)

    def test_replacing_environment_resource_invalidates_old_views(self):
        checker = self.check('fn main() { let text << String::from("x"); let[mut] f << fn[text]() -> str { text << String::from("new"); text.as_str() }; let old = f(); f(); println("{}", old); }')
        self.assertEqual(next(iter(checker.closures.values())).invalidated_captures, frozenset({0}))
        self.assertTrue(checker.warnings)

    def test_mutating_counter_does_not_invalidate_field_address(self):
        checker = self.check('fn main() { let n = 0; let[mut] f << fn[n]() -> i32@ { n = n + 1; n@ }; let first = f(); f(); println("{}", first#); }')
        info = next(iter(checker.closures.values()))
        self.assertEqual(info.invalidated_captures, frozenset())
        self.assertEqual(info.invalidated_external_captures, frozenset())
        self.assertEqual(checker.warnings, [])

    def test_mutable_pointer_capture_append_invalidates_external_views(self):
        checker = self.check('fn main() { let[mut] text << String::from("x"); let f << fn[text@[mut]]() -> str { text.push_str("more"); text.as_str() }; let old = text.as_str(); let fresh = f(); println("{}", old); println("{}", fresh); }')
        info = next(iter(checker.closures.values()))
        self.assertEqual(info.mode, "read")
        self.assertEqual(info.invalidated_external_captures, frozenset({0}))
        entries = {entry.get("name"): entry for entry in checker.inferred_types.values() if entry.get("name")}
        self.assertTrue(entries["old"]["unsafe"])
        self.assertFalse(entries["fresh"]["unsafe"])

    def test_nested_callback_propagates_resource_invalidation(self):
        checker = self.check('fn main() { let[mut] text << String::from("x"); let inner << fn[text@[mut]]() { text.push_str("more"); }; let outer << fn[inner]() { inner(); }; let old = text.as_str(); outer(); println("{}", old); }')
        infos = list(checker.closures.values())
        self.assertEqual([info.invalidated_external_captures for info in infos], [frozenset({0}), frozenset({0})])
        self.assertTrue(checker.warnings)

    def test_pointer_call_returning_view_of_environment_is_valid_while_alive(self):
        checker = self.check('fn main() { let text << String::from("x"); let f << fn[text]() -> str { text.as_str() }; let view = (f@)(); println("{}", view); }')
        self.assertEqual(checker.warnings, [])

    def test_only_returned_parameter_contributes_view_sources(self):
        checker = self.check('fn main() { let n = 1; let f << fn[n](first: str, second: str) -> str { first }; (f@)("a", "b"); }')
        info = next(iter(checker.closures.values()))
        self.assertEqual(info.borrow_parameters, frozenset({0}))
        self.assertEqual(info.borrow_captures, frozenset())

    def test_failed_anonymous_analysis_restores_outer_context(self):
        text = "fn broken() { let n = 1; let f << fn[n](x) -> i32 { x }; }"
        checker = Checker(Source(text, "closure.xe"), parse_source(text, "closure.xe"))
        checker.collect()
        with self.assertRaises(Diagnostic):
            checker.check_function(checker.functions["broken"])
        self.assertEqual(checker.result.name, "Unit")
        self.assertEqual(checker._closure_bindings, {})
        self.assertIn("n", checker.scopes[-1])


if __name__ == "__main__":
    unittest.main()
