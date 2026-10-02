"""前端契约测试：只验证 AST/语法，不把语义错误当语法错误。"""
import unittest
from pathlib import Path

from compiler.xe_ast import Diagnostic, parse_source

ROOT = Path(__file__).resolve().parents[2]


def tail(expression: str):
    return parse_source("fn main() { " + expression + " }")["items"][0]["body"]["tail"]


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


class ExpressionTests(unittest.TestCase):
    def test_arithmetic(self):
        node = tail("1 + 2 * 3")
        self.assertEqual(node["operator"], "+")
        self.assertEqual(node["right"]["operator"], "*")

    def test_comparison_chain(self):
        node = tail("a == b == c")
        self.assertEqual(node["kind"], "ComparisonChain")
        self.assertEqual(node["operators"], ["==", "=="])
        self.assertEqual(len(node["operands"]), 3)

    def test_logic(self):
        node = tail("a or b and c")
        self.assertEqual(node["operator"], "or")
        self.assertEqual(node["right"]["operator"], "and")

    def test_range(self):
        self.assertEqual(tail("1..4")["kind"], "Range")
        node = tail("3..")
        self.assertIsNone(node["upper"])
        self.assertIsNone(tail("..4")["lower"])
        self.assertRaises(Diagnostic, tail, "1..2..3")

    def test_pipeline_left_associative(self):
        node = tail("input |> f1 |> f2")
        self.assertEqual(node["kind"], "Pipeline")
        self.assertEqual(node["input"]["kind"], "Pipeline")

    def test_whole_pipeline_result_requires_parentheses(self):
        self.assertRaises(Diagnostic, tail, "input |> f + 1")
        self.assertEqual(tail("(input |> f) + 1")["kind"], "Binary")

    def test_whole_branch_result_requires_parentheses(self):
        self.assertRaises(Diagnostic, tail, "a? 1> f 2> g + 1")
        self.assertEqual(tail("(a? 1> f 2> g) + 1")["kind"], "Binary")

    def test_dispatch_after_pipeline(self):
        node = tail("input |> parse ? 1> good 2> bad")
        self.assertEqual(node["kind"], "Branch")
        self.assertEqual(node["input"]["kind"], "Pipeline")

    def test_propagation_before_pipeline(self):
        node = tail("input?[return] |> use_value")
        self.assertEqual(node["input"]["kind"], "Propagate")

    def test_handler_body_contains_following_pipe(self):
        node = tail("value |> x: i32 -> f(x) |> g")
        self.assertEqual(node["handler"]["kind"], "HandlerBinding")
        self.assertEqual(node["handler"]["body"]["kind"], "Pipeline")

    def test_inline_binding_not_closure(self):
        node = tail("a? 1> value -> value 2> _ -> 0")
        self.assertEqual(node["arms"][0]["handler"]["kind"], "HandlerBinding")
        self.assertEqual(node["arms"][1]["handler"]["parameters"][0]["name"], "_")

    def test_fn_is_a_value(self):
        node = tail("fn[base](x: i32) -> i32 { base + x }")
        self.assertEqual(node["kind"], "AnonymousFunction")
        self.assertEqual(node["captures"][0]["name"], "base")

    def test_return_function_not_call(self):
        node = tail("a? 1> _ -> foo 2> _ -> bar")
        self.assertEqual(node["arms"][0]["handler"]["body"]["kind"], "Name")
        node = tail("a? 1> foo 2> bar")
        self.assertEqual(node["arms"][0]["handler"]["kind"], "FunctionTarget")

    def test_type_postfixes(self):
        module = parse_source("fn f(x: T@[mut, r], y: i32?[Error]) {}")
        parameters = module["items"][0]["parameters"]
        self.assertEqual(parameters[0]["type"]["kind"], "PointerType")
        self.assertEqual(parameters[1]["type"]["kind"], "MaybeType")

    def test_brackets_not_guessed_by_capitalization(self):
        self.assertEqual(tail("Token::Integer[42]")["kind"], "BracketApply")
        self.assertEqual(tail("array[0]")["kind"], "BracketApply")

    def test_literal_values_and_raw(self):
        self.assertEqual(tail("0xff")["value"], 255)
        self.assertEqual(tail('"猫"')["value"], "猫")
        self.assertEqual(tail("'猫'")["value"], "猫")
        self.assertEqual(tail("1e3")["value"], 1000.0)
        self.assertEqual(tail("1_000")["raw"], "1_000")

    def test_ordinary_comparison_whitespace(self):
        for code in ("1>0", "1 > 0"):
            self.assertEqual(tail(code)["kind"], "ComparisonChain")

    def test_comparison_inside_channel_body(self):
        node = tail("a? 1> x -> 1>0 2> _ -> false")
        self.assertEqual(node["arms"][0]["handler"]["body"]["kind"], "ComparisonChain")


