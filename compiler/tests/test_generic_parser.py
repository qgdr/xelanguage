"""泛型附件、关联路径和普通 [] 的边界；执行另由泛型后端测试覆盖。"""
import unittest

from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Diagnostic


class GenericParserTests(unittest.TestCase):
    def value(self, expression):
        return parse_source(f"fn main() {{ let value = {expression}; }}")["items"][0]["body"]["statements"][0]["value"]

    def test_associated_call_retains_concrete_owner_type(self):
        node = self.value("Holder[i32]::new(10)")
        self.assertEqual(node["kind"], "Call")
        self.assertEqual(node["callee"]["kind"], "AssociatedAccess")
        self.assertEqual(node["callee"]["member"], "new")
        self.assertEqual(node["callee"]["object"]["kind"], "BracketApply")

    def test_generic_enum_payload_is_distinct_from_type_arguments(self):
        node = self.value("Token[i32]::Value[42]")
        self.assertEqual(node["kind"], "BracketApply")
        self.assertEqual(node["object"]["kind"], "AssociatedAccess")
        self.assertEqual(node["object"]["member"], "Value")
        self.assertEqual(node["arguments"][0]["kind"], "Literal")

    def test_nested_type_arguments_keep_bracket_boundaries(self):
        node = self.value("identity[Holder[Array[i32, 2]]](value)")
        inner = node["callee"]["arguments"][0]
        self.assertEqual(inner["kind"], "BracketApply")
        self.assertEqual(inner["arguments"][0]["object"]["path"]["parts"], ["Array"])

    def test_pointer_type_arguments_preserve_modifiers(self):
        node = self.value("identity[i32@[mut, unsafe]](pointer)")
        argument = node["callee"]["arguments"][0]
        self.assertEqual(argument["kind"], "Borrow")
        self.assertEqual([m["name"] for m in argument["modifiers"]], ["mut", "unsafe"])

    def test_result_type_arguments_use_type_suffix_not_branch(self):
        node = self.value("identity[i32?](value)")
        self.assertEqual(node["callee"]["arguments"][0]["kind"], "MaybeTypeAttachment")
        node = self.value("identity[i32?[ConversionError]](value)")
        self.assertEqual(node["callee"]["arguments"][0]["error"]["kind"], "NamedType")

    def test_function_type_argument_does_not_require_closure_body(self):
        node = self.value("identity[fn(i32) -> i32](callback)")
        self.assertEqual(node["callee"]["arguments"][0]["kind"], "FunctionType")

    def test_ordinary_function_value_in_array_is_not_a_function_type(self):
        node = self.value("[fn(x: i32) -> i32 { x }]")
        self.assertEqual(node["elements"][0]["kind"], "AnonymousFunction")

    def test_index_branch_unwrap_is_not_a_type_suffix(self):
        node = self.value("items[index?[panic]]")
        self.assertEqual(node["arguments"][0]["kind"], "Unwrap")

    def test_explicit_generic_method_keeps_object_and_method(self):
        node = self.value("item.convert[u8](value)")
        self.assertEqual(node["callee"]["object"]["kind"], "FieldAccess")
        self.assertEqual(node["callee"]["object"]["field"], "convert")

    def test_pipe_and_channel_targets_retain_generic_attachments(self):
        node = self.value("42 |> identity[i32]")
        self.assertEqual(node["handler"]["target"]["kind"], "BracketApply")
        node = self.value("result? 1> identity[i32] 2> fallback")
        self.assertEqual(node["arms"][0]["handler"]["target"]["kind"], "BracketApply")
        node = self.value("42 |> Holder[i32]::new")
        self.assertEqual(node["handler"]["target"]["kind"], "AssociatedAccess")

    def test_generic_pipe_does_not_enable_direct_call_syntax(self):
        for text in ("42 |> identity[i32](42)", "result? 1> identity[i32](42) 2> fallback"):
            with self.subTest(text=text):
                with self.assertRaises(Diagnostic) as caught:
                    self.value(text)
                self.assertIn("管道后不能直接写函数调用", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
