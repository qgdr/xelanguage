"""Public View[P] spelling and mandatory local type annotations.

The selected spelling names the already implemented nonowning, original-
coordinate descriptor. P is its complete pointer result, including permission.
It does not authorize View-level type attachments,
view parameters/results, generic user types, or implicit permission changes.
"""
import copy
import json
import unittest

from compiler.xe_ast.access import view_type
from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import (
    AnnotatedChecker,
    CBackend,
    lower_program_to_c,
    lower_to_c,
)
from compiler.xe_ast.language_policy import current_profile, language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.typesys import I32, ptr

VIEW_PROGRAM = '''fn main()->Unit {
    let[mut] values:Array[i32,4]=[10,20,30,40];
    let part:View[i32@]=values⟨1..4⟩[@];
    let copied:View[i32@]=part;
    let nested:View[i32@]=copied⟨2..=3⟩;
    let tail:View[i32@]=nested⟨4..4⟩;
    let tail_copy:View[i32@]=tail;
    let same_anchor:View[i32@]=tail_copy⟨4..4⟩;
    values⟦2⟧=99;
    println("{} {} {}",part⟦2⟧#,nested⟦2⟧#,copied⟦1⟧#);
}'''


class CoreViewSpellingTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "view-spelling.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        self.assertEqual(tree, original, "semantic View types belong in side tables")
        return source, tree, checker

    def rejected(self, text, code):
        diagnostics = check_source(text, "view-spelling.xe")
        self.assertTrue(diagnostics, text)
        self.assertEqual(diagnostics[0].code, code, diagnostics[0].render())

    def test_source_ast_retains_named_type_and_schema_one(self):
        source, tree, checker = self.checked(VIEW_PROGRAM)
        original = copy.deepcopy(tree)
        bindings = {node["name"]: node for node in tree["items"][0]["body"]["statements"]
                    if node["kind"] == "Binding"}
        node = bindings["part"]["type"]
        self.assertEqual(node["kind"], "NamedType")
        self.assertEqual(node["path"]["parts"], ["View"])
        self.assertEqual(node["arguments"][0]["target"]["path"]["parts"], ["i32"])
        self.assertEqual(checker.binding_types[id(bindings["part"])], view_type(ptr(I32)))
        self.assertEqual(checker.binding_types[id(bindings["copied"])], view_type(ptr(I32)))
        saved = document(source, tree)
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(json.loads(json.dumps(saved))["ast"], tree)
        self.assertEqual(tree, original)

    def test_alias_elements_and_pointer_elements_preserve_type_levels(self):
        self.checked('''type Number=i32;
        fn main()->Unit {
            let value:Number=42;
            let numbers:Array[Number,1]=[value];
            let part:View[Number@]=numbers⟨..⟩[@];
            let pointer:Number@=part⟦0⟧;
            let pointers:Array[Number@,1]=[pointer];
            let addresses:View[Number@@]=pointers⟨..⟩[@];
            let slot:Number@@=addresses⟦0⟧;
            println("{}",slot##);
        }''')

    def test_mutable_binding_can_rebind_readonly_descriptor_without_stale_bounds(self):
        self.checked('''fn main()->Unit {
            let values:Array[i32,4]=[10,20,30,40];
            let[mut] part:View[i32@]=values⟨0..2⟩[@];
            part=values⟨2..4⟩[@];
            println("{}",part⟦3⟧#);
        }''')

    def test_mutable_view_does_not_implicitly_become_readonly(self):
        self.rejected('''fn main()->Unit {
            let[mut] values:Array[i32,2]=[1,2];
            let part:View[i32@]=values⟨..⟩[@[mut]];
        }''', "XE-TYPE-0001")
        self.rejected('''fn main()->Unit {
            let[mut] values:Array[i32,2]=[1,2];
            let[mut] part:View[i32@]=values⟨..⟩[@];
            part=values⟨..⟩[@[mut]];
        }''', "XE-TYPE-0001")

    def test_readonly_view_does_not_grant_element_write_permission(self):
        for operation in ("part⟦0⟧#=3;", "part⟦0⟧#=3;", "part⟨..⟩⟦0⟧#=3;"):
            with self.subTest(operation=operation):
                self.rejected('fn main()->Unit{let[mut] values:Array[i32,2]=[1,2];'
                              'let part:View[i32@]=values⟨..⟩[@];' + operation + '}', "XE-MUT-0001")

    def test_view_annotation_cannot_replace_an_owning_array_or_thin_pointer(self):
        for declaration in ("let part:View[i32@]=values;", "let part:i32@=values⟨..⟩[@];",
                            "let part:Array[i32,2]=values⟨..⟩[@];", "let part:View[bool@]=values⟨..⟩[@];"):
            with self.subTest(declaration=declaration):
                self.rejected('fn main()->Unit{let values:Array[i32,2]=[1,2];'
                              + declaration + '}', "XE-TYPE-0001")

    def test_missing_local_annotations_are_rejected_even_with_typed_initializers(self):
        for declaration in ("let part=values⟨..⟩[@];", "let[mut] part=values⟨..⟩[@];",
                            "let number=typed;", "let[mut] number=typed;",
                            "let copied=part;", "let text << String::from(\"x\");"):
            with self.subTest(declaration=declaration):
                self.rejected('fn main()->Unit{let values:Array[i32,2]=[1,2];'
                              'let typed:i32=42;let part:View[i32@]=values⟨..⟩[@];'
                              + declaration + '}', "XE-CORE-0001")

    def test_old_saved_writable_view_examples_fail_instead_of_guessing_a_type(self):
        # These were valid before mandatory annotations. Keep the exact old
        # descriptor-save/copy shape as migration evidence; an inline writable
        # restriction remains tested separately, not a writable type approval.
        self.rejected('''fn main()->Unit {
            let[mut] values:Array[i32,3]=[10,20,30];
            let writable=values⟨1..3⟩[@[mut]];
            let copied=writable;
            copied⟦2⟧#=40;
        }''', "XE-CORE-0001")

    def test_unconnected_view_positions_fail_closed(self):
        for text in ("fn inspect(part:View[i32@])->Unit{unit}",
                     "fn create()->View[i32@]{panic(\"stop\")}",
                     "type Part=View[i32@];", "fn inspect(pointer:View[i32@]@)->Unit{unit}",
                     "fn inspect(parts:Array[View[i32@],2])->Unit{unit}"):
            with self.subTest(text=text):
                self.rejected(text, "XE-CORE-0002")

    def test_view_of_noncopy_resource_elements_is_not_opened(self):
        self.rejected('fn main()->Unit{let part:View[String@]=panic("stop");}', "XE-CORE-0002")

    def test_descriptor_addresses_and_pipeline_combinations_remain_closed(self):
        prefix = ('fn main()->Unit{let values:Array[i32,2]=[1,2];'
                  'let part:View[i32@]=values⟨..⟩[@];')
        for operation in ("part@;", "part |> _ -> unit;",
                          "let copy:View[i32@]=unit |> _ -> part;"):
            with self.subTest(operation=operation):
                self.rejected(prefix + operation + '}', "XE-CORE-0002")

    def test_unselected_view_spellings_and_type_attachments_stay_rejected(self):
        for annotation in ("i32@..", "Slice[i32]", "view[i32]",
                           "View", "View[i32,bool@]"):
            with self.subTest(annotation=annotation):
                # Candidate attachments may fail in the parser rather than
                # admission. Rejection matters here, not a new diagnostic
                # contract for syntax that has not been selected.
                self.assertTrue(check_source('fn main()->Unit{'
                                             'let values:Array[i32,2]=[1,2];'
                                             f'let part:{annotation}=values⟨..⟩[@];}}'))

    def test_missing_annotation_in_handwritten_ast_cannot_bypass_any_consumer(self):
        source, original, _ = self.checked(VIEW_PROGRAM)
        tree = copy.deepcopy(original)
        tree["items"][0]["body"]["statements"][1]["type"] = None
        before = copy.deepcopy(tree)
        self.assertEqual([error.code for error in Checker(source, tree).check()], ["XE-CORE-0001"])
        for consume in (lambda: document(source, tree), lambda: lower_program_to_c(source, tree),
                        lambda: CBackend(AnnotatedChecker(source, tree)).generate()):
            with self.subTest(consumer=consume), self.assertRaises(Diagnostic) as caught:
                consume()
            self.assertEqual(caught.exception.code, "XE-CORE-0001")
        self.assertEqual(tree, before)

    def test_explicit_legacy_keeps_local_type_inference(self):
        text = "fn main(){let number=42;println(\"{}\",number);}"
        with language_profile("legacy"):
            self.assertEqual(check_source(text), [])
            self.assertIn("int main(", lower_to_c(text))
        self.assertEqual(current_profile(), "core")

    def test_legacy_user_type_named_view_does_not_acquire_core_descriptor_layout(self):
        cases = (
            'type View=i32;fn main(){let number:View=42;println("{}",number);}',
            'struct View{value:i32,}impl Copy for View{}'
            'fn main(){let item:View=View{.value=42;};println("{}",item.value);}',
            'struct[T] View{value:T,}impl[T] Copy for View[T] where T implements Copy{}'
            'fn main(){let item:View[i32]=View[i32]{.value=42;};println("{}",item.value);}',
        )
        for text in cases:
            with self.subTest(source=text), language_profile("legacy"):
                self.assertEqual(check_source(text), [])
                self.assertNotIn("coordinate_view_layout", lower_to_c(text))
        self.assertEqual(current_profile(), "core")