class DeclarationTests(unittest.TestCase):
    def test_function_generics_follow_fn_and_inline_bounds_are_constraints(self):
        tree = parse_source("pub fn[T: Measure, U] inspect(value: T@, other: U) -> i32 "
                            "where U implements Copy { value.measure() }")
        function = tree["items"][0]
        self.assertTrue(function["public"])
        self.assertEqual(function["name"], "inspect")
        self.assertEqual([g["name"] for g in function["generics"]], ["T", "U"])
        self.assertEqual([(c["target"]["path"]["parts"], c["trait"]["path"]["parts"])
                          for c in function["constraints"]], [(["T"], ["Measure"]), (["U"], ["Copy"])])

    def test_generic_trait_method_and_extern_prototype(self):
        tree = parse_source('trait Mapper { fn[T: Copy] map(self: Self@, value: T) -> T; } '
                            'extern "C" { fn[T] identity(value: T) -> T; }')
        method = tree["items"][0]["methods"][0]
        self.assertEqual(method["generics"][0]["name"], "T")
        self.assertEqual(method["constraints"][0]["trait"]["path"]["parts"], ["Copy"])
        self.assertIsNone(method["body"])
        self.assertEqual(tree["items"][1]["functions"][0]["generics"][0]["name"], "T")

    def test_old_function_generic_placement_reports_migration(self):
        with self.assertRaises(Diagnostic) as caught:
            parse_source("fn identity[T](value: T) -> T { value }")
        self.assertIn("已移动到 fn 后", caught.exception.message)
        assert caught.exception.hint is not None
        self.assertIn("fn[T] identity", caught.exception.hint)

    def test_generic_closures_not_declared_and_captures_unchanged(self):
        with self.assertRaises(Diagnostic):
            tail("fn[T: Copy](value: T) -> T { value }")
        node = tail("fn[base](value: i32) -> i32 { base + value }")
        self.assertEqual(node["kind"], "AnonymousFunction")
        self.assertEqual(node["captures"][0]["name"], "base")

    def test_function_generic_regions_and_duplicate_parameters(self):
        function = parse_source("fn[region r, T] first(value: T@[r]) -> T@[r] { value }")["items"][0]
        self.assertEqual(function["generics"][0]["category"], "region")
        with self.assertRaises(Diagnostic):
            parse_source("fn[T, T] bad(value: T) {}")

    def test_imports_and_extern(self):
        tree = parse_source('use crate::syntax::{Token, Node as AstNode}; '
                            'extern "C" { fn release(pointer: u8@[unsafe]); }')
        self.assertEqual(tree["items"][0]["names"][1]["alias"], "AstNode")
        self.assertEqual(tree["items"][1]["kind"], "Extern")

    def test_traits_impl_and_generics(self):
        tree = parse_source("""
        trait Display { fn show(self: Self@); }
        struct[T] Node { value: T, }
        impl[T] Display for Node[T] where T implements Display {
            fn show(self: Self@) {}
        }
        impl Copy for Position;
        """)
        self.assertEqual([item["kind"] for item in tree["items"]],
                         ["Trait", "Struct", "Impl", "Impl"])

    def test_constant_arguments_and_function_types(self):
        tree = parse_source("fn f(x: Array[i32, 3], y: fn(i32) -> i32) {}")
        parameters = tree["items"][0]["parameters"]
        self.assertEqual(parameters[0]["type"]["arguments"][1]["value"], 3)
        self.assertEqual(parameters[1]["type"]["kind"], "FunctionType")

    def test_struct_initialization_and_store(self):
        tree = parse_source("fn main() { let result: i32; "
                            "let m << Message { .text << text; .line = 1; }; "
                            "3 |> f >> result; }")
        body = tree["items"][0]["body"]
        self.assertEqual(body["statements"][1]["value"]["kind"], "StructLiteral")
        self.assertEqual(body["statements"][2]["operator"], ">>")

    def test_block_tail_and_semicolon(self):
        self.assertEqual(tail("42")["value"], 42)
        body = parse_source("fn f() { 42; }")["items"][0]["body"]
        self.assertIsNone(body["tail"])

    def test_nested_control_flow(self):
        tree = parse_source("fn f() { for n in 1..4 { if n == 2 { continue; } } "
                            "while ready { break; } if ready { 1 } else { 2 } }")
        body = tree["items"][0]["body"]
        self.assertEqual(len(body["statements"]), 2)
        self.assertEqual(body["tail"]["kind"], "If")

    def test_parenthesized_struct_in_condition(self):
        tree = parse_source("fn f() { if check((Flag { .ready = true; })) { 1 } else { 0 } }")
        self.assertEqual(tree["items"][0]["body"]["tail"]["kind"], "If")

    def test_branch_pointer_and_parameters(self):
        node = tail("token ?[@] { Token::Integer :> number: i64@ -> number#, "
                    "Token::End :> _ -> 0, }")
        self.assertEqual(node["modifiers"], ["borrow"])
        parameter = node["arms"][0]["handler"]["parameters"][0]
        self.assertEqual(parameter["type"]["kind"], "PointerType")

    def test_multiple_payloads_and_none_selector(self):
        node = tail("event ? { Event::Pair :> [x: i32, y: i32] -> x+y, "
                    "Maybe::None :> _ -> 0, }")
        self.assertEqual(len(node["arms"][0]["handler"]["parameters"]), 2)

    def test_selector_filter_and_or(self):
        node = tail("token ? { Token::Integer[0] | Token::Integer[1] :> _ -> 0, "
                    "_ :> _ -> 1, }")
        self.assertEqual(node["arms"][0]["selector"]["kind"], "OrSelector")

    def test_nested_selector_requires_explicit_second_match(self):
        with self.assertRaises(Diagnostic) as caught:
            tail("outer ? { Outer::Wrap[Inner::Number[0]] :> _ -> 0, }")
        self.assertIn("一层", caught.exception.message)
        node = tail("outer ? { Outer::Wrap :> inner -> inner ? { "
                    "Inner::Number :> number -> number, Inner::End :> _ -> 0, }, }")
        self.assertEqual(node["arms"][0]["handler"]["body"]["kind"], "Branch")

    def test_all_target_examples(self):
        paths = sorted((ROOT / "tests/language").glob("*.xe"))
        self.assertGreater(len(paths), 20)
        for path in paths:
            with self.subTest(path=path.name):
                tree = parse_source(path.read_text(encoding="utf-8"), str(path))
                self.assertEqual(tree["kind"], "Module")

    def test_semantic_failures_can_parse(self):
        for name in ("borrow_move_resource.xe", "borrow_match_move.xe",
                     "borrow_scalar_parameter.xe", "closure_capture_copy.xe"):
            path = ROOT / "tests/fails" / name
            with self.subTest(name=name):
                self.assertEqual(parse_source(path.read_text())["kind"], "Module")


