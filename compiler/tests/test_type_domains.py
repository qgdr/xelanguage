"""Independent finite-set checks for the experimental scalar domain algebra.

This module deliberately does not parse new Xe syntax or call ``compatible``.
The reference sets below use plain Python values, not the implementation's
normalization/subset functions, so the algebra tests can detect shared mistakes
in those functions rather than merely compare one API with another.
"""
import unittest
from dataclasses import FrozenInstanceError
from itertools import combinations, permutations, product

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.type_domains import (
    NEVER_DOMAIN,
    UNIT_VALUE,
    Constant,
    Domain,
    DomainError,
    DuplicateMemberError,
    contains,
    equivalent,
    finite,
    is_subset,
    scalar_domain,
    union,
)
from compiler.xe_ast.typesys import BOOL, I32, NEVER, STR, UNIT, Type


class TypeDomainConstantTests(LegacyTestCase):
    def assert_invalid_constant(self, carrier, value):
        # Invalid host inputs are intentional runtime-validation probes.  Keep
        # them behind this untyped boundary rather than suppressing Pyright.
        with self.assertRaises(DomainError):
            Constant(carrier, value)

    def test_supported_constants_keep_their_machine_carrier(self):
        for carrier, value in (
            (BOOL, False), (BOOL, True), (I32, 42),
            (Type("u64"), (1 << 64) - 1),
            (Type("char"), "\U0001f600"), (STR, "hello\0世界"),
            (UNIT, UNIT_VALUE),
        ):
            with self.subTest(carrier=carrier, value=value):
                constant = Constant(carrier, value)
                self.assertEqual(constant.carrier, carrier)
                self.assertEqual(constant.value, value)

    def test_equal_numeric_values_do_not_erase_type_identity(self):
        constants = (
            Constant(BOOL, True), Constant(Type("u8"), 1),
            Constant(I32, 1), Constant(Type("u64"), 1),
        )
        self.assertEqual(len(set(constants)), 4)
        for left, right in combinations(constants, 2):
            self.assertNotEqual(left, right)

    def test_boolean_is_not_a_host_integer_and_integer_is_not_a_boolean(self):
        for carrier, value in (
            (I32, True), (I32, False), (Type("u8"), True),
            (BOOL, 0), (BOOL, 1), (BOOL, "true"), (BOOL, None),
        ):
            with self.subTest(carrier=carrier, value=value):
                self.assert_invalid_constant(carrier, value)

    def test_all_fixed_width_integer_boundaries_are_checked(self):
        for prefix, bits in product(("i", "u"), (8, 16, 32, 64)):
            carrier = Type(f"{prefix}{bits}")
            minimum = -(1 << (bits - 1)) if prefix == "i" else 0
            maximum = (1 << (bits - (prefix == "i"))) - 1
            with self.subTest(carrier=carrier):
                self.assertEqual(Constant(carrier, minimum).value, minimum)
                self.assertEqual(Constant(carrier, maximum).value, maximum)
                for invalid in (minimum - 1, maximum + 1, 1.0, "1", None):
                    with self.subTest(invalid=invalid):
                        self.assert_invalid_constant(carrier, invalid)

    def test_unit_has_an_explicit_value_not_python_none(self):
        self.assertEqual(Constant(UNIT, UNIT_VALUE), Constant(UNIT, UNIT_VALUE))
        for value in (None, (), "unit", 0, False):
            with self.subTest(value=value):
                self.assert_invalid_constant(UNIT, value)

    def test_char_is_one_unicode_scalar_not_one_byte_or_one_grapheme(self):
        for value in ("a", "\0", "é", "\U0001f600", "\U0010ffff"):
            with self.subTest(value=value):
                self.assertEqual(Constant(Type("char"), value).value, value)
        for value in ("", "ab", "e\u0301", "\ud800", "\udfff", 65, None):
            with self.subTest(value=repr(value)):
                self.assert_invalid_constant(Type("char"), value)

    def test_str_rejects_surrogates_and_does_not_invent_unicode_normalization(self):
        for value in ("", "\0", "世界", "é", "e\u0301", "\U0001f600"):
            with self.subTest(value=value):
                self.assertEqual(Constant(STR, value).value, value)
        self.assertNotEqual(Constant(STR, "é"), Constant(STR, "e\u0301"))
        for value in ("\ud800", "x\udfffy", b"abc", None, 1):
            with self.subTest(value=repr(value)):
                self.assert_invalid_constant(STR, value)

    def test_unsupported_carriers_are_rejected_even_for_empty_domains(self):
        carriers = (
            NEVER, Type("None"), Type("usize"), Type("isize"),
            Type("f32"), Type("f64"), Type("String"), Type("UserType"),
            Type("ptr", (I32,)), Type("fn", (I32, I32)),
            Type("ptr"), Type("fn"), Type("closure"), Type("maybe"),
            Type("maybe", (I32, UNIT)), Type("$unknown"),
            Type("bool", (I32,)), Type("bool", mutable=True),
            Type("bool", identity=1), Type("bool", unsafe=True),
        )
        for carrier in carriers:
            with self.subTest(carrier=repr(carrier)):
                for members in (frozenset(), None):
                    with self.assertRaises(DomainError):
                        Domain(carrier, members)
                with self.assertRaises(DomainError):
                    Constant(carrier, 0)

    def test_constant_and_domain_are_immutable_and_hashable(self):
        constant = Constant(I32, 42)
        domain = finite([constant])
        self.assertEqual({constant: "value"}[Constant(I32, 42)], "value")
        self.assertEqual({domain: "domain"}[finite([Constant(I32, 42)])], "domain")
        for instance, attribute, value in ((constant, "value", 7), (domain, "carrier", BOOL)):
            with self.assertRaises(FrozenInstanceError):
                setattr(instance, attribute, value)


