"""Archived toy-language boundaries, explicitly tested under legacy.

These are temporary integration contracts for the independent algebra stage,
not evidence of set/union syntax, narrowed assignment, or runtime support.
The approved core declaration slice now has its own scalar_declarations tests.
These fixtures preserve old behavior; their rejection of the new source syntax
does not describe the default core profile or narrowed runtime value support.
"""
import copy
import unittest

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.ast import SCHEMA_VERSION, document
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source
from compiler.xe_ast.type_domains import (
    NEVER_DOMAIN,
    Constant,
    Domain,
    DomainError,
    DuplicateMemberError,
    finite,
    is_subset,
    scalar_domain,
)
from compiler.xe_ast.typesys import BOOL, I32, STR, UNIT, Type, maybe


class TypeDomainBoundaryTests(LegacyTestCase):
    def checked(self, text):
        source = Source(text, "<type-domain-boundary>")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        checker = Checker(source, tree)
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        self.assertEqual(tree, original)
        self.assertEqual(document(source, tree)["schema_version"], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 1)
        return checker

    def test_existing_transparent_aliases_resolve_before_model_adaptation(self):
        checker = self.checked("""type Count=Number; type Number=i32;
            type Flag=bool; type Text=str; type NothingToReport=Unit;
            fn count(value:Count)->Number {value} fn main() {}""")
        for name, carrier in (("Count", I32), ("Number", I32),
                              ("Flag", BOOL), ("Text", STR), ("NothingToReport", UNIT)):
            with self.subTest(alias=name):
                resolved = checker.alias_types[name]
                self.assertEqual(resolved, carrier)
                self.assertEqual(scalar_domain(resolved), scalar_domain(carrier))
        self.assertEqual(checker.functions["count"].parameters, [I32])

    def test_decoded_literal_identity_rejects_duplicates_without_rewriting_ast(self):
        # This is an explicit test adapter, not a new production literal rule:
        # the caller supplies the carrier; no integer defaults are inferred by
        # the domain module. Unit is intentionally absent: the parser's host
        # None encoding must not be silently reused as a UnitValue constant.
        for text, carrier, first_raw, second_raw in (
            ("fn main(){let a=1;let b=0x1;}", I32, "1", "0x1"),
            (r'fn main(){let a="a";let b="\x61";}', STR, '"a"', r'"\x61"'),
            (r"fn main(){let a='a';let b='\x61';}", Type("char"), "'a'", r"'\x61'"),
        ):
            with self.subTest(carrier=carrier):
                checker = self.checked(text)
                tree = checker.tree
                original = copy.deepcopy(tree)
                literals = [binding["value"]
                            for binding in tree["items"][0]["body"]["statements"]]
                self.assertEqual([literal["raw"] for literal in literals],
                                 [first_raw, second_raw])
                self.assertEqual(literals[0]["value"], literals[1]["value"])
                with self.assertRaises(DuplicateMemberError) as caught:
                    finite(Constant(carrier, literal["value"]) for literal in literals)
                self.assertEqual(caught.exception.first_index, 0)
                self.assertEqual(caught.exception.duplicate_index, 1)
                self.assertEqual(tree, original)

    def test_numeric_domain_inclusion_does_not_add_implicit_machine_conversion(self):
        byte = finite([Constant(Type("u8"), 1)])
        self.assertFalse(is_subset(byte, scalar_domain(I32)))
        text = "fn main(){let byte:u8=1;let wide:i32=byte;}"
        errors = check_source(text)
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-TYPE-0001", errors[0].render())
        self.checked("fn main(){let byte:u8=1;let wide:i32=byte as i32;}")

    def test_none_remains_valid_but_result_domains_cannot_be_treated_as_empty(self):
        checker = self.checked("""fn absent()->i32? {Maybe::None}
            fn main(){let absent:i32?=Maybe::None;}""")
        result = checker.functions["absent"].result
        self.assertEqual(result, maybe(I32))
        with self.assertRaises(DomainError):
            scalar_domain(result)
        # Validation must happen before empty normalization. An unsupported
        # result type must not disappear into the valid NEVER_DOMAIN value.
        with self.assertRaises(DomainError):
            Domain(result, frozenset())
        self.assertEqual(NEVER_DOMAIN, finite([]))

    def test_future_syntax_words_still_work_as_ordinary_identifiers(self):
        self.checked("""fn set(value:i32)->i32 {value}
            fn union(value:i32)->i32 {value}
            fn assert(value:i32)->i32 {value}
            fn main(){
                let set=1;
                let union=set+1;
                let assert=union+1;
                println("{}",assert);
            }""")

    def test_new_type_syntax_has_not_silently_been_connected_to_old_parser(self):
        for text in ("type Small=set {1,2}; fn main(){}",
                     "type Combined=i32 union i32; fn main(){}",
                     "type Combined=i32 | i32; fn main(){}"):
            with self.subTest(text=text):
                errors = check_source(text)
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-PARSE-0001", errors[0].render())


if __name__ == "__main__":
    unittest.main()
