"""旧类型关系的隔离验收，不是 set/union 或新子类型设计的实现证据。

手写预期矩阵检查关键语义；另用迁移前算法快照交叉检查有限结构样本，
避免抽离时无意放宽/收紧旧规则。快照只是一致性对照，不是正确性证明。
"""

import unittest
from itertools import product

from compiler.xe_ast.type_relations import legacy_compatible, same_type
from compiler.xe_ast.typesys import (
    BOOL,
    I32,
    INT_LITERAL,
    NEVER,
    NONE,
    NUMERIC,
    STR,
    STRING,
    UNIT,
    UNKNOWN,
    Type,
    callable_type,
    maybe,
    ptr,
)


def migration_snapshot_compatible(actual, expected, pointer_weakening=True, never_coercion=True):
    """迁移前 Checker.compatible 的测试快照；不要跟随新实现同步改写。

    保留旧分支和递归的原样含义。将来更改语言规则时，应另行说明旧模型
    的废弃范围，而不是为了让本对照通过同时改动两个实现。
    """
    if (actual == expected or actual == UNKNOWN or expected == UNKNOWN
            or never_coercion and actual == NEVER):
        return True
    if actual == INT_LITERAL and expected.name in NUMERIC:
        return True
    if actual.name == expected.name == "ptr":
        return actual.args == expected.args and (actual.mutable == expected.mutable or
                pointer_weakening and actual.mutable and not expected.mutable)
    if actual.name == expected.name and len(actual.args) == len(expected.args):
        return actual.identity == expected.identity and actual.mutable == expected.mutable and all(
            migration_snapshot_compatible(a, b, pointer_weakening=False, never_coercion=False)
            for a, b in zip(actual.args, expected.args))
    return False


class SameTypeTests(unittest.TestCase):
    def test_name_arguments_permission_and_identity_are_significant(self):
        for left, right in (
            (I32, Type("u8")),
            (Type("Meters"), Type("Seconds")),
            (Type("Holder", (I32,)), Type("Holder", (STR,))),
            (ptr(I32), ptr(I32, mutable=True)),
            (callable_type([I32], I32, capture=True, identity=1),
             callable_type([I32], I32, capture=True, identity=2)),
        ):
            with self.subTest(left=repr(left), right=repr(right)):
                self.assertFalse(same_type(left, right))

    def test_risk_metadata_does_not_create_new_type_or_hash_identity(self):
        for plain, annotated in (
            (ptr(I32), ptr(I32, unsafe=True)),
            (Type("Record"), Type("Record", unsafe=True)),
            (Type("tuple", (ptr(I32),)), Type("tuple", (ptr(I32, unsafe=True),))),
        ):
            with self.subTest(type=repr(plain)):
                self.assertTrue(same_type(plain, annotated))
                self.assertEqual(hash(plain), hash(annotated))
                self.assertEqual({plain, annotated}, {plain})

    def test_no_name_resolution_or_value_domain_equivalence_is_invented(self):
        self.assertFalse(same_type(Type("AliasName"), I32))
        self.assertFalse(same_type(Type("EmptyNominal"), NEVER))
        self.assertTrue(same_type(Type("Record", (I32,)), Type("Record", (I32,))))