class TypeDomainConstructionTests(LegacyTestCase):
    def assert_invalid_domain(self, carrier, members):
        with self.assertRaises(DomainError):
            Domain(carrier, members)

    def test_empty_domain_is_canonical_and_not_unit(self):
        self.assertEqual(NEVER_DOMAIN, Domain(None, frozenset()))
        self.assertEqual(finite([]), NEVER_DOMAIN)
        self.assertEqual(scalar_domain(NEVER), NEVER_DOMAIN)
        for carrier in (BOOL, I32, STR, UNIT):
            with self.subTest(carrier=carrier):
                self.assertEqual(Domain(carrier, frozenset()), NEVER_DOMAIN)
        self.assertNotEqual(NEVER_DOMAIN, scalar_domain(UNIT))
        self.assertFalse(equivalent(NEVER_DOMAIN, scalar_domain(UNIT)))
        self.assertFalse(contains(NEVER_DOMAIN, Constant(UNIT, UNIT_VALUE)))

    def test_nonempty_domain_needs_one_carrier_and_matching_constants(self):
        integer = Constant(I32, 1)
        for carrier, members in (
            (None, None), (None, frozenset([integer])),
            (BOOL, frozenset([integer])),
            (I32, frozenset([integer, Constant(Type("u8"), 1)])),
            (I32, frozenset([1])),
        ):
            with self.subTest(carrier=carrier, members=members):
                self.assert_invalid_domain(carrier, members)

    def test_mutable_or_untyped_member_containers_are_rejected(self):
        for members in ([], set(), [Constant(I32, 1)], (Constant(I32, 1),)):
            with self.subTest(members=members):
                self.assert_invalid_domain(I32, members)

    def test_finite_requires_typed_constants_not_bare_host_values(self):
        def reject(values):
            with self.assertRaises(DomainError):
                finite(values)
        for values in ([1], [None], [True], ["unit"], [Constant(I32, 1), 2]):
            with self.subTest(values=values):
                reject(values)

    def test_finite_rejects_cross_carrier_values_in_either_order(self):
        values = (Constant(I32, 1), Constant(Type("u8"), 1))
        for ordering in permutations(values):
            with self.subTest(ordering=ordering):
                with self.assertRaises(DomainError):
                    finite(ordering)

    def test_duplicate_members_report_both_zero_based_positions(self):
        values = [Constant(STR, "a"), Constant(STR, "b"), Constant(STR, "a")]
        with self.assertRaises(DuplicateMemberError) as caught:
            finite(values)
        self.assertEqual(caught.exception.first_index, 0)
        self.assertEqual(caught.exception.duplicate_index, 2)
        self.assertIsInstance(caught.exception, DomainError)

    def test_duplicate_check_precedes_bool_full_domain_normalization(self):
        with self.assertRaises(DuplicateMemberError) as caught:
            finite([Constant(BOOL, False), Constant(BOOL, True), Constant(BOOL, False)])
        self.assertEqual(caught.exception.first_index, 0)
        self.assertEqual(caught.exception.duplicate_index, 2)

    def test_finite_consumes_iterables_once_and_has_no_order_identity(self):
        values = [Constant(I32, number) for number in (1, 2, 3)]
        expected = finite(values)
        for ordering in permutations(values):
            with self.subTest(ordering=ordering):
                self.assertEqual(finite(iter(ordering)), expected)
        # A mutable input list must not become shared model storage.
        values.append(Constant(I32, 4))
        self.assertFalse(contains(expected, Constant(I32, 4)))

    def test_supported_full_domains_have_distinct_carriers(self):
        carriers = [BOOL, STR, UNIT, Type("char")]
        carriers += [Type(f"{prefix}{bits}")
                     for prefix, bits in product(("i", "u"), (8, 16, 32, 64))]
        for carrier in carriers:
            with self.subTest(carrier=carrier):
                domain = scalar_domain(carrier)
                self.assertEqual(domain, Domain(carrier, None))
                self.assertEqual(domain.carrier, carrier)
                self.assertIsNone(domain.members)
        self.assertEqual(len(set(map(scalar_domain, carriers))), len(carriers))

    def test_full_bool_unit_and_byte_sets_are_normalized(self):
        self.assertEqual(finite([Constant(BOOL, False), Constant(BOOL, True)]),
                         scalar_domain(BOOL))
        self.assertEqual(finite([Constant(UNIT, UNIT_VALUE)]), scalar_domain(UNIT))
        for carrier, values in ((Type("u8"), range(256)),
                                (Type("i8"), range(-128, 128))):
            with self.subTest(carrier=carrier):
                full = finite(Constant(carrier, value) for value in values)
                self.assertEqual(full, scalar_domain(carrier))
                self.assertIsNone(full.members)

    def test_partial_byte_set_is_not_promoted_to_full(self):
        carrier = Type("u8")
        domain = finite(Constant(carrier, value) for value in range(255))
        self.assertIsNotNone(domain.members)
        self.assertFalse(contains(domain, Constant(carrier, 255)))
        self.assertFalse(equivalent(domain, scalar_domain(carrier)))


