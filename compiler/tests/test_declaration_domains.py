"""Declaration-only adapter checks, independent of source parsing and legacy.

The direct AST probes are intentional: unknown fields or forged literal values
must be refused even when a caller did not use the production parser.
"""
import copy
import unittest
from itertools import permutations, product

from compiler.xe_ast.declaration_domains import resolve_declaration_domain
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.type_domains import (
    NEVER_DOMAIN,
    UNIT_VALUE,
    Constant,
    contains,
    equivalent,
    finite,
    is_subset,
    scalar_domain,
)
from compiler.xe_ast.typesys import BOOL, I32, NEVER, UNIT, Type, ptr


class DeclarationDomainTests(unittest.TestCase):
    def setUp(self):
        self.source = Source("type Small = set {1:i32,2:i32};", "domains.xe")
        self.aliases = {}

    def node(self, kind, **fields):
        return self.source.node(kind, 13, 16, **fields)

    def named(self, name):
        return self.node("NamedType", path=self.node("Path", parts=[name]), arguments=[])

    def literal(self, value):
        if type(value) is bool:
            raw = "true" if value else "false"
            return self.node("Literal", literal_kind=raw, raw=raw, value=value)
        if value is None:
            return self.node("Literal", literal_kind="unit", raw="unit", value=None)
        if value < 0:
            return self.node("Unary", operator="-", operand=self.literal(-value))
        return self.node("Literal", literal_kind="INTEGER", raw=str(value), value=value)

    def member(self, value, annotation=None):
        return self.node("SetMember", value=self.literal(value),
                         type=self.named(annotation) if isinstance(annotation, str) else annotation)

    def set_type(self, *members):
        return self.node("SetType", members=list(members))

    def union(self, left, right, operator="|"):
        return self.node("UnionType", operator=operator, left=left, right=right)

    def resolve(self, node):
        return resolve_declaration_domain(self.source, node, self.aliases.get)

    def reject(self, node, code="XE-CORE-0001", message=None):
        with self.assertRaises(Diagnostic) as caught:
            self.resolve(node)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.source.filename, "domains.xe")
        if message:
            self.assertIn(message, caught.exception.message)
        return caught.exception

    def test_empty_domain_and_builtin_never_have_identical_meaning(self):
        self.assertEqual(self.resolve(self.set_type()), NEVER_DOMAIN)
        self.assertEqual(self.resolve(self.named("Never")), NEVER_DOMAIN)

    def test_explicit_integer_members_preserve_carrier_and_order_is_irrelevant(self):
        expected = finite(Constant(I32, value) for value in (1, 2, 3))
        for values in permutations((1, 2, 3)):
            tree = self.set_type(*(self.member(value, "i32") for value in values))
            self.assertEqual(self.resolve(tree), expected)
        narrow = self.resolve(self.set_type(self.member(1, "u8")))
        self.assertNotEqual(narrow, self.resolve(self.set_type(self.member(1, "i32"))))

    def test_all_fixed_machine_integer_boundaries_include_negative_minimum(self):
        for prefix, bits in product(("i", "u"), (8, 16, 32, 64)):
            minimum = -(1 << (bits - 1)) if prefix == "i" else 0
            maximum = (1 << (bits - (prefix == "i"))) - 1
            name = f"{prefix}{bits}"
            for value in (minimum, maximum):
                with self.subTest(name=name, value=value):
                    domain = self.resolve(self.set_type(self.member(value, name)))
                    self.assertTrue(contains(domain, Constant(Type(name), value)))
            for value in (minimum - 1, maximum + 1):
                with self.subTest(name=name, invalid=value):
                    self.reject(self.set_type(self.member(value, name)), "XE-TYPE-0001")

    def test_bool_and_unit_need_no_guessed_integer_type(self):
        self.assertEqual(self.resolve(self.set_type(self.member(True))),
                         finite([Constant(BOOL, True)]))
        self.assertEqual(self.resolve(self.set_type(self.member(False), self.member(True))),
                         scalar_domain(BOOL))
        self.assertEqual(self.resolve(self.set_type(self.member(None))), scalar_domain(UNIT))
        self.assertTrue(contains(self.resolve(self.set_type(self.member(None))),
                                 Constant(UNIT, UNIT_VALUE)))

    def test_matching_explicit_bool_and_unit_annotations_are_not_conversions(self):
        self.assertEqual(self.resolve(self.set_type(self.member(True, "bool"))),
                         finite([Constant(BOOL, True)]))
        self.assertEqual(self.resolve(self.set_type(self.member(None, "Unit"))), scalar_domain(UNIT))

    def test_integer_annotations_may_name_resolved_transparent_aliases(self):
        self.aliases["Byte"] = Type("u8")
        self.aliases["Whole"] = scalar_domain(I32)
        self.assertTrue(contains(self.resolve(self.set_type(self.member(4, "Byte"))),
                                 Constant(Type("u8"), 4)))
        self.assertTrue(contains(self.resolve(self.set_type(self.member(4, "Whole"))), Constant(I32, 4)))

    def test_finite_and_empty_annotations_are_pending_not_implicitly_constructed(self):
        self.aliases["Small"] = finite([Constant(I32, 1)])
        self.aliases["Empty"] = NEVER_DOMAIN
        for name in ("Small", "Empty", "Never"):
            self.reject(self.set_type(self.member(1, name)), message="尚未确定")

    def test_full_boolean_domain_alias_can_be_used_as_its_known_carrier(self):
        self.aliases["Bool"] = self.resolve(self.set_type(self.member(False), self.member(True)))
        self.assertEqual(self.resolve(self.set_type(self.member(True, "Bool"))),
                         finite([Constant(BOOL, True)]))

    def test_set_members_need_explicit_integer_types_without_default_i32(self):
        for value in (0, 1, -1):
            self.reject(self.set_type(self.member(value)), message="默认整数类型未确定")

    def test_non_decimal_integer_spellings_are_still_pending(self):
        for raw, value in (("0xff", 255), ("0o10", 8), ("0b10", 2), ("1_000", 1000)):
            member = self.member(value, "i32")
            member["value"]["raw"] = raw
            self.reject(self.set_type(member), message="拼写")

    def test_duplicate_declarations_are_pending_even_if_domains_would_normalize(self):
        for value, annotation in ((1, "i32"), (True, None), (None, None)):
            self.reject(self.set_type(self.member(value, annotation), self.member(value, annotation)),
                        message="重复")

    def test_type_inconsistent_annotation_cannot_sneak_in_a_conversion(self):
        for value, annotation in ((True, "i32"), (1, "bool"), (None, "i32"),
                                  (1, "Unit"), (False, "Unit"), (None, "bool")):
            self.reject(self.set_type(self.member(value, annotation)), "XE-TYPE-0001",
                        message="不是转换")

    def test_mixed_set_carriers_remain_pending_without_numeric_widening(self):
        self.reject(self.set_type(self.member(1, "u8"), self.member(2, "i32")), message="混合载体")
        self.reject(self.set_type(self.member(True), self.member(1, "i32")), message="混合载体")

    def test_union_spellings_share_one_normalization(self):
        a = self.set_type(self.member(1, "i32"))
        b = self.set_type(self.member(2, "i32"))
        self.assertEqual(self.resolve(self.union(a, b)), self.resolve(self.union(a, b, "union")))
        self.assertEqual(self.resolve(self.union(a, b)), finite([Constant(I32, 1), Constant(I32, 2)]))

    def test_union_laws_and_internal_subset_have_independent_expected_members(self):
        a, b, c = (self.set_type(self.member(value, "i32")) for value in (1, 2, 3))
        empty = self.set_type()
        self.assertTrue(equivalent(self.resolve(self.union(a, b)), self.resolve(self.union(b, a))))
        self.assertTrue(equivalent(self.resolve(self.union(self.union(a, b), c)),
                                   self.resolve(self.union(a, self.union(b, c)))))
        self.assertEqual(self.resolve(self.union(a, a)), self.resolve(a))
        self.assertEqual(self.resolve(self.union(a, empty)), self.resolve(a))
        self.assertEqual(self.resolve(self.union(empty, a)), self.resolve(a))
        combined = self.resolve(self.union(self.union(a, b), c))
        for value in range(-1, 5):
            self.assertEqual(contains(combined, Constant(I32, value)), value in {1, 2, 3})
        self.assertTrue(is_subset(self.resolve(a), combined))
        self.assertFalse(is_subset(combined, self.resolve(a)))

    def test_full_carrier_absorbs_only_same_carrier_finite_subsets(self):
        small = self.set_type(self.member(1, "i32"))
        self.assertEqual(self.resolve(self.union(self.named("i32"), small)), scalar_domain(I32))
        self.assertEqual(self.resolve(self.union(small, self.named("i32"))), scalar_domain(I32))
        self.assertEqual(self.resolve(self.union(self.named("bool"), self.named("Never"))),
                         scalar_domain(BOOL))

    def test_heterogeneous_nonempty_unions_report_implementation_gap_not_layout_choice(self):
        for left, right in ((self.named("i32"), self.named("u8")),
                            (self.named("bool"), self.named("Unit")),
                            (self.named("i32"), self.set_type(self.member(1, "u8")))):
            self.reject(self.union(left, right), "XE-CORE-0002")

    def test_unknown_names_report_name_error_not_default_carriers(self):
        self.reject(self.named("Missing"), "XE-NAME-0001", message="Missing")
        self.reject(self.set_type(self.member(1, "Missing")), "XE-NAME-0001")

    def test_unconnected_carriers_are_not_inherited_from_internal_experimental_model(self):
        for name in ("char", "str", "f32", "isize", "usize", "Record"):
            self.aliases[name] = Type(name)
            self.reject(self.named(name), "XE-CORE-0002")
        self.aliases["Pointer"] = ptr(I32)
        self.aliases["Nominal"] = Type("i32", identity=1)
        self.aliases["Unsafe"] = Type("i32", unsafe=True)
        for name in ("Pointer", "Nominal", "Unsafe"):
            self.reject(self.named(name), "XE-CORE-0002")

    def test_negative_unit_bool_and_arbitrary_expressions_are_not_evaluated(self):
        for value in (True, None):
            member = self.member(value)
            member["value"] = self.node("Unary", operator="-", operand=member["value"])
            self.reject(self.set_type(member))
        for operator in ("+", "bitnot", "not"):
            member = self.member(1, "i32")
            member["value"] = self.node("Unary", operator=operator, operand=member["value"])
            self.reject(self.set_type(member))
        member = self.member(1, "i32")
        member["value"] = self.node("Call", callee=self.named("factory"), arguments=[])
        self.reject(self.set_type(member))

    def test_malformed_node_shapes_and_unknown_fields_cannot_approve_features(self):
        trees = [None, [], {"kind": []}, {"kind": "Unknown"},
                 self.node("SetType", members=None), self.node("SetType"),
                 self.node("SetType", members=[None])]
        set_node = self.set_type(self.member(1, "i32"))
        member_node, literal_node = set_node["members"][0], set_node["members"][0]["value"]
        annotation = member_node["type"]
        for target in (set_node, member_node, literal_node, annotation, annotation["path"]):
            original = copy.deepcopy(set_node)
            target["approved"] = True
            self.reject(set_node, message="未确认字段")
            del target["approved"]
            self.assertEqual(set_node, original)
        for tree in trees:
            self.reject(tree)

    def test_malformed_leaf_fields_report_diagnostics_instead_of_python_exceptions(self):
        for field, bad in (("literal_kind", []), ("raw", None), ("value", True),
                           ("raw", "2"), ("raw", " 1"), ("raw", "1 ")):
            member = self.member(1, "i32")
            member["value"][field] = bad
            self.reject(self.set_type(member))
        for bad in (None, "", [], 1):
            node = self.union(self.named("i32"), self.named("i32"))
            node["operator"] = bad
            self.reject(node)
        for bad in (None, [], ["A", "B"], ["_"], [1], ["类型"]):
            node = self.named("i32")
            node["path"]["parts"] = bad
            self.reject(node)
        for bad in (None, [self.named("i32")]):
            node = self.named("i32")
            node["arguments"] = bad
            self.reject(node)

    def test_metadata_does_not_grant_admission_and_ast_is_not_mutated(self):
        tree = self.set_type(self.member(1, "i32"))
        tree["_file"], tree["_module"] = "elsewhere.xe", "untrusted"
        snapshot = copy.deepcopy(tree)
        self.resolve(tree)
        self.assertEqual(tree, snapshot)
        tree["members"][0]["value"]["raw"] = "0xff"
        self.reject(tree)

    def test_cyclic_ast_or_excessive_nesting_is_a_source_diagnostic(self):
        cycle = self.union(self.named("i32"), self.named("Never"))
        cycle["left"] = cycle
        self.reject(cycle, message="循环或嵌套过深")
        deep = self.named("i32")
        for _ in range(130):
            deep = self.union(deep, self.named("Never"))
        self.reject(deep, message="循环或嵌套过深")

    def test_diagnostic_span_is_preserved_or_safely_sanitized(self):
        error = self.reject(self.set_type(self.member(1)), message="默认整数")
        self.assertEqual((error.start, error.end), (13, 16))
        for span in (None, {"start": []}, {"start": {"offset": "bad"}},
                     {"start": {"offset": -50}, "end": {"offset": 500}}):
            node = self.named("Missing")
            node["span"] = span
            error = self.reject(node, "XE-NAME-0001")
            self.assertTrue(0 <= error.start <= error.end <= len(self.source.text))

    def test_alias_lookup_results_are_reused_without_mutation_or_type_identity_changes(self):
        small = finite([Constant(I32, 1)])
        self.aliases.update({"Small": small, "Plain": I32, "Bottom": NEVER})
        self.assertIs(self.resolve(self.named("Small")), small)
        self.assertEqual(self.resolve(self.named("Plain")), scalar_domain(I32))
        self.assertEqual(self.resolve(self.named("Bottom")), NEVER_DOMAIN)
        self.assertEqual(self.aliases, {"Small": small, "Plain": I32, "Bottom": NEVER})


if __name__ == "__main__":
    unittest.main()