class SourceAndFailureTests(unittest.TestCase):
    def test_nested_comments_and_doc_comments(self):
        tree = parse_source("/// API\n/* outer /* inner */ done */ fn f() {}")
        self.assertEqual(len(tree["comments"]), 2)
        self.assertTrue(tree["comments"][0]["documentation"])

    def test_crlf_unicode_offsets(self):
        code = '// 猫\r\nfn f() { "猫" }'
        tree = parse_source(code)
        literal = tree["items"][0]["body"]["tail"]
        start = literal["span"]["start"]
        self.assertEqual(start["line"], 2)
        self.assertEqual(start["offset"], code.index('"'))
        self.assertEqual(code[start["offset"]:literal["span"]["end"]["offset"]], '"猫"')

    def test_every_node_span_is_in_source(self):
        code = "fn f(x: i32) -> i32 { x + 1 }"
        for node in walk(parse_source(code)):
            if "kind" in node and "span" in node:
                self.assertLessEqual(node["span"]["start"]["offset"], node["span"]["end"]["offset"])
                self.assertLessEqual(node["span"]["end"]["offset"], len(code))

    def test_errors_are_source_diagnostics(self):
        cases = [
            "fn f() { let x = 1 }", "fn f() { 1 |> 42; }",
            "fn f() { a? 1> f 1> g; }", "fn f() { a? 1> f; }",
            "fn f() { a? 1> f(_) 2> g; }",
            "fn f() { token? { Token::End => 0, } }",
            "fn f() { value: i32 -> value }",
            "fn f() { let x = \"unterminated; }",
            "/* unclosed", "fn f() { 0xnope }",
            "fn f() { 1 + ; }", "fn f() { (1+2)@ }",
            "fn f() { 1 = 2; }",
        ]
        for code in cases:
            with self.subTest(code=code):
                with self.assertRaises(Diagnostic) as caught:
                    parse_source(code)
                self.assertIn("XE-", caught.exception.render())
                self.assertIn("^", caught.exception.render())

    def test_old_match_hint(self):
        with self.assertRaises(Diagnostic) as caught:
            tail("match token {}")
        self.assertIn("旧 match", caught.exception.message)

    def test_match_is_not_accidentally_a_reserved_identifier(self):
        tree = parse_source("fn match(x: i32) -> i32 { x } fn f() { match(1) }")
        self.assertEqual(tree["items"][1]["body"]["tail"]["kind"], "Call")

    def test_invalid_literals(self):
        for value in ('"\\q"', "b'é'", "'ab'", '"\\uD800"'):
            with self.subTest(value=value):
                self.assertRaises(Diagnostic, tail, value)

    def test_byte_escape(self):
        self.assertEqual(tail("b'\\xe9'")["value"], "é")

    def test_random_token_streams_never_crash(self):
        # 固定 seed：用于发现 EOF、错误附件或缺括号引发的宿主异常，不替代语法测试。
        import random
        randomizer = random.Random(17)
        atoms = ["fn", "main", "let", "var", "{", "}", "(", ")", "[", "]",
                 "x", "i32", "1", ">", "?", ":", ":>", "->", ";", ",", "@", "|>"]
        for _ in range(500):
            code = " ".join(randomizer.choices(atoms, k=randomizer.randrange(1, 50)))
            try:
                parse_source(code)
            except Diagnostic:
                pass