class TypeDomainOperationTests(LegacyTestCase):
    def test_api_boundaries_reject_raw_values_not_implicitly_construct_domains(self):
        def reject(value):
            for operation in (lambda: union(value),
                              lambda: equivalent(value, NEVER_DOMAIN),
                              lambda: equivalent(NEVER_DOMAIN, value),
                              lambda: is_subset(value, NEVER_DOMAIN),
                              lambda: is_subset(NEVER_DOMAIN, value),
                              lambda: contains(value, Constant(I32, 1)),
                              lambda: contains(NEVER_DOMAIN, value)):
                with self.assertRaises(DomainError):
                    operation()
        for value in (None, I32, [], set(), 1, "Unit"):
            with self.subTest(value=value):
                reject(value)

    def test_empty_union_and_empty_arguments_are_identities(self):
        domain = finite([Constant(I32, 1)])
        self.assertEqual(union(), NEVER_DOMAIN)
        self.assertEqual(union(NEVER_DOMAIN, NEVER_DOMAIN), NEVER_DOMAIN)
        self.assertEqual(union(NEVER_DOMAIN, domain, NEVER_DOMAIN), domain)

    def test_union_deduplicates_across_inputs_but_not_within_finite_declarations(self):
        first = finite([Constant(I32, 1), Constant(I32, 2)])
        second = finite([Constant(I32, 2), Constant(I32, 3)])
        expected = finite([Constant(I32, number) for number in (1, 2, 3)])
        self.assertEqual(union(first, second, first), expected)

    def test_union_rejects_cross_carrier_inputs_even_after_a_full_domain(self):
        left = scalar_domain(I32)
        right = finite([Constant(Type("u8"), 1)])
        for ordering in permutations((left, right, NEVER_DOMAIN)):
            with self.subTest(ordering=ordering):
                with self.assertRaises(DomainError):
                    union(*ordering)

    def test_full_domain_absorbs_only_its_own_subsets(self):
        full = scalar_domain(I32)
        partial = finite([Constant(I32, 1), Constant(I32, 2)])
        self.assertEqual(union(partial, full), full)
        self.assertTrue(is_subset(partial, full))
        self.assertFalse(is_subset(full, partial))
        self.assertFalse(equivalent(full, partial))

    def test_union_of_bool_parts_and_unit_part_normalizes(self):
        false = finite([Constant(BOOL, False)])
        true = finite([Constant(BOOL, True)])
        self.assertEqual(union(false, true), scalar_domain(BOOL))
        self.assertEqual(union(NEVER_DOMAIN, finite([Constant(UNIT, UNIT_VALUE)])),
                         scalar_domain(UNIT))

    def test_cross_carrier_contains_and_nonempty_subset_are_false(self):
        boolean = scalar_domain(BOOL)
        integer = scalar_domain(I32)
        byte = scalar_domain(Type("u8"))
        for left, right in permutations((boolean, integer, byte), 2):
            with self.subTest(left=left, right=right):
                self.assertFalse(is_subset(left, right))
                self.assertFalse(equivalent(left, right))
        self.assertFalse(contains(boolean, Constant(I32, 1)))
        self.assertFalse(contains(integer, Constant(BOOL, True)))
        self.assertFalse(contains(integer, Constant(Type("u8"), 1)))

    def test_equal_cardinality_does_not_make_different_domains_equivalent(self):
        one_integer = finite([Constant(I32, 1)])
        another_integer = finite([Constant(I32, 2)])
        one_boolean = finite([Constant(BOOL, True)])
        unit = scalar_domain(UNIT)
        for left, right in permutations((one_integer, another_integer, one_boolean, unit), 2):
            with self.subTest(left=left, right=right):
                self.assertFalse(equivalent(left, right))
                self.assertFalse(is_subset(left, right))

    def test_empty_is_subset_of_every_supported_domain_but_not_conversely(self):
        for domain in (NEVER_DOMAIN, scalar_domain(BOOL), scalar_domain(UNIT),
                       scalar_domain(STR), finite([Constant(I32, 7)])):
            with self.subTest(domain=domain):
                self.assertTrue(is_subset(NEVER_DOMAIN, domain))
                self.assertEqual(is_subset(domain, NEVER_DOMAIN), domain == NEVER_DOMAIN)

    def test_finite_membership_has_no_hidden_numeric_or_unicode_conversion(self):
        domain = finite([Constant(STR, "é"), Constant(STR, "a")])
        self.assertTrue(contains(domain, Constant(STR, "é")))
        self.assertFalse(contains(domain, Constant(STR, "e\u0301")))
        self.assertFalse(contains(domain, Constant(Type("char"), "é")))
        self.assertFalse(contains(finite([Constant(I32, 1)]), Constant(Type("u8"), 1)))


