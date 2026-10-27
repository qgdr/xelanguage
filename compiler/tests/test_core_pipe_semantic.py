"""Narrow handler-component regressions, not source-admission approval.

Public core admits the reviewed pipelines, coordinate-access and minimal enum
subset. These tests construct the existing parser
AST through its lower-level component, then exercise only the reviewed
zero/one-input and discard rules. They do not establish executable else,
resource-branch or pointer-match semantics.
"""
import unittest

from compiler.xe_ast.backend_c import AnnotatedChecker
from compiler.xe_ast.language_policy import current_profile, language_profile
from compiler.xe_ast.parser import Parser
from compiler.xe_ast.semantic import Value, check_source
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.typesys import I32, STRING, UNIT, Type


class _ObservedChecker(AnnotatedChecker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.declared_names = []

    def declare(self, name, *args, **kwargs):
        self.declared_names.append(name)
        return super().declare(name, *args, **kwargs)


class CorePipeHandlerTests(unittest.TestCase):
    def component(self, handler_text):
        text = ("fn identity(value:i32)->i32 { value } "
                "fn zero()->i32 { 42 } "
                f"fn probe()->i32 {{ 1 |> {handler_text} }}")
        source = Source(text, "<handler-component>")
        # Preserve old AST shapes for negative component tests even when the
        # core parser already rejects their source spelling. The handler is
        # checked under the caller's restored profile, never auto-retried.
        with language_profile("legacy"):
            tree = Parser(source).module()
        checker = _ObservedChecker(source, tree)
        checker.collect()
        checker.scopes = [{}]
        pipeline = tree["items"][-1]["body"]["tail"]
        return checker, pipeline["handler"], pipeline["input"]

    def invoke(self, handler_text, input_types=(I32,)):
        checker, handler, input_node = self.component(handler_text)
        payloads = [Value(type_, input_node) for type_ in input_types]
        result = checker.handle(handler, payloads, I32)
        return checker, handler, result

    def assert_arity_error(self, handler_text, input_types):
        with self.assertRaises(Diagnostic) as caught:
            self.invoke(handler_text, input_types)
        self.assertEqual(caught.exception.code, "XE-CALL-0001", caught.exception.render())

    def test_untyped_and_typed_bindings_receive_one_value(self):
        for handler in ("value -> value", "value:i32 -> value"):
            with self.subTest(handler=handler):
                checker, _, result = self.invoke(handler)
                self.assertEqual(result.type, I32)
                self.assertEqual(checker.declared_names, ["value"])

    def test_untyped_discard_never_declares_a_source_name(self):
        checker, handler, result = self.invoke("_ -> 42")
        self.assertEqual(result.type, I32)
        self.assertEqual(checker.declared_names, [])
        parameter = handler["parameters"][0]
        self.assertEqual(checker.binding_types[id(parameter)], I32)
        self.assertNotIn("name", checker.inferred_types[id(parameter)])

    def test_typed_discard_checks_and_records_type_without_a_binding(self):
        checker, handler, result = self.invoke("_:i32 -> 42")
        self.assertEqual(result.type, I32)
        self.assertEqual(checker.declared_names, [])
        self.assertEqual(checker.binding_types[id(handler["parameters"][0])], I32)

    def test_discard_does_not_skip_its_annotation_check(self):
        with self.assertRaises(Diagnostic) as caught:
            self.invoke("_:bool -> 42")
        self.assertEqual(caught.exception.code, "XE-TYPE-0001")

    def test_even_a_handcrafted_body_cannot_read_the_discard_token(self):
        checker, handler, input_node = self.component("_:i32 -> 42")
        span = handler["body"]["span"]
        handler["body"] = {"kind": "Name", "span": span,
                           "path": {"kind": "Path", "span": span, "parts": ["_"]}}
        with self.assertRaises(Diagnostic) as caught:
            checker.handle(handler, [Value(I32, input_node)], I32)
        self.assertEqual(caught.exception.code, "XE-NAME-0001")
        self.assertEqual(checker.declared_names, [])

    def test_resource_discard_records_an_owned_slot_without_a_readable_name(self):
        # DEC-024: no source binding does not mean no cleanup obligation.
        # Runtime lifetime/exit order is tested in the discard backend suite.
        for text in ("_ -> 42", "_:String -> 42"):
            with self.subTest(text=text):
                checker, handler, result = self.invoke(text, (STRING,))
                self.assertEqual(result.type, I32)
                self.assertEqual(checker.binding_types[id(handler["parameters"][0])], STRING)
                self.assertEqual(checker.declared_names, [])

    def test_zero_payload_does_not_become_unit_or_an_ignored_input(self):
        for handler in ("_ -> 42", "_:Unit -> 42", "value -> 42"):
            with self.subTest(handler=handler):
                self.assert_arity_error(handler, ())

    def test_unit_is_one_input_and_can_be_ignored(self):
        checker, handler, result = self.invoke("_:Unit -> 42", (UNIT,))
        self.assertEqual(result.type, I32)
        self.assertEqual(checker.binding_types[id(handler["parameters"][0])], UNIT)
        self.assertEqual(checker.declared_names, [])

    def test_function_targets_keep_exact_zero_or_one_parameter_arity(self):
        for handler, input_types in (("zero", ()), ("identity", (I32,))):
            with self.subTest(handler=handler):
                _, _, result = self.invoke(handler, input_types)
                self.assertEqual(result.type, I32)
        self.assert_arity_error("zero", (I32,))
        self.assert_arity_error("identity", ())

    def test_multiple_inputs_and_legacy_binding_lists_are_not_implicit_spreads(self):
        for handler in ("_ -> 42", "identity", "[a,b] -> 42"):
            with self.subTest(handler=handler):
                self.assert_arity_error(handler, (I32, I32))
        self.assert_arity_error("[value] -> value", ())
        self.assert_arity_error("[a,b] -> 42", (I32,))

    def test_one_tuple_value_is_not_multiple_inputs(self):
        pair = Type("tuple", (I32, I32))
        checker, handler, result = self.invoke("_ -> 42", (pair,))
        self.assertEqual(result.type, I32)
        self.assertEqual(checker.binding_types[id(handler["parameters"][0])], pair)

    def test_unknown_handler_kind_is_not_treated_as_an_implicit_binding(self):
        checker, handler, input_node = self.component("value -> value")
        handler["kind"] = "FutureHandler"
        with self.assertRaises(Diagnostic) as caught:
            checker.handle(handler, [Value(I32, input_node)], I32)
        self.assertEqual(caught.exception.code, "XE-CORE-0001")

    def test_explicit_legacy_retains_zero_and_multiple_input_discard(self):
        with language_profile("legacy"):
            for input_types in ((), (I32,), (I32, I32)):
                with self.subTest(input_types=input_types):
                    checker, _, result = self.invoke("_ -> 42", input_types)
                    self.assertEqual(result.type, I32)
                    self.assertEqual(checker.declared_names, [])
        self.assertEqual(current_profile(), "core")

    def test_ordinary_function_calls_still_copy_basic_inputs(self):
        self.assertEqual(check_source('''fn identity(value:i32)->i32 { value }
fn main()->Unit {
    let value:i32 = 42;
    let answer:i32 = identity(value);
    println("{} {}", answer, value);
}'''), [])


if __name__ == "__main__":
    unittest.main()