class LegacyCompatibilityTests(unittest.TestCase):
    def test_independent_expected_matrix(self):
        readonly, writable = ptr(I32), ptr(I32, mutable=True)
        cases = (
            ("same primitive", I32, I32, True),
            ("no implicit machine widening", Type("u8"), I32, False),
            ("unit is not top", I32, UNIT, False),
            ("none marker is not unit", NONE, UNIT, False),
            ("actual inference hole", UNKNOWN, I32, True),
            ("expected inference hole", I32, UNKNOWN, True),
            ("literal numeric candidate", INT_LITERAL, Type("u8"), True),
            ("literal float candidate", INT_LITERAL, Type("f32"), True),
            ("literal is not bool", INT_LITERAL, BOOL, False),
            ("concrete integer is not unresolved literal", I32, INT_LITERAL, False),
            ("top-level noncompletion", NEVER, I32, True),
            ("normal value is not never", I32, NEVER, False),
            ("drop outer pointer permission", writable, readonly, True),
            ("no pointer permission increase", readonly, writable, False),
            ("no pointee integer conversion", ptr(Type("u8")), readonly, False),
            ("no nested pointer permission change", ptr(writable), ptr(readonly), False),
            ("outer permission can still drop", ptr(writable, mutable=True), ptr(writable), True),
            ("no never pointee adaptation", ptr(NEVER), readonly, False),
            ("no nested never array adaptation", Type("Array", (NEVER, Type("0"))),
             Type("Array", (I32, Type("0"))), False),
            ("no nested pointer result adaptation", maybe(writable), maybe(readonly), False),
            ("no function parameter variance", callable_type([readonly], I32),
             callable_type([writable], I32), False),
            ("no function result covariance", callable_type([], NEVER),
             callable_type([], I32), False),
            ("closure identity differs", callable_type([], I32, capture=True, identity=1),
             callable_type([], I32, capture=True, identity=2), False),
            ("closure identity matches", callable_type([], I32, capture=True, identity=1),
             callable_type([], I32, capture=True, identity=1), True),
            ("risk is not identity", ptr(I32, unsafe=True), readonly, True),
            ("nominal names stay separate", Type("Meters"), Type("Seconds"), False),
            # These are legacy inference permissions, not subtype derivations.
            ("nested inference hole", Type("tuple", (UNKNOWN,)), Type("tuple", (I32,)), True),
            ("nested literal candidate", Type("tuple", (INT_LITERAL,)), Type("tuple", (I32,)), True),
        )
        for name, actual, expected, accepted in cases:
            with self.subTest(case=name):
                self.assertIs(legacy_compatible(actual, expected), accepted)

    def test_flags_only_remove_their_specific_outer_permission(self):
        self.assertFalse(legacy_compatible(NEVER, I32, never_coercion=False))
        self.assertTrue(legacy_compatible(NEVER, NEVER, never_coercion=False))
        self.assertTrue(legacy_compatible(NEVER, UNKNOWN, never_coercion=False))
        self.assertFalse(legacy_compatible(ptr(I32, mutable=True), ptr(I32), pointer_weakening=False))
        self.assertTrue(legacy_compatible(ptr(I32, mutable=True), ptr(I32, mutable=True),
                                          pointer_weakening=False))
        self.assertTrue(legacy_compatible(INT_LITERAL, I32, False, False))
        self.assertFalse(legacy_compatible(ptr(NEVER), ptr(I32), True, True))

    def test_relation_does_not_replace_separate_wellformedness_validation(self):
        # The old relation does not validate constructor arity or numeric shape.
        # Preserve those answers without endorsing malformed types as source.
        for actual, expected, accepted in (
            (Type("ptr"), Type("ptr"), True),
            (Type("fn"), Type("fn"), True),
            (Type("ptr"), ptr(I32), False),
            (INT_LITERAL, Type("i32", (STR,)), True),
            (Type("Array"), Type("Array", (I32,)), False),
            (Type("ptr", (I32,), identity=1), Type("ptr", (I32,), identity=2), True),
        ):
            with self.subTest(actual=repr(actual), expected=repr(expected)):
                self.assertIs(legacy_compatible(actual, expected), accepted)

    def test_finite_structural_cross_product_matches_migration_snapshot(self):
        atoms = (I32, Type("u8"), Type("f32"), BOOL, STR, STRING, UNIT,
                 NEVER, NONE, UNKNOWN, INT_LITERAL, Type("UserA"), Type("UserB"))
        nested = (
            *(ptr(atom, mutable=mutable) for atom in atoms for mutable in (False, True)),
            *(Type("tuple", (atom,)) for atom in (I32, NEVER, UNKNOWN, INT_LITERAL)),
            *(maybe(atom) for atom in (I32, NEVER, UNKNOWN, INT_LITERAL)),
            Type("Array", (NEVER, Type("0"))), Type("Array", (I32, Type("0"))),
            ptr(ptr(I32)), ptr(ptr(I32, mutable=True)),
            ptr(ptr(I32, mutable=True), mutable=True), ptr(I32, unsafe=True),
            callable_type([I32], NEVER), callable_type([I32], I32),
            callable_type([], I32, capture=True, identity=1),
            callable_type([], I32, capture=True, identity=2),
            Type("ptr"), Type("fn"), Type("i32", (STR,)),
        )
        sample = atoms + nested
        for actual, expected, weakening, noncompletion in product(sample, sample, (False, True), (False, True)):
            with self.subTest(actual=repr(actual), expected=repr(expected),
                              pointer_weakening=weakening, never_coercion=noncompletion):
                self.assertIs(
                    legacy_compatible(actual, expected, weakening, noncompletion),
                    migration_snapshot_compatible(actual, expected, weakening, noncompletion),
                )


if __name__ == "__main__":
    unittest.main()