class TypeDomainAlgebraReferenceTests(LegacyTestCase):
    def assert_finite_reference_algebra(self, carrier, sample, universe):
        """Exhaustively compare small input families with independent raw sets.

        ``universe`` is the *whole* carrier when finite (bool/Unit/u8),
        not merely the sample used to generate subsets.  Consequently this also
        checks that a finite integer subset is not confused with the full type.
        """
        cases = []
        for size in range(len(sample) + 1):
            for values in combinations(sample, size):
                reference = frozenset(values)
                domain = finite(Constant(carrier, value) for value in values)
                cases.append((domain, reference))
        full = scalar_domain(carrier)
        if frozenset(universe) not in [reference for _, reference in cases]:
            cases.append((full, frozenset(universe)))

        def assert_reference(domain, reference):
            for value in universe:
                self.assertEqual(contains(domain, Constant(carrier, value)),
                                 value in reference, (carrier, domain, value, reference))

        for domain, reference in cases:
            assert_reference(domain, reference)
            self.assertTrue(is_subset(domain, domain))
            self.assertTrue(equivalent(domain, domain))
            self.assertEqual(union(domain, domain), domain)

        for (left, left_set), (right, right_set) in product(cases, repeat=2):
            result = union(left, right)
            assert_reference(result, left_set | right_set)
            self.assertEqual(result, union(right, left))
            self.assertEqual(is_subset(left, right), left_set <= right_set)
            self.assertEqual(equivalent(left, right), left_set == right_set)
            self.assertEqual(is_subset(left, right) and is_subset(right, left),
                             equivalent(left, right))

        for (a, a_set), (b, b_set), (c, c_set) in product(cases, repeat=3):
            self.assertEqual(union(union(a, b), c), union(a, union(b, c)))
            self.assertEqual(union(a, b, c), union(union(a, b), c))
            if a_set <= b_set <= c_set:
                self.assertTrue(is_subset(a, c))

    def test_all_boolean_subsets_against_raw_boolean_sets(self):
        self.assert_finite_reference_algebra(BOOL, (False, True), (False, True))

    def test_all_unit_subsets_against_empty_or_singleton_sets(self):
        self.assert_finite_reference_algebra(UNIT, (UNIT_VALUE,), (UNIT_VALUE,))

    def test_small_byte_subsets_and_full_byte_carrier_against_256_values(self):
        self.assert_finite_reference_algebra(Type("u8"), (0, 1, 255), tuple(range(256)))

    def test_string_subsets_against_raw_string_sets(self):
        sample = ("", "a", "世界")
        cases = [(finite(Constant(STR, value) for value in values), frozenset(values))
                 for size in range(4) for values in combinations(sample, size)]
        for (a, a_set), (b, b_set) in product(cases, repeat=2):
            self.assertEqual(is_subset(a, b), a_set <= b_set)
            self.assertEqual(equivalent(a, b), a_set == b_set)
            joined = union(a, b)
            for value in (*sample, "outside"):
                self.assertEqual(contains(joined, Constant(STR, value)), value in a_set | b_set)
        # The infinite carrier is not equivalent to any finite member set.
        for domain, _ in cases:
            self.assertTrue(is_subset(domain, scalar_domain(STR)))
            self.assertFalse(is_subset(scalar_domain(STR), domain))

    def test_char_finite_union_uses_scalars_without_string_carrier_coercion(self):
        carrier = Type("char")
        left = finite([Constant(carrier, "a"), Constant(carrier, "\U0001f600")])
        right = finite([Constant(carrier, "\0"), Constant(carrier, "a")])
        result = union(left, right)
        for value in ("a", "\U0001f600", "\0", "z"):
            self.assertEqual(contains(result, Constant(carrier, value)),
                             value in {"a", "\U0001f600", "\0"})
            self.assertFalse(contains(result, Constant(STR, value)))
        self.assertTrue(is_subset(result, scalar_domain(carrier)))
        self.assertFalse(is_subset(scalar_domain(carrier), result))


if __name__ == "__main__":
    unittest.main()
