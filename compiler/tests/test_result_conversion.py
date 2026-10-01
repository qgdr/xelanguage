"""结果构造/消解与转换规则的前端验收。

正例验证精确类型，不把“没有 Python 异常”视为实现完成；反例断言
面向使用者的诊断编号。运行行为另外由 test_backend_results 覆盖。
"""
import unittest

from compiler.xe_ast import Diagnostic, parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source
from compiler.xe_ast.typesys import CONVERSION_ERROR, Type, maybe


class RecordingChecker(Checker):
    """模拟后端侧表：记录输入节点本身的类型，不改 AST JSON。"""
    def __init__(self, source, tree):
        super().__init__(source, tree)
        self.expression_types = {}

    def infer(self, node, expected=None, lift=False):
        value = super().infer(node, expected, lift)
        self.expression_types[id(node)] = value.type
        return value


class ResultConversionTests(unittest.TestCase):
    def assert_ok(self, text):
        errors = check_source(text)
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))

    def assert_error(self, text, code):
        errors = check_source(text)
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def assert_escape_warning(self, text):
        checker = Checker(Source(text), parse_source(text))
        self.assertEqual(checker.check(), [])
        self.assertTrue(any(warning.code == "XE-PTR-0001" and warning.severity == "warning"
                            for warning in checker.warnings))

    def test_checked_conversion_is_a_normal_call_with_typed_error(self):
        text = "fn f(n: i32) { let byte: u8?[ConversionError] = u8::try_from(n); }"
        tree = parse_source(text)
        checker = RecordingChecker(Source(text), tree)
        self.assertEqual(checker.check(), [])
        call = tree["items"][0]["body"]["statements"][0]["value"]
        self.assertEqual(call["kind"], "Call")
        self.assertEqual(checker.expression_types[id(call)], maybe(Type("u8"), CONVERSION_ERROR))

    def test_range_failure_remains_runtime_result_not_compile_time_error(self):
        self.assert_ok("fn f() { let a: u8?[ConversionError] = u8::try_from(300); let b: u8?[ConversionError] = u8::try_from(-1); }")

    def test_try_from_requires_one_integer_and_does_not_invent_float_policy(self):
        self.assert_error("fn f() { u8::try_from(); }", "XE-CALL-0001")
        self.assert_error("fn f() { u8::try_from(1, 2); }", "XE-CALL-0001")
        self.assert_error('fn f() { u8::try_from("hello"); }', "XE-TYPE-0001")
        self.assert_error("fn f(n: f64) { u8::try_from(n); }", "XE-SEM-0001")
        self.assert_error("fn f(n: i32) { f64::try_from(n); }", "XE-SEM-0001")

    def test_lossless_as_and_no_implicit_widening(self):
        self.assert_ok("fn f(n: i32) { let wide: i64 = n as i64; }")
        self.assert_error("fn f(n: i64) { let byte: u8 = n as u8; }", "XE-TYPE-0001")
        self.assert_error("fn f(n: i32) { let wide: i64 = n; }", "XE-TYPE-0001")

    def test_removed_cast_modifiers_have_actionable_migration_message(self):
        for modifier in ("checked", "wrap"):
            with self.subTest(modifier=modifier), self.assertRaises(Diagnostic) as caught:
                parse_source(f"fn f(n: i32) {{ n as[{modifier}] u8; }}")
            self.assertEqual(caught.exception.code, "XE-PARSE-0001")
            self.assertIn("try_from", caught.exception.message)
            assert caught.exception.hint is not None
            self.assertIn("?[panic]", caught.exception.hint)

    def test_panic_unwrap_is_postfix_and_does_not_require_maybe_return(self):
        text = "fn f(n: i32) -> u8 { u8::try_from(n)?[panic] }"
        tree = parse_source(text)
        self.assertEqual(tree["items"][0]["body"]["tail"]["kind"], "Unwrap")
        self.assert_ok(text)
        self.assert_ok("fn f(n: i32?) -> i64 { n?[panic] as i64 }")
        self.assert_error("fn f(n: i32) { n?[panic]; }", "XE-RESULT-0002")

    def test_unwrap_moves_resources_and_cannot_steal_through_pointer(self):
        self.assert_ok("fn f(text: String?) -> String { text?[panic] }")
        self.assert_error("fn f(text: String?) { let first << text?[panic]; let second << text?[panic]; }", "XE-MOVE-0001")
        self.assert_error("fn f(text: String?@) -> String { text#?[panic] }", "XE-MOVE-0002")

    def test_propagation_still_requires_identical_error_type(self):
        self.assert_ok("fn f(n: i32) -> u8?[ConversionError] { u8::try_from(n)?[return] }")
        self.assert_error("fn f(n: i32) -> u8? { u8::try_from(n)?[return] }", "XE-RESULT-0002")
        self.assert_error("fn f(n: i32) -> u8 { u8::try_from(n)?[return] }", "XE-RESULT-0002")

    def test_none_is_result_marker_not_storage_or_parameter_type(self):
        for text in ("fn f() { let n = None; }", "fn f() { let n: None = None; }",
                     "fn f(n: None) {}", "fn f() -> None { None }",
                     "fn f() { None; }"):
            with self.subTest(source=text):
                self.assert_error(text, "XE-RESULT-0001")
        self.assert_ok("fn f(flag: bool) -> i32?[None] { if flag { 3 } else { None } }")
        self.assert_error("fn f() -> i32?[ConversionError] { None }", "XE-RESULT-0001")

    def test_nested_maybe_returns_wrap_only_the_outer_result(self):
        self.assert_ok("fn wrap(value: i32?) -> i32?? { value }")
        self.assert_ok("fn identity(value: i32??) -> i32?? { value }")
        self.assert_ok("fn wrap(value: i32?) -> i32?? { return value; }")

    def test_result_payload_receives_numeric_and_tuple_context(self):
        self.assert_ok("fn wide() -> i64? { 1 + 2 }")
        self.assert_ok("fn fractional() -> f32? { -1.5 }")
        self.assert_ok("fn pair() -> tuple[i64, f32]? { tuple[10, -1.5] }")
        self.assert_error("fn pair() -> tuple[u8, i64]? { tuple[256, 2] }", "XE-TYPE-0004")

    def test_handlers_keep_return_position_success_context(self):
        self.assert_ok("fn f(value: i32?) -> i64? { value ? 1> n -> (n as i64) + 1 2> _ -> None }")
        self.assert_ok("enum E { Number[i32], End, } fn f(value: E) -> i64? { value ? { E::Number :> n -> (n as i64) + 1, E::End :> _ -> None, } }")

    def test_negative_literal_patterns_share_existing_selector_kind(self):
        text = "fn f(n: i32) -> i32 { n ? { -1 :> _ -> 0, _ :> x -> x, } }"
        tree = parse_source(text)
        selector = tree["items"][0]["body"]["tail"]["arms"][0]["selector"]
        self.assertEqual(selector["kind"], "LiteralSelector")
        self.assertEqual(selector["value"]["value"], -1)
        self.assert_ok(text)

    def test_copying_fields_through_mutable_pointer_does_not_keep_a_loan(self):
        self.assert_ok("struct S { x: i32, y: i32, } fn pair(x: i32, y: i32) {} impl S { fn f(self: Self@[mut]) { pair(self.x, self.y); let copied = self.x; self.x = copied + 1; } }")
        self.assert_ok("struct S { x: i32, } impl Copy for S; impl S { fn read(self: Self@[mut]) -> S { S { .x = self.x; } } } fn f() { let[mut] value = S { .x = 1; }; let copy = value.read(); value.x = 2; println(\"{}\", copy.x); }")

    def test_copy_uses_equal_and_resource_uses_transfer(self):
        self.assert_ok('fn f() { let i = 1; let[mut] j = i; j = 2; i >> j; let s << String::from("hello"); let t << s; }')
        for text in ("fn f() { let i << 1; }", "fn f() { let[mut] i = 0; i << 1; }",
                     "struct S { i: i32, } fn f() { let s = S { .i << 1; }; }",
                     "struct S { i: i32, } impl Copy for S; fn f() { let s << S { .i = 1; }; }",
                     "fn f(n: i32) { let byte << u8::try_from(n); }",
                     "fn f(n: i32?) { let copy << n; }"):
            with self.subTest(source=text):
                self.assert_error(text, "XE-OWN-0001")
        self.assert_error('fn f() { let s = String::from("hello"); }', "XE-OWN-0001")
        self.assert_ok("fn f(n: String?) { let moved << n; }")

    def test_propagated_error_reports_view_outliving_local_owner(self):
        self.assert_escape_warning('fn bad() -> i32?[str] { let s << String::from("owned"); let r: i32?[str] = Maybe::No[s.as_str()]; r?[return] }')
        # 错误来源必须穿过调用边界，即使 get 定义在 caller 后面也一样。
        self.assert_escape_warning('fn bad() -> i32?[str] { let s << String::from("owned"); get(s.as_str())?[return] } fn get(text: str) -> i32?[str] { Maybe::No[text] }')
        self.assert_ok('fn get(text: str) -> i32?[str] { Maybe::No[text] } fn forward(text: str) -> i32?[str] { get(text)?[return] }')

    def test_static_error_message_does_not_borrow_mutable_receiver(self):
        self.assert_ok('struct S { n: i32, } impl Copy for S; struct E { message: str, } impl S { fn next(self: Self@[mut]) -> i32?[E] { self.n = self.n + 1; Maybe::No[E { .message = "static"; }] } } fn f() -> i32?[E] { let[mut] s = S { .n = 0; }; s.next()?[return] }')
        # 参数依赖必须传遍递归调用，不能靠声明顺序偷偷允许逃逸。
        self.assert_escape_warning('fn bad() -> i32?[str] { let s << String::from("owned"); a(s.as_str())?[return] } fn a(text: str) -> i32?[str] { b(text)?[return] } fn b(text: str) -> i32?[str] { Maybe::No[text] }')

    def test_recursive_value_layouts_report_friendly_frontend_error(self):
        for text in ("struct S { value: S, }", "struct A { b: B, } struct B { a: A, }",
                     "enum E { Next[E], End, }", "struct S { value: S?, }"):
            with self.subTest(source=text):
                self.assert_error(text, "XE-TYPE-0006")
        self.assert_ok("struct Node { next: Node@, value: i32, }")

    def test_trait_is_constraint_not_undecided_dynamic_value_type(self):
        self.assert_error("trait Measure { fn measure(self: Self@) -> i32; } fn f(value: Measure) {}", "XE-SEM-0001")

    def test_copy_from_rejects_noncopy_elements_before_backend(self):
        self.assert_error("fn f(destination: SliceMut[String], source: Slice[String]) { let[mut] writable = destination; writable.copy_from(source); }", "XE-OWN-0001")

    def test_recursive_rotating_parameters_reach_a_real_fixed_point(self):
        # One recursive function needs ten propagation rounds, not one round per
        # function. Its result can originate from every rotated str parameter.
        parameters = ", ".join(f"a{i}: str" for i in range(10))
        rotated = ", ".join([*(f"a{i}" for i in range(1, 10)), "a0", "true"])
        text = (f"fn rotate({parameters}, done: bool) -> str {{ "
                f"if done {{ a0 }} else {{ rotate({rotated}) }} }}")
        checker = Checker(Source(text), parse_source(text))
        self.assertEqual(checker.check(), [])
        self.assertEqual(checker.functions["rotate"].borrow_parameters, frozenset(range(10)))

    def test_late_recursive_parameter_origin_reports_unsafe_escape(self):
        parameters = ", ".join(f"a{i}: str" for i in range(10))
        rotated = ", ".join([*(f"a{i}" for i in range(1, 10)), "a0", "true"])
        actual = ", ".join([*('"static"' for _ in range(9)), "local.as_str()", "false"])
        # Caller comes first: final validation must use a fully stable summary,
        # not accidentally rely on declaration order to get one more round.
        text = (f'fn bad() -> str {{ let local << String::from("owned"); rotate({actual}) }} '
                f"fn rotate({parameters}, done: bool) -> str {{ "
                f"if done {{ a0 }} else {{ rotate({rotated}) }} }}")
        self.assert_escape_warning(text)

    def test_mutually_recursive_static_results_do_not_invent_parameter_borrows(self):
        text = '''fn safe() -> str {
            let local << String::from("temporary"); a(local.as_str(), 2)
        }
        fn a(text: str, depth: i32) -> str {
            if depth == 0 { "static a" } else { b(text, depth - 1) }
        }
        fn b(text: str, depth: i32) -> str {
            if depth == 0 { "static b" } else { a(text, depth - 1) }
        }'''
        checker = Checker(Source(text), parse_source(text))
        self.assertEqual(checker.check(), [])
        self.assertEqual(checker.functions["a"].borrow_parameters, frozenset())
        self.assertEqual(checker.functions["b"].borrow_parameters, frozenset())


if __name__ == "__main__":
    unittest.main()
