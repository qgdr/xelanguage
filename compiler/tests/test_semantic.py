"""语法与语义反例分层验收。断言错误编号而不是把任意失败都当作通过。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from compiler.xe_ast import Diagnostic, parse_source
from compiler.xe_ast.cli import main
from compiler.xe_ast.semantic import check_source

ROOT = Path(__file__).resolve().parents[2]

FAILURES = {
    "struct_missing_field": "XE-INIT-0002",
    "struct_extra_field": "XE-INIT-0002",
    "struct_duplicate_field": "XE-INIT-0002",
    "struct_field_type": "XE-TYPE-0001",
    "struct_resource_copy": "XE-OWN-0001",
    "method_readonly_receiver": "XE-BORROW-0004",
    "method_owned_receiver_moved": "XE-MOVE-0001",
    "method_argument_type": "XE-TYPE-0001",
    "method_unknown": "XE-NAME-0001",
    "trait_copy_resource": "XE-OWN-0001",
    "trait_copy_drop_conflict": "XE-OWN-0001",
    "trait_drop_signature": "XE-OWN-0002",
    "trait_copy_method": "XE-OWN-0001",
    "String": "XE-MOVE-0001",
    "borrow_alias": "XE-BORROW-0002",
    "borrow_match_custom_drop": "XE-OWN-0002",
    "borrow_match_move": "XE-MOVE-0002",
    "borrow_move_resource": "XE-MOVE-0002",
    "borrow_pipe_move": "XE-MOVE-0002",
    "borrow_scalar_parameter": "XE-TYPE-0001",
    "branch_call_target": "XE-PARSE-0001",
    "branch_value_target": "XE-PARSE-0001",
    "channel_missing_parameter": "XE-PARSE-0001",
    "channel_none_payload": "XE-CALL-0001",
    "closure_capture_copy": "XE-OWN-0001",
    "closure_without_fn": "XE-PARSE-0001",
    "copy_move_type": "XE-OWN-0001",
    "enum_variant_call": "XE-TYPE-0005",
    "format_move": "XE-MOVE-0001",
    "none_error_result": "XE-RESULT-0001",
    "pointer_owned_match": "XE-MOVE-0002",
    "result_unhandled": "XE-PARSE-0001",
    "str_pointer": "XE-TYPE-0003",
    "semantic_unknown_name": "XE-NAME-0001",
    "semantic_argument_type": "XE-TYPE-0001",
    "semantic_argument_count": "XE-CALL-0001",
    "semantic_uninitialized": "XE-INIT-0001",
    "semantic_immutable_assignment": "XE-MUT-0001",
    "semantic_non_exhaustive": "XE-MATCH-0003",
    "semantic_branch_move": "XE-MOVE-0001",
    "semantic_return_local_borrow": "XE-BORROW-0003",
    "semantic_readonly_pointer": "XE-MUT-0001",
    "semantic_format_count": "XE-FORMAT-0001",
}

SYNTAX_FAILURES = {
    "invalid_character": ("XE-LEX-0001", 1, 25, "无法识别的字符"),
    "missing_argument_comma": ("XE-PARSE-0001", 1, 32, "需要 )"),
    "missing_channel": ("XE-PARSE-0001", 2, 37, "必须处理 1> 和 2>"),
    "missing_expression": ("XE-PARSE-0001", 1, 25, "需要表达式"),
    "missing_semicolon": ("XE-PARSE-0001", 3, 5, "需要 ;"),
    "missing_type": ("XE-PARSE-0001", 1, 24, "需要名称"),
    "old_match_arrow": ("XE-PARSE-0001", 4, 26, "分支管道 :>"),
    "unclosed_block": ("XE-PARSE-0001", 3, 1, "缺少 }"),
    "unclosed_comment": ("XE-LEX-0001", 2, 1, "缺少 */"),
    "unclosed_string": ("XE-LEX-0001", 1, 21, "字面量不能直接跨行"),
}


class SemanticTests(unittest.TestCase):
    def assert_error(self, source, code, checked=True):
        errors = check_source(source, "example.xe", checked)
        self.assertTrue(errors, source)
        self.assertEqual(errors[0].code, code, errors[0].render())
        self.assertIn("example.xe:", errors[0].render())
        return errors

    def test_all_modern_positive_examples(self):
        for path in sorted((ROOT / "tests/stage999").glob("*.xe")):
            with self.subTest(path=path.name):
                errors = check_source(path.read_text(), str(path), path.stem != "pointer_unchecked")
                self.assertEqual(errors, [], "\n".join(e.render() for e in errors))

    def test_all_failure_examples_have_intended_error(self):
        paths = sorted((ROOT / "tests/fails").glob("*.xe"))
        self.assertEqual({p.stem for p in paths}, set(FAILURES), "为新反例登记预期编号")
        for path in paths:
            with self.subTest(path=path.name):
                text, code = path.read_text(), FAILURES[path.stem]
                # 类型/移动反例必须先能生成 AST，不能靠语法错误蒙混过关。
                if not code.startswith(("XE-PARSE", "XE-LEX")):
                    parse_source(text, str(path))
                errors = check_source(text, str(path), True)
                self.assertTrue(errors, path)
                self.assertEqual(errors[0].code, code, errors[0].render())

    def test_new_syntax_failure_files(self):
        paths = sorted((ROOT / "tests/syntax_fails").glob("*.xe"))
        self.assertEqual({p.stem for p in paths}, set(SYNTAX_FAILURES))
        for path in paths:
            with self.subTest(path=path.name):
                with self.assertRaises(Diagnostic) as caught:
                    parse_source(path.read_text(), str(path))
                error = caught.exception
                code, line, column, message = SYNTAX_FAILURES[path.stem]
                self.assertEqual(error.code, code)
                position = error.to_dict()["span"]["start"]
                self.assertEqual((position["line"], position["column"]), (line, column))
                self.assertIn(message, error.message)
                self.assertIn(str(path), error.render())
                self.assertGreaterEqual(error.to_dict()["span"]["start"]["line"], 1)

    def test_ownership_is_checked_in_both_modes(self):
        for name in ("borrow_move_resource", "borrow_pipe_move", "borrow_match_move"):
            source = (ROOT / "tests/fails" / (name + ".xe")).read_text()
            self.assert_error(source, "XE-MOVE-0002", checked=False)

    def test_readonly_check_is_optional(self):
        source = "fn write(p: i32@) { p# = 1; }"
        self.assertEqual(check_source(source), [])
        self.assert_error(source, "XE-MUT-0001")

    def test_multiple_functions_report_independent_errors(self):
        errors = check_source('fn a() { missing; } fn b() { let x: i32 = "bad"; }')
        self.assertEqual([e.code for e in errors], ["XE-NAME-0001", "XE-TYPE-0001"])

    def test_move_merge_excludes_returning_branches(self):
        source = '''fn take(x: String) {}
        fn inspect(x: String, cond: bool) {
            if cond { take(x); return; }
            println("{}", x@);
        }'''
        self.assertEqual(check_source(source, check_borrows=True), [])

    def test_initialization_merge(self):
        self.assert_error("fn f(c: bool) { let x: i32; if c { x = 1; } println(\"{}\", x); }",
                          "XE-INIT-0001")
        source = "fn f(c: bool) { let x: i32; if c { x = 1; } else { x = 2; } println(\"{}\", x); }"
        self.assertEqual(check_source(source), [])

    def test_repeated_resource_in_arguments(self):
        self.assert_error('fn f(a: String, b: String) {} fn main() { let x << String::from("x"); f(x, x); }',
                          "XE-MOVE-0001")

    def test_argument_borrow_conflict(self):
        self.assert_error("fn f(a: i32@[mut], b: i32@) {} fn main() { var x = 1; f(x@[mut], x@); }",
                          "XE-BORROW-0002")

    def test_receiver_borrow_lasts_through_argument_evaluation(self):
        source = '''struct S { n: i32, }
        impl S { fn update(self: Self@[mut], x: Self@) {} }
        fn f() { var s << S { .n = 0; }; s.update(s@); }'''
        self.assert_error(source, "XE-BORROW-0002")

    def test_return_local_view(self):
        self.assert_error('fn f() -> str { let x << String::from("x"); x.as_str() }',
                          "XE-BORROW-0003")

    def test_last_use_ends_shared_loan(self):
        source = 'fn main() { var x = 1; let p = x@; println("{}", p#); let q << x@[mut]; q# = 2; }'
        self.assertEqual(check_source(source, check_borrows=True), [])

    def test_invalid_return_and_condition(self):
        self.assert_error('fn f() -> i32 { "hello" }', "XE-TYPE-0001")
        self.assert_error("fn f() { if 1 {} }", "XE-TYPE-0001")

    def test_duplicate_declarations(self):
        self.assert_error("fn f() {} fn f() {}", "XE-NAME-0002")
        self.assert_error("fn f() { let x = 1; let x = 2; }", "XE-NAME-0002")

    def test_bounds_and_addressable_storage(self):
        self.assert_error("fn f() { let x: u8 = 256; }", "XE-TYPE-0004")
        self.assert_error("fn f() { let x = [1, 2]; x[2]; }", "XE-TYPE-0004")
        self.assert_error("fn f() { let x = 1@; }", "XE-PARSE-0001")

    def test_unreachable_match_arm(self):
        self.assert_error("enum E { A, B, } fn f(x: E) { x ? { _ :> _ -> unit, E::A :> _ -> unit, }; }",
                          "XE-MATCH-0002")

    def test_break_outside_loop(self):
        self.assert_error("fn f() { break; }", "XE-FLOW-0001")

    def test_loop_cannot_repeatedly_move_outer_resource(self):
        self.assert_error('fn take(x: String) {} fn main() { let x << String::from("x"); for i in 0..2 { take(x); } }',
                          "XE-MOVE-0003")

    def test_error_propagation_type(self):
        self.assert_error('fn a() -> i32?[str] { 1 } fn b() -> i32? { a()?[return] }',
                          "XE-RESULT-0002")

    def test_unsupported_features_are_not_silent_success(self):
        self.assert_error("use other; fn main() {}", "XE-SEM-0001")

    def test_block_assignment_cannot_leak_local_pointer(self):
        self.assert_error('fn f() { var p: i32@; { let x = 1; p = x@; }; println("{}", p#); }',
                          "XE-BORROW-0003")

    def test_temporary_resource_view(self):
        self.assert_error('fn f() -> str { String::from("x").as_str() }', "XE-BORROW-0003")

    def test_owned_parameter_cannot_return_its_field_view(self):
        self.assert_error('struct S { text: String, } fn f(s: S) -> str { s.text.as_str() }',
                          "XE-BORROW-0003")
        self.assertEqual(check_source("fn f(x: i32@) -> i32@ { x }", check_borrows=True), [])
        self.assert_error("fn f(x: i32) -> i32@ { x@ }", "XE-BORROW-0003")

    def test_signed_minimum_literal(self):
        self.assertEqual(check_source("fn f() { let x: i8 = -128; }"), [])
        self.assert_error("fn f() { let x: i8 = -129; }", "XE-TYPE-0004")

    def test_unresolved_maybe_is_not_any(self):
        self.assert_error('fn f() { let x << Maybe::No["bad"]; }', "XE-TYPE-0001")

    def test_type_attachments_and_empty_array(self):
        self.assert_error("fn f(x: Vec) {}", "XE-TYPE-0001")
        self.assert_error("fn f(x: i32[str]) {}", "XE-TYPE-0001")
        self.assertEqual(check_source("fn f() { let x: Array[i32, 0] = []; }"), [])

    def test_generic_call_and_copy_constraint(self):
        source = "fn[T] same(x: T) -> T { x } fn f() { let x: i32 = same(1); }"
        self.assertEqual(check_source(source), [])
        self.assert_error('fn[T: Copy] copy(x: T) -> T { x } fn f() { copy(String::from("x")); }',
                          "XE-OWN-0001")

    def test_partial_move_tracking(self):
        source = '''struct Pair { text: String, count: i32, }
        fn f() {
            let p << Pair { .text << String::from("x"); .count = 1; };
            let text << p.text;
            println("{}", p.count);
        }'''
        self.assertEqual(check_source(source, check_borrows=True), [])
        self.assert_error(source.replace('println("{}", p.count);', 'println("{}", p.text@);'),
                          "XE-MOVE-0001")

    def test_closure_moves_capture_and_is_single_use(self):
        self.assert_error('''fn f() {
            let x << String::from("x");
            let callback << fn[x]() { println("{}", x); };
            callback();
            callback();
        }''', "XE-MOVE-0001")

    def test_random_token_inputs_do_not_crash_semantics(self):
        import random
        randomizer = random.Random(991)
        tokens = ["fn", "f", "x", "let", "var", "=", "<<", ";", "(", ")", "{", "}",
                  "i32", "String", ":", "?", "->", "1", '"s"', "@", "#", "unit"]
        for _ in range(300):
            source = " ".join(randomizer.choices(tokens, k=randomizer.randint(1, 35)))
            check_source(source, check_borrows=True)


class CheckCliTests(unittest.TestCase):
    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(args)
        return status, out.getvalue(), err.getvalue()

    def test_semantic_json_and_no_ast_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.xe"
            path.write_text('fn f() { let x: i32 = "bad"; }')
            status, out, err = self.run_cli([str(path), "--check", "--diagnostic-format", "json"])
            self.assertEqual(status, 1)
            self.assertEqual(out, "")
            self.assertEqual(json.loads(err)["diagnostics"][0]["code"], "XE-TYPE-0001")
            self.assertEqual(list(Path(directory).iterdir()), [path])
            # 同一文件语法合法，仍可以显式输出 AST。
            status, out, err = self.run_cli([str(path), "-o", "-"])
            self.assertEqual(status, 0, err)
            self.assertIn("ast", json.loads(out))

    def test_borrow_mode_and_success_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pointer.xe"
            path.write_text("fn f(p: i32@) { p# = 1; }")
            self.assertEqual(self.run_cli([str(path), "--check"])[0], 0)
            self.assertEqual(self.run_cli([str(path), "--check", "--check-borrows"])[0], 1)
            status, out, err = self.run_cli([str(path), "--check", "--diagnostic-format", "json"])
            self.assertEqual(status, 0, err)
            self.assertEqual(json.loads(out), {"diagnostics": []})
