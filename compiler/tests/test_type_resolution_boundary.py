"""旧前端类型解析边界回归，不是 set/union/newtype 新规范的准入测试。

直接通过 Checker 的既有入口检查搬移前后的结果、环境恢复与诊断位置。
解析出 Type 只证明当前基线接受其形状，不能证明包含、赋值或资源权限。
"""
import copy
import unittest

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.typesys import (
    BOOL,
    I32,
    IO_ERROR,
    NONE,
    UNIT,
    Type,
    callable_type,
    maybe,
    ptr,
)


class TypeResolutionBoundaryTests(LegacyTestCase):
    def checker(self, text=""):
        source = Source(text, "<type-resolution-boundary>")
        return Checker(source, parse_source(text, source.filename))

    def annotation(self, spelling):
        checker = self.checker(f"fn inspect(value:{spelling}){{}}")
        return checker, checker.tree["items"][0]["parameters"][0]["type"]

    def register_aliases(self, text):
        checker = self.checker(text)
        checker.aliases.update({node["name"]: node for node in checker.tree["items"]})
        return checker

    def assert_diagnostic(self, action, code, fragment=None):
        with self.assertRaises(Diagnostic) as caught:
            action()
        error = caught.exception
        self.assertEqual(error.code, code, error.render())
        if fragment is not None:
            self.assertEqual(error.source.text[error.start:error.end], fragment, error.render())
        return error

    def test_composite_types_preserve_type_and_source_ast(self):
        for spelling, expected in (
            ("i32@[mut,unsafe]", ptr(I32, True, True)),
            ("i32?[None]", maybe(I32)),
            ("i32?[String]", maybe(I32, Type("String"))),
            ("fn(i32@,bool)->Unit", callable_type([ptr(I32), BOOL], UNIT)),
            ("tuple[i32,bool@]", Type("tuple", (I32, ptr(BOOL)))),
            ("Array[Vec[i32],0]", Type("Array", (Type("Vec", (I32,)), Type("0")))),
        ):
            with self.subTest(spelling=spelling):
                checker, node = self.annotation(spelling)
                original = copy.deepcopy(checker.tree)
                self.assertEqual(checker.type_of(node), expected)
                self.assertEqual(checker.tree, original)

    def test_omitted_type_and_internal_resolved_type_keep_existing_results(self):
        checker = self.checker()
        resolved = ptr(I32, True)
        self.assertEqual(checker.type_of(None), UNIT)
        self.assertIs(checker.type_of({"kind": "InternalResolvedType", "resolved_type": resolved}), resolved)

    def test_aliases_cache_canonical_type_without_rewriting_ast(self):
        checker = self.register_aliases("type A=B; type B=Vec[i32];")
        original = copy.deepcopy(checker.tree)
        resolved = checker.resolve_alias("A")
        self.assertEqual(resolved, Type("Vec", (I32,)))
        self.assertIs(resolved, checker.resolve_alias("A"))
        self.assertIs(checker.alias_types["A"], checker.alias_types["B"])
        self.assertEqual(checker._alias_stack, [])
        self.assertEqual(checker.tree, original)

    def test_alias_resolution_restores_host_environment_after_success_and_failure(self):
        checker = self.register_aliases("type Number=i32; type Parameter=T; type Owner=Self;")
        generics, substitutions = {"T"}, {"$T": BOOL}
        checker.generic_names = generics
        checker.self_type = Type("Context")
        checker.type_substitutions = substitutions
        self.assertEqual(checker.resolve_alias("Number"), I32)
        for name, fragment in (("Parameter", "T"), ("Owner", "Self")):
            with self.subTest(name=name):
                self.assert_diagnostic(lambda name=name: checker.resolve_alias(name), "XE-NAME-0001", fragment)
                self.assertNotIn(name, checker.alias_types)
                self.assertIs(checker.generic_names, generics)
                self.assertIs(checker.type_substitutions, substitutions)
                self.assertEqual(checker.self_type, Type("Context"))
                self.assertEqual(checker._alias_stack, [])

    def test_alias_cycle_reports_existing_chain_and_cleans_stack(self):
        checker = self.register_aliases("type A=B; type B=A;")
        error = self.assert_diagnostic(lambda: checker.resolve_alias("A"), "XE-TYPE-0007")
        self.assertIn("A -> B -> A", error.message)
        self.assertEqual(checker._alias_stack, [])
        self.assertEqual(checker.alias_types, {})

    def test_function_generic_still_shadows_module_alias(self):
        checker, node = self.annotation("T")
        alias = self.register_aliases("type T=i32;").tree["items"][0]
        checker.aliases["T"] = alias
        self.assertEqual(checker.type_of(node, {"T"}), Type("$T"))
        self.assertEqual(checker.type_of(node, set()), I32)

    def test_self_requires_context_and_does_not_accept_arguments(self):
        checker, node = self.annotation("Self")
        self.assert_diagnostic(lambda: checker.type_of(node), "XE-NAME-0001", "Self")
        self.assertEqual(checker.type_of(node, self_type=Type("Owner")), Type("Owner"))
        checker, node = self.annotation("Self[i32]")
        self.assert_diagnostic(lambda: checker.type_of(node, self_type=Type("Owner")),
                               "XE-TYPE-0001", "i32")

    def test_internal_specialization_retains_invalid_complete_type_attachment(self):
        checker, node = self.annotation("T[i32]")
        internal = {"kind": "InternalResolvedType", "resolved_type": I32,
                    "_invalid_type_application": {"name": "T", "arguments": node["arguments"]}}
        self.assert_diagnostic(lambda: checker.type_of(internal), "XE-TYPE-0001", "i32")

    def test_unknown_attachment_is_diagnosed_before_wrong_arity(self):
        checker, node = self.annotation("Vec[Unknown,i32]")
        self.assert_diagnostic(lambda: checker.type_of(node), "XE-NAME-0001", "Unknown")

    def test_array_slot_retains_literal_grouping_and_strict_host_integer(self):
        checker = self.checker("fn main(){3;}")
        offset = checker.source.text.index("3")
        integer = checker.source.node("Literal", offset, offset + 1, literal_kind="INTEGER", value=3)
        grouped = checker.source.node("Group", offset, offset + 1, expression=integer)
        self.assertEqual(checker.array_length_type(grouped), Type("3"))
        for value, literal_kind in ((True, "INTEGER"), (3, "BYTE"), (-1, "INTEGER")):
            with self.subTest(value=value, literal_kind=literal_kind):
                node = checker.source.node("Literal", offset, offset + 1, literal_kind=literal_kind, value=value)
                self.assert_diagnostic(lambda node=node: checker.array_length_type(node), "XE-TYPE-0001", "3")

    def test_io_alias_and_none_marker_keep_their_existing_categories(self):
        checker, node = self.annotation("std::io::Error")
        self.assertEqual(checker.type_of(node), IO_ERROR)
        checker, node = self.annotation("None")
        self.assert_diagnostic(lambda: checker.type_of(node), "XE-RESULT-0001", "None")
        checker, node = self.annotation("i32?")
        self.assertEqual(checker.type_of(node).args[1], NONE)

    def test_from_fn_preserves_callback_validation_and_generic_deferral(self):
        checker, node = self.annotation("FromFn[fn()->Step[i32]]")
        self.assertEqual(checker.type_of(node).args[0], callable_type([], Type("Step", (I32,))))
        checker, node = self.annotation("FromFn[fn()->i32]")
        self.assert_diagnostic(lambda: checker.type_of(node), "XE-ITER-0001", "FromFn[fn()->i32]")
        checker, node = self.annotation("FromFn[T]")
        self.assertEqual(checker.type_of(node, {"T"}), Type("FromFn", (Type("$T"),)))

    def test_type_resolution_does_not_claim_assignability_or_copy_capability(self):
        checker, node = self.annotation("Vec[i32]")
        resolved = checker.type_of(node)
        self.assertFalse(checker.copyable(resolved))
        self.assertFalse(checker.compatible(resolved, I32))


if __name__ == "__main__":
    unittest.main()
