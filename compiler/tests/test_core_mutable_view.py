"""View[P] locals preserve complete result-pointer permission layers.

This is the public source/AST-to-executable contract for the selected spelling,
not permission to treat a view as an owner, unique loan, or thin pointer.  The
tests deliberately keep view parameters/results and implicit permission
adaptation closed, and never execute a dangling view.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.access import view_type
from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import (
    AnnotatedChecker,
    CBackend,
    lower_program_to_c,
    lower_to_c,
)
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.language_policy import current_profile, language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source
from compiler.xe_ast.typesys import I32, ptr

CC = shutil.which(os.environ.get("CC", "gcc"))
MUTABLE_VIEWS = '''fn main()->Unit {
    let[mut] values:Array[i32,4]=[10,20,30,40];
    let part:View[i32@[mut]]=values⟨1..4⟩[@[mut]];
    let copied:View[i32@[mut]]=part;
    let narrowed:View[i32@[mut]]=copied⟨2..=3⟩;
    let pointer:i32@[mut]=narrowed⟦2⟧;
    pointer#=99;
    copied⟦3⟧#=88;
    let readonly:View[i32@]=values⟨1..4⟩[@];
    let empty:View[i32@[mut]]=part⟨4..4⟩;
    let empty_copy:View[i32@[mut]]=empty;
    let same_anchor:View[i32@[mut]]=empty_copy⟨4..4⟩;
    println("{} {} {}",part⟦2⟧#,readonly⟦3⟧#,values⟦2⟧);
}'''
POINTER_ELEMENTS = '''fn main()->Unit {
    let[mut] first:i32=10;
    let[mut] second:i32=20;
    let[mut] slots:Array[i32@[mut],1]=[first@[mut]];
    let writable:View[i32@[mut]@[mut]]=slots⟨..⟩[@[mut]];
    let slot:i32@[mut]@[mut]=writable⟦0⟧;
    slot#=second@[mut];
    let readonly:View[i32@[mut]@]=slots⟨..⟩[@];
    let readonly_slot:i32@[mut]@=readonly⟦0⟧;
    let pointed:i32@[mut]=readonly_slot#;
    pointed#=77;
    println("{} {}",first,second);
}'''
REBOUND = '''fn main()->Unit {
    let[mut] values:Array[i32,4]=[10,20,30,40];
    let[mut] part:View[i32@[mut]]=values⟨0..2⟩[@[mut]];
    let original:View[i32@[mut]]=part;
    if true { part=values⟨2..4⟩[@[mut]]; }
    part⟦3⟧#=99;
    original⟦1⟧#=88;
    println("{} {}",values⟦1⟧,values⟦3⟧);
}'''


class CoreMutableViewTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "mutable-view.xe")
        tree = parse_source(text, source.filename)
        before = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(error.render() for error in diagnostics))
        self.assertEqual(tree, before, "semantic permissions belong in side tables")
        return source, tree, checker

    def rejected(self, text, code):
        diagnostics = check_source(text, "mutable-view.xe")
        self.assertTrue(diagnostics, text)
        self.assertEqual(diagnostics[0].code, code, diagnostics[0].render())

    def test_source_ast_attaches_mut_to_complete_pointer_result(self):
        source, tree, checker = self.checked(MUTABLE_VIEWS)
        bindings = {node["name"]: node for node in tree["items"][0]["body"]["statements"]
                    if node["kind"] == "Binding"}
        annotation = bindings["part"]["type"]
        self.assertEqual(annotation["kind"], "NamedType")
        self.assertEqual(annotation["path"]["parts"], ["View"])
        pointer = annotation["arguments"][0]
        self.assertEqual(pointer["kind"], "PointerType")
        self.assertEqual(pointer["target"]["path"]["parts"], ["i32"])
        self.assertEqual([modifier["name"] for modifier in pointer["modifiers"]], ["mut"])
        self.assertEqual(checker.binding_types[id(bindings["part"])], view_type(ptr(I32, True)))
        self.assertEqual(checker.binding_types[id(bindings["copied"])], view_type(ptr(I32, True)))
        self.assertEqual(checker.binding_types[id(bindings["readonly"])], view_type(ptr(I32)))
        saved = document(source, tree)
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(json.loads(json.dumps(saved))["ast"], tree)

    def test_pointer_elements_add_an_outer_pointer_without_changing_inner_permission(self):
        self.checked(POINTER_ELEMENTS)

    def test_transparent_element_alias_does_not_change_view_permission(self):
        self.checked('''type Number=i32;
        fn main()->Unit {
            let[mut] values:Array[Number,2]=[10,20];
            let part:View[Number@[mut]]=values⟨..⟩[@[mut]];
            let pointer:Number@[mut]=part⟦1⟧;
            pointer#=42;
        }''')

    def test_type_parentheses_only_group_the_same_permission_layer(self):
        self.checked('''fn main()->Unit {
            let[mut] values:Array[i32,2]=[10,20];
            let part:(View[(i32)@[mut]])=values⟨..⟩[@[mut]];
            let copied:(View[i32@[mut]])=part;
            let pointer:(i32)@[mut]=copied⟦1⟧;
            pointer#=42;
        }''')

    def test_immutable_descriptor_binding_still_allows_element_writes(self):
        self.checked('''fn main()->Unit {
            let[mut] values:Array[i32,2]=[1,2];
            let part:View[i32@[mut]]=values⟨..⟩[@[mut]];
            part⟦0⟧#=3;
            part⟦1⟧#=4;
        }''')

    def test_immutable_descriptor_binding_cannot_be_rebound(self):
        self.rejected('''fn main()->Unit {
            let[mut] values:Array[i32,2]=[1,2];
            let part:View[i32@[mut]]=values⟨..⟩[@[mut]];
            part=values⟨1..2⟩[@[mut]];
        }''', "XE-MUT-0001")

    def test_mutable_descriptor_rebinding_does_not_keep_initial_domain(self):
        self.checked(REBOUND)

    def test_writable_view_requires_source_write_permission(self):
        for source in ("values⟨..⟩[@[mut]]", "(values@#)⟨..⟩[@[mut]]"):
            with self.subTest(source=source):
                self.rejected('fn main()->Unit{let values:Array[i32,2]=[1,2];'
                              f'let part:View[i32@[mut]]={source};}}', "XE-MUT-0001")

    def test_mutable_array_pointer_carries_source_write_permission(self):
        self.checked('''fn main()->Unit {
            let[mut] values:Array[i32,2]=[1,2];
            let owner:Array[i32,2]@[mut]=values@[mut];
            let part:View[i32@[mut]]=(owner#)⟨..⟩[@[mut]];
            part⟦1⟧#=42;
        }''')

    def test_readonly_mapping_and_its_subrange_cannot_upgrade_permissions(self):
        for operation in ("readonly⟦0⟧#=3;", "readonly⟨..⟩⟦0⟧#=3;"):
            with self.subTest(operation=operation):
                self.rejected('fn main()->Unit{let[mut] values:Array[i32,2]=[1,2];'
                              'let part:View[i32@[mut]]=values⟨..⟩[@[mut]];'
                              'let[mut] readonly:View[i32@]=values⟨..⟩[@];'
                              + operation + '}', "XE-MUT-0001")

    def test_permission_adaptation_is_not_implicitly_authorized(self):
        for declaration in ("let readonly:View[i32@]=part;",
                            "let readonly:View[i32@]=part⟨..⟩;",
                            "let[mut] readonly:View[i32@]=values⟨..⟩[@];readonly=part;",
                            "let writable:View[i32@[mut]]=values⟨..⟩[@];"):
            with self.subTest(declaration=declaration):
                self.rejected('fn main()->Unit{let[mut] values:Array[i32,2]=[1,2];'
                              'let part:View[i32@[mut]]=values⟨..⟩[@[mut]];'
                              + declaration + '}', "XE-TYPE-0001")

    def test_pointer_element_permission_does_not_grant_slot_write_permission(self):
        self.rejected('''fn main()->Unit {
            let[mut] first:i32=10;
            let[mut] second:i32=20;
            let slots:Array[i32@[mut],1]=[first@[mut]];
            let readonly:View[i32@[mut]@]=slots⟨..⟩[@];
            readonly⟦0⟧#=second@[mut];
        }''', "XE-MUT-0001")

    def test_known_invalid_coordinate_and_empty_anchor_are_compile_errors(self):
        for operation in ("part⟦0⟧;", "part⟦3⟧;", "part⟨0..0⟩;",
                          "part⟨3..=3⟩;", "part⟨2..1⟩;"):
            with self.subTest(operation=operation):
                self.rejected('fn main()->Unit{let[mut] values:Array[i32,4]=[1,2,3,4];'
                              'let part:View[i32@[mut]]=values⟨1..3⟩[@[mut]];'
                              + operation + '}', "XE-INDEX-0001")

    def test_unconnected_view_type_positions_remain_closed(self):
        for text in ("fn inspect(part:View[i32@[mut]])->Unit{unit}",
                     "fn create()->View[i32@[mut]]{panic(\"stop\")}",
                     "type Part=View[i32@[mut]];",
                     "struct Holder{part:View[i32@[mut]],}",
                     "enum Holder{Part[View[i32@[mut]]],}",
                     "fn inspect(pointer:View[i32@[mut]]@)->Unit{unit}",
                     "fn inspect(parts:Array[View[i32@[mut]],2])->Unit{unit}",
                     'fn main()->Unit{let part:View[View[i32]@[mut]]=panic("stop");}',
                     'fn main()->Unit{let part:View[String@[mut]]=panic("stop");}'):
            with self.subTest(text=text):
                self.rejected(text, "XE-CORE-0002")

    def test_view_attachment_is_not_an_arbitrary_type_modifier(self):
        for annotation in ("i32[mut]", "Array[i32,2][mut]", "View[i32@][unsafe]",
                           "View[i32@][mut,mut]", "View[i32@[mut]][mut]", "View[i32@][mut,unsafe]"):
            with self.subTest(annotation=annotation):
                self.assertTrue(check_source('fn main()->Unit{let[mut] values:Array[i32,2]=[1,2];'
                                             f'let part:{annotation}=values⟨..⟩[@[mut]];}}'))

    def test_handwritten_ast_cannot_add_proof_fields_or_change_attachment_shape(self):
        source, original, _ = self.checked(MUTABLE_VIEWS)

        def annotation(tree):
            return tree["items"][0]["body"]["statements"][1]["type"]["arguments"][0]

        def extra_field(node):
            node["permission_proved"] = True

        def duplicate_modifier(node):
            node["modifiers"].append(copy.deepcopy(node["modifiers"][0]))

        def tuple_modifiers(node):
            node["modifiers"] = tuple(node["modifiers"])

        def scalar_target(node):
            node["target"] = {"kind": "PointerType", "span": node["span"], "target": None, "modifiers": []}

        def missing_target(node):
            node["target"] = None

        def changed_modifier_fields(node):
            node["modifiers"][0]["exclusive"] = True

        for change in (extra_field, duplicate_modifier,
                       tuple_modifiers, scalar_target, missing_target, changed_modifier_fields):
            with self.subTest(change=change.__name__):
                tree = copy.deepcopy(original)
                change(annotation(tree))
                before = copy.deepcopy(tree)
                errors = Checker(source, tree).check()
                self.assertEqual([error.code for error in errors], ["XE-CORE-0001"])
                for consume in (lambda tree=tree: document(source, tree),
                                lambda tree=tree: lower_program_to_c(source, tree),
                                lambda tree=tree: CBackend(AnnotatedChecker(source, tree)).generate()):
                    with self.subTest(consumer=consume), self.assertRaises(Diagnostic) as caught:
                        consume()
                    self.assertEqual(caught.exception.code, "XE-CORE-0001")
                self.assertEqual(tree, before)

    def test_valid_pointer_annotation_changes_are_checked_as_type_relations(self):
        # [] and [unsafe] are valid pointer forms, not malformed source ASTs.
        # Replacing [mut] changes P, so the writable constructor must mismatch.
        source, original, _ = self.checked(MUTABLE_VIEWS)
        for modifiers in ([], [{"kind": "Modifier", "span": original["span"], "name": "unsafe"}]):
            tree = copy.deepcopy(original)
            pointer = tree["items"][0]["body"]["statements"][1]["type"]["arguments"][0]
            pointer["modifiers"] = modifiers
            with self.subTest(modifiers=modifiers):
                self.assertEqual(document(source, tree)["ast"], tree)
                self.assertEqual([error.code for error in Checker(source, tree).check()], ["XE-TYPE-0001"])
                with self.assertRaises(Diagnostic) as caught:
                    lower_program_to_c(source, tree)
                self.assertEqual(caught.exception.code, "XE-TYPE-0001")

    def test_core_mutable_view_ast_cannot_enter_explicit_legacy(self):
        source, tree, _ = self.checked(MUTABLE_VIEWS)
        before = copy.deepcopy(tree)
        with language_profile("legacy"):
            self.assertEqual([error.code for error in Checker(source, tree).check()], ["XE-CORE-0002"])
            for consume in (lambda: document(source, tree), lambda: lower_program_to_c(source, tree)):
                with self.assertRaises(Diagnostic) as caught:
                    consume()
                self.assertEqual(caught.exception.code, "XE-CORE-0002")
        self.assertEqual(current_profile(), "core")
        self.assertEqual(tree, before)


@unittest.skipUnless(CC, "actual mutable view execution requires a C compiler")
class CoreMutableViewBackendTests(unittest.TestCase):
    def execution(self, text, *, sanitize=False):
        assert CC is not None  # The class-level skip protects execution; narrow for Pyright.
        self.assertEqual(current_profile(), "core")
        generated = lower_to_c(text, "mutable-view.xe")
        self.assertIn("size_t lower, upper;", generated)
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory(prefix="xe-mutable-view-") as directory:
            root = Path(directory)
            source, program = root / "main.xe", root / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, program, cc=CC, extra_flags=flags)
            # Do not turn off LSan to make a sandbox/environment failure pass.
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
        self.assertEqual(current_profile(), "core")
        return result

    def execute(self, text, expected, *, sanitize=False):
        result = self.execution(text, sanitize=sanitize)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)

    def runtime_failure(self, text, operation, message, *, sanitize=False):
        result = self.execution(text, sanitize=sanitize)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        offset = text.index(operation)
        line = text.count("\n", 0, offset) + 1
        column = offset - text.rfind("\n", 0, offset)
        self.assertEqual(result.stderr.splitlines()[0], f"Xe runtime error: {message}")
        self.assertEqual(len(result.stderr.splitlines()), 2, result.stderr)
        self.assertTrue(result.stderr.splitlines()[1].endswith(f"/main.xe:{line}:{column}"), result.stderr)

    def test_copied_descriptors_alias_elements_and_preserve_source_coordinates(self):
        self.execute(MUTABLE_VIEWS, "99 88 99\n")

    def test_pointer_elements_keep_both_layers_at_runtime(self):
        self.execute(POINTER_ELEMENTS, "10 77\n")

    def test_mutable_binding_rebinds_without_retargeting_existing_copies(self):
        self.execute(REBOUND, "88 99\n")

    def test_mutable_array_pointer_source_writes_original_storage(self):
        self.execute('''fn main()->Unit {
            let[mut] values:Array[i32,2]=[10,20];
            let owner:Array[i32,2]@[mut]=values@[mut];
            let part:View[i32@[mut]]=(owner#)⟨..⟩[@[mut]];
            part⟦1⟧#=42;
            println("{}",values⟦1⟧);
        }''', "42\n")

    def test_zero_length_array_and_tail_empty_writable_views_are_valid(self):
        self.execute('''fn main()->Unit {
            let[mut] zero:Array[i32,0]=[];
            let empty:View[i32@[mut]]=zero⟨..⟩[@[mut]];
            let copied:View[i32@[mut]]=empty;
            let same_anchor:View[i32@[mut]]=copied⟨0..0⟩;
            println("empty");
        }''', "empty\n")

    def test_dynamic_boundaries_use_original_coordinates(self):
        self.execute('''fn lower()->usize{1}
        fn upper()->usize{4}
        fn coordinate()->usize{2}
        fn main()->Unit {
            let[mut] values:Array[i32,4]=[10,20,30,40];
            let part:View[i32@[mut]]=values⟨lower()..upper()⟩[@[mut]];
            part⟦coordinate()⟧#=99;
            println("{}",values⟦2⟧);
        }''', "99\n")

    def test_rebinding_never_uses_old_domain_to_accept_invalid_literal_coordinate(self):
        text = '''fn main()->Unit {
    let[mut] values:Array[i32,4]=[10,20,30,40];
    let[mut] part:View[i32@[mut]]=values⟨0..2⟩[@[mut]];
    part=values⟨2..4⟩[@[mut]];
    part⟦0⟧#=99;
}'''
        self.runtime_failure(text, "part⟦0⟧", "array index out of bounds")

    def test_dynamic_index_rejects_tail_before_write(self):
        text = '''fn main()->Unit {
    let[mut] values:Array[i32,3]=[10,20,30];
    let part:View[i32@[mut]]=values⟨1..3⟩[@[mut]];
    let index:usize=3;
    part⟦index⟧#=99;
}'''
        self.runtime_failure(text, "part⟦index⟧", "array index out of bounds")

    def test_dynamic_empty_subrange_outside_current_domain_is_rejected(self):
        text = '''fn main()->Unit {
    let[mut] values:Array[i32,4]=[10,20,30,40];
    let part:View[i32@[mut]]=values⟨1..3⟩[@[mut]];
    let outside:usize=0;
    let bad:View[i32@[mut]]=part⟨outside..outside⟩;
}'''
        self.runtime_failure(text, "part⟨outside..outside⟩", "slice range out of bounds")

    @unittest.skipUnless(sys.platform.startswith("linux"), "sanitizer execution requires Linux")
    def test_writable_aliases_under_asan_ubsan_and_default_lsan(self):
        self.execute(MUTABLE_VIEWS, "99 88 99\n", sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "sanitizer execution requires Linux")
    def test_pointer_elements_under_asan_ubsan_and_default_lsan(self):
        self.execute(POINTER_ELEMENTS, "10 77\n", sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "sanitizer execution requires Linux")
    def test_rebound_domains_under_asan_ubsan_and_default_lsan(self):
        self.execute(REBOUND, "88 99\n", sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "sanitizer execution requires Linux")
    def test_failed_dynamic_write_under_asan_ubsan_and_default_lsan(self):
        text = '''fn main()->Unit {
    let[mut] values:Array[i32,3]=[10,20,30];
    let part:View[i32@[mut]]=values⟨1..3⟩[@[mut]];
    let index:usize=3;
    part⟦index⟧#=99;
}'''
        self.runtime_failure(text, "part⟦index⟧", "array index out of bounds", sanitize=True)


if __name__ == "__main__":
    unittest.main()
