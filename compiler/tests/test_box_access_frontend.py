"""DEC-035 box/value indexing and narrowly admitted temporary Array sources.

All checks use the public core profile. Dangerous examples are only rejected
statically; executable and sanitizer evidence is provided by backend tests.
"""
import copy
import json
import unittest

from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import AnnotatedChecker
from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.typesys import I32, ptr

MAKE = "fn make()->Array[i32,2]{[10,20]}"
READ = "fn read(pointer:i32@)->i32{pointer#}"
PROGRAM = '''fn main()->Unit {
    let[mut] a:Array[i32,4]=[10,20,30,40];
    let copied:i32=a⟦1⟧;
    let direct:i32@=a⟦1⟧@;
    let[mut] r:View[i32@]=a⟨1..3⟩[@];
    let w:View[i32@[mut]]=a⟨1..3⟩[@[mut]];
    let child:View[i32@[mut]]=w⟨1..2⟩;
    a⟦1⟧=77;
    child⟦1⟧#=99;
    let same:i32=r⟦1⟧#;
}'''


class BoxAccessFrontendTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "box-access.xe")
        tree = parse_source(text, source.filename)
        before = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(d.render() for d in diagnostics))
        self.assertEqual(checker.warnings, [])
        self.assertEqual(tree, before)
        self.assertEqual(json.loads(json.dumps(document(source, tree)))["ast"], tree)
        return checker, tree

    def rejected(self, text, code=None):
        errors = check_source(text, "box-access.xe")
        self.assertTrue(errors, text)
        if code is not None:
            self.assertEqual(errors[0].code, code, errors[0].render())
        return errors[0]

    def test_confirmed_program_uses_distinct_index_restriction_and_constructor_nodes(self):
        _, tree = self.checked(PROGRAM)
        bindings = {statement["name"]: statement for statement in tree["items"][0]["body"]["statements"]
                    if statement["kind"] == "Binding"}
        self.assertEqual(bindings["copied"]["value"]["kind"], "Index")
        self.assertEqual(set(bindings["copied"]["value"]), {"kind", "span", "target", "index"})
        self.assertEqual(bindings["r"]["value"]["kind"], "ViewConstruct")
        self.assertEqual(bindings["r"]["value"]["target"]["kind"], "Restriction")
        self.assertEqual(bindings["child"]["value"]["kind"], "Restriction")

    def test_array_and_view_index_have_their_confirmed_distinct_type_levels(self):
        checker, tree = self.checked('''fn main()->Unit {
            let number:i32=42;
            let addresses:Array[i32@,1]=[number@];
            let value:i32@=addresses⟦0⟧;
            let slot:i32@@=addresses⟦0⟧@;
            let mapping:View[i32@@]=addresses⟨..⟩[@];
            let located:i32@@=mapping⟦0⟧;
            let copied:i32@=mapping⟦0⟧#;
        }''')
        bindings = {statement["name"]: statement for statement in tree["items"][0]["body"]["statements"]
                    if statement["kind"] == "Binding"}
        self.assertEqual(checker.binding_types[id(bindings["value"])], ptr(I32))
        self.assertEqual(checker.binding_types[id(bindings["slot"])], ptr(ptr(I32)))
        self.assertEqual(checker.binding_types[id(bindings["located"])], ptr(ptr(I32)))

    def test_four_confirmed_error_categories_are_rejected(self):
        for expression in ("a⟨1⟩", "a⟦1..3⟧", "a⟦1⟧[@]", "w⟦1⟧@", "(w⟦1⟧)@"):
            text = 'fn main()->Unit{let a:Array[i32,4]=[1,2,3,4];let w:View[i32@]=a⟨..⟩[@];' + expression + ';}'
            with self.subTest(expression=expression):
                self.rejected(text)

    def test_old_call_element_view_and_range_mut_forms_do_not_get_implicit_compatibility(self):
        for expression in ("a(0)", "a(0)[mut]", "a⟨..⟩[mut]", "a⟨..⟩"):
            with self.subTest(expression=expression):
                self.rejected("fn main()->Unit{let a:Array[i32,2]=[1,2];" + expression + ";}")
        self.rejected("fn main()->Unit{let a:Array[i32,2]=[1,2];let w:View[i32]=a⟨..⟩[@];}")

    def test_restriction_preserves_the_complete_pointer_permission_and_original_domain(self):
        self.checked('''fn main()->Unit {
            let[mut] a:Array[i32,4]=[1,2,3,4];
            let w:View[i32@[mut]]=a⟨1..=3⟩[@[mut]];
            let child:View[i32@[mut]]=w⟨2..3⟩;
            child⟦2⟧#=20;
        }''')
        self.rejected('''fn main()->Unit {
            let a:Array[i32,4]=[1,2,3,4];
            let w:View[i32@]=a⟨1..3⟩[@];
            let value:i32=w⟦0⟧#;
        }''', "XE-INDEX-0001")

    def test_array_box_write_and_address_permissions_are_checked_independently(self):
        for expression in ("a⟦0⟧=2", "let p:i32@[mut]=a⟦0⟧@[mut]",
                           "let w:View[i32@[mut]]=a⟨..⟩[@[mut]]"):
            with self.subTest(expression=expression):
                self.rejected("fn main()->Unit{let a:Array[i32,1]=[1];" + expression + ";}", "XE-MUT-0001")

    def test_temporary_copy_read_and_named_readonly_call_are_admitted(self):
        self.checked(MAKE + READ + '''fn main()->Unit {
            let first:i32=make()⟦0⟧;
            let second:i32=read(make()⟦1⟧@);
            read(make()⟦0⟧@);
        }''')

    def test_temporary_element_address_cannot_be_saved_after_its_full_expression(self):
        self.rejected(MAKE + "fn main()->Unit{let p:i32@=make()⟦0⟧@;}", "XE-PTR-0004")

    def test_copying_a_temporary_stored_pointer_keeps_the_external_target(self):
        self.checked('''fn make(pointer:i32@)->Array[i32@,1]{[pointer]}
        fn main()->Unit {let target:i32=42;let pointer:i32@=make(target@)⟦0⟧;}''')

    def test_unconfirmed_temporary_contexts_and_write_permissions_remain_closed(self):
        cases = (
            "fn main()->Unit{let p:i32@[mut]=make()⟦0⟧@[mut];}",
            "fn main()->Unit{let w:View[i32@]=make()⟨..⟩[@];}",
            "fn main()->Unit{let n:i32={make()⟦0⟧};}",
            "fn main()->Unit{let n:i32=if true{make()⟦0⟧}else{1};}",
            "fn result()->i32{make()⟦0⟧}",
        )
        for suffix in cases:
            with self.subTest(suffix=suffix):
                self.rejected(MAKE + suffix, "XE-CORE-0002")

    def test_manual_ast_cannot_hide_extra_fields_range_indices_or_removed_kinds(self):
        source = Source("fn main()->Unit{let a:Array[i32,1]=[1];let n:i32=a⟦0⟧;}")
        base = parse_source(source.text)
        for mutation in ("field", "range", "old"):
            tree = copy.deepcopy(base)
            index = tree["items"][0]["body"]["statements"][-1]["value"]
            if mutation == "field":
                index["trusted_box"] = True
            elif mutation == "range":
                index["index"] = {"kind": "Range", "span": index["span"], "operator": "..", "lower": None, "upper": None}
            else:
                index.update(kind="Access", modifiers=[])
            original = copy.deepcopy(tree)
            with self.subTest(mutation=mutation):
                self.assertTrue(Checker(source, tree).check())
                with self.assertRaises(Diagnostic):
                    document(source, tree)
                self.assertEqual(tree, original)

    def test_legacy_does_not_acquire_new_tokens_or_handwritten_ast_nodes(self):
        source = Source("fn main()->Unit{let a:Array[i32,1]=[1];let n:i32=a⟦0⟧;}")
        tree = parse_source(source.text)
        with language_profile("legacy"):
            self.rejected(source.text, "XE-LEX-0001")
            self.assertEqual([d.code for d in Checker(source, tree).check()], ["XE-CORE-0002"])


if __name__ == "__main__":
    unittest.main()
