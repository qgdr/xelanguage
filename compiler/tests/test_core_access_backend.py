"""Actual core Array location and original-coordinate view lowering.

These are public frontend-to-executable tests, not permission to run an
internally parsed, unadmitted AST. They intentionally avoid iterator APIs and
unreviewed View parameter/return positions. Array indexing names a T box;
View[P] indexing produces P, and Array address mappings use [@]/[@[mut]].
"""
import copy
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend, lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.language_policy import current_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Source

CC = shutil.which(os.environ.get("CC", "gcc"))
LOCATIONS = '''fn main()->Unit {
    let values:Array[i32, 6] = [10,20,30,40,50,60];
    let pointer:i32@ = values⟦3⟧@;
    let nested:i32 = values⟨1..6⟩[@]⟨2..5⟩⟦3⟧#;
    let grouped:i32 = (values⟨1..6⟩[@])⟦4⟧#;
    println("{} {} {}", pointer#, nested, grouped);
}'''
DYNAMIC = '''fn source(values:Array[i32, 4]@)->Array[i32, 4]@ {
    println("source");
    values
}
fn lower()->usize { println("lower"); 1 }
fn upper()->usize { println("upper"); 4 }
fn coordinate()->usize { println("index"); 2 }
fn main()->Unit {
    let values:Array[i32, 4] = [10,20,30,40];
    let pointer:i32@ = (source(values@)#)⟨lower()..upper()⟩[@]⟦coordinate()⟧;
    println("{}", pointer#);
}'''
ALIASES_AND_EMPTY = '''fn main()->Unit {
    let[mut] values:Array[i32, 4] = [10,20,30,40];
    let pointer:i32@[mut] = values⟨1..4⟩[@[mut]]⟦2⟧;
    let alias:i32@[mut] = pointer;
    alias# = 70;
    let part:View[i32@] = values⟨1..4⟩[@];
    let readonly:View[i32@] = part⟨1..4⟩;
    let readonly_copy:View[i32@] = readonly;
    let left_empty:View[i32@] = values⟨0..0⟩[@];
    let tail_empty:View[i32@] = values⟨4..4⟩[@];
    let nested_empty:View[i32@] = part⟨4..4⟩;
    let empty_copy:View[i32@] = nested_empty;
    let same_anchor:View[i32@] = empty_copy⟨4..4⟩;
    let zero:Array[i32, 0] = [];
    let zero_view:View[i32@] = zero⟨0..0⟩[@];
    println("{} {} {}", part⟦2⟧#, readonly_copy⟦2⟧#, values⟦2⟧);
}'''


class CoreAccessBackendTests(unittest.TestCase):
    def generated(self, text):
        self.assertEqual(current_profile(), "core")
        source = Source(text, "coordinate-access.xe")
        tree = parse_source(text, source.filename)
        before = copy.deepcopy(tree)
        self.assertEqual(document(source, tree)["schema_version"], 1)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(error.render() for error in diagnostics))
        generated = CBackend(checker).generate()
        self.assertEqual(tree, before, "lowering must not annotate the source AST")
        return generated

    def execution(self, text, *, sanitize=False):
        self.generated(text)
        if CC is None:
            self.skipTest("actual execution requires a C compiler")
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory(prefix="xe-coordinate-backend-") as directory:
            root = Path(directory)
            source, program = root / "main.xe", root / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, program, cc=CC, extra_flags=flags)
            # Preserve the default leak detector. An environment failure is
            # not a successful sanitizer run and must remain visible.
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
        # Exact stderr also rejects accidentally hidden ASan/UBSan reports.
        self.assertEqual(result.stderr.splitlines()[0], f"Xe runtime error: {message}")
        self.assertEqual(len(result.stderr.splitlines()), 2, result.stderr)
        self.assertTrue(result.stderr.splitlines()[1].endswith(f"/main.xe:{line}:{column}"), result.stderr)

    def test_index_box_address_points_to_original_element(self):
        self.execute('''fn main()->Unit {
            let values:Array[i32, 3] = [10,20,30];
            let first:i32@ = values⟦0⟧@;
            let last:i32@ = values⟦2⟧@;
            println("{} {}", first#, last#);
        }''', "10 30\n")

    def test_nested_restriction_and_grouped_calls_keep_source_coordinates(self):
        self.execute(LOCATIONS, "40 40 50\n")

    def test_pointer_elements_preserve_the_additional_pointer_layer(self):
        self.execute('''fn main()->Unit {
            let first:i32 = 10;
            let second:i32 = 20;
            let values:Array[i32@, 2] = [first@, second@];
            let element:i32@@ = values⟦1⟧@;
            println("{}", element##);
        }''', "20\n")

    def test_explicit_array_pointer_dereference_preserves_storage(self):
        self.execute('''fn main()->Unit {
            let values:Array[i32, 3] = [10,20,30];
            let owner:Array[i32, 3]@ = values@;
            let element:i32@ = (owner#)⟦1⟧@;
            println("{}", element#);
        }''', "20\n")

    def test_location_points_into_original_array_not_a_copy(self):
        self.execute('''fn main()->Unit {
            let[mut] values:Array[i32, 3] = [10,20,30];
            let element:i32@ = values⟦1⟧@;
            values = [40,50,60];
            println("{} {}", element#, values⟦1⟧);
        }''', "50 50\n")

    def test_point_and_range_mutability_do_not_copy_elements(self):
        self.execute('''fn main()->Unit {
            let[mut] values:Array[i32, 4] = [10,20,30,40];
            let first:i32@[mut] = values⟦1⟧@[mut];
            first# = 50;
            let second:i32@[mut] = values⟨1..4⟩[@[mut]]⟦2⟧;
            second# = 60;
            println("{} {}", values⟦1⟧, values⟦2⟧);
        }''', "50 60\n")

    def test_inclusive_range_is_normalized_without_rebasing(self):
        self.execute('''fn main()->Unit {
            let values:Array[i32, 5] = [10,20,30,40,50];
            let first:i32 = values⟨1..=3⟩[@]⟦1⟧#;
            let last:i32 = values⟨1..=3⟩[@]⟨2..=3⟩⟦3⟧#;
            println("{} {}", first, last);
        }''', "20 40\n")

    def test_grouped_range_and_full_restriction_keep_coordinate_metadata(self):
        self.execute('''fn main()->Unit {
            let values:Array[i32, 4] = [10,20,30,40];
            let grouped:i32 = values⟨(1..4)⟩[@]⟦2⟧#;
            let full:i32 = values⟨..⟩[@]⟦3⟧#;
            println("{} {}", grouped, full);
        }''', "30 40\n")

    def test_source_expression_is_evaluated_once_before_each_location(self):
        # Literal coordinates keep this test inside the currently admitted
        # static contract. The source call is still observable and must never
        # be duplicated when forming data/lower/upper or nesting restrictions.
        self.execute('''fn source(values:Array[i32, 4]@)->Array[i32, 4]@ {
            println("source");
            values
        }
        fn main()->Unit {
            let values:Array[i32, 4] = [10,20,30,40];
            let first:i32 = (source(values@)#)⟦1⟧;
            println("{}", first);
            let second:i32 = (source(values@)#)⟨1..4⟩[@]⟨2..4⟩⟦2⟧#;
            println("{}", second);
        }''', "source\n20\nsource\n30\n")

    def test_dynamic_source_bounds_and_index_evaluate_once_in_source_order(self):
        self.execute(DYNAMIC, "source\nlower\nupper\nindex\n30\n")

    def test_lower_endpoint_is_snapshotted_before_upper_changes_its_binding(self):
        self.execute('''fn upper(lower:usize@[mut])->usize {
            lower# = 3;
            4
        }
        fn main()->Unit {
            let values:Array[i32, 4] = [10,20,30,40];
            let[mut] lower:usize = 1;
            let part:View[i32@] = values⟨lower..upper(lower@[mut])⟩[@];
            println("{} {}", part⟦1⟧#, lower);
        }''', "20 3\n")

    def test_array_storage_address_is_snapshotted_before_endpoint_retargets_pointer(self):
        # Keep the retargeting in a known local box. The former helper wrote
        # an address through an unknown parameter target; DEC-038 reserves that
        # combination until write-back/retention summaries are available.
        self.execute('''fn main()->Unit {
            let first:Array[i32, 3] = [10,20,30];
            let second:Array[i32, 3] = [40,50,60];
            let[mut] selected:Array[i32, 3]@ = first@;
            let part:View[i32@] = (selected#)⟨{selected=second@;1}..3⟩[@];
            println("{} {}", part⟦1⟧#, (selected#)⟦1⟧);
        }''', "20 50\n")

    def test_return_from_index_stops_before_location_and_following_code(self):
        self.execute('''fn compute()->i32 {
            let values:Array[i32, 2] = [10,20];
            values⟦{ return 42; }⟧@;
            println("unreachable");
            0
        }
        fn main()->Unit { println("{}", compute()); }''', "42\n")

    def test_return_from_lower_endpoint_does_not_evaluate_upper(self):
        self.execute('''fn upper()->usize {
            println("unreachable upper");
            2
        }
        fn compute()->i32 {
            let values:Array[i32, 2] = [10,20];
            values⟨{ return 42; }..upper()⟩[@];
            println("unreachable");
            0
        }
        fn main()->Unit { println("{}", compute()); }''', "42\n")

    def test_return_from_upper_endpoint_keeps_prior_lower_effect(self):
        self.execute('''fn lower()->usize {
            println("lower");
            0
        }
        fn compute()->i32 {
            let values:Array[i32, 2] = [10,20];
            values⟨lower()..{ return 42; }⟩[@];
            println("unreachable");
            0
        }
        fn main()->Unit { println("{}", compute()); }''', "lower\n42\n")

    def test_returning_index_block_does_not_attempt_final_dereference(self):
        self.execute('''fn compute()->i32 {
            let values:Array[i32, 2] = [10,20];
            values⟦{ return 42; }⟧
        }
        fn main()->Unit { println("{}", compute()); }''', "42\n")

    def test_returning_source_block_skips_index_and_final_dereference(self):
        self.execute('''fn index()->usize {
            println("unreachable index");
            0
        }
        fn compute()->i32 {
            let values:Array[i32, 2] = [10,20];
            ({ println("source"); return 42; values })⟦index()⟧
        }
        fn main()->Unit { println("{}", compute()); }''', "source\n42\n")

    def test_returning_slice_source_skips_endpoints_nested_index_and_dereference(self):
        self.execute('''fn lower()->usize { println("unreachable lower"); 0 }
        fn upper()->usize { println("unreachable upper"); 2 }
        fn index()->usize { println("unreachable index"); 0 }
        fn compute()->i32 {
            let values:Array[i32, 2] = [10,20];
            ({ println("source"); return 42; values })⟨lower()..upper()⟩[@]⟦index()⟧#
        }
        fn main()->Unit { println("{}", compute()); }''', "source\n42\n")

    def test_copy_descriptors_share_elements_and_empty_views_keep_anchors(self):
        self.execute(ALIASES_AND_EMPTY, "70 70 70\n")

    def test_mutable_view_binding_uses_rebound_runtime_coordinates(self):
        self.execute('''fn main()->Unit {
            let values:Array[i32, 4] = [10,20,30,40];
            let[mut] part:View[i32@] = values⟨0..2⟩[@];
            if true { part = values⟨2..4⟩[@]; }
            println("{}", part⟦3⟧#);
        }''', "40\n")

    def test_dynamic_index_rejects_tail_before_any_pointer_access(self):
        text = '''fn main()->Unit {
    let values:Array[i32, 3] = [10,20,30];
    let index:usize = 3;
    let bad:i32@ = values⟦index⟧@;
}'''
        self.runtime_failure(text, "values⟦index⟧@", "array index out of bounds")

    def test_dynamic_empty_range_outside_parent_domain_does_not_pass_vacuously(self):
        text = '''fn main()->Unit {
    let values:Array[i32, 4] = [10,20,30,40];
    let part:View[i32@] = values⟨1..3⟩[@];
    let outside:usize = 0;
    let bad:View[i32@] = part⟨outside..outside⟩;
}'''
        self.runtime_failure(text, "part⟨outside..outside⟩", "slice range out of bounds")

    def test_dynamic_index_cannot_dereference_an_empty_view(self):
        text = '''fn main()->Unit {
    let values:Array[i32, 3] = [10,20,30];
    let empty:View[i32@] = values⟨3..3⟩[@];
    let index:usize = 3;
    let bad:i32@ = empty⟦index⟧;
}'''
        self.runtime_failure(text, "empty⟦index⟧", "array index out of bounds")

    def test_inclusive_dynamic_maximum_is_rejected_before_end_increment(self):
        maximum = str(2 ** (8 * struct.calcsize("P")) - 1)
        text = '''fn main()->Unit {
    let values:Array[i32, 3] = [10,20,30];
    let upper:usize = MAXIMUM;
    let bad:View[i32@] = values⟨0..=upper⟩[@];
}'''.replace("MAXIMUM", maximum)
        self.runtime_failure(text, "values⟨0..=upper⟩[@]", "slice range out of bounds",
                             sanitize=sys.platform.startswith("linux"))

    def test_location_syntax_does_not_change_ordinary_named_function_calls(self):
        self.execute('''fn identity(value:i32)->i32 { value }
        fn main()->Unit {
            let values:Array[i32, 2] = [10,20];
            let answer:i32 = identity(values⟦1⟧);
            let piped:i32 = values⟨0..2⟩[@]⟦0⟧# |> identity;
            println("{} {}", answer, piped);
        }''', "20 10\n")

    def test_view_layout_retains_both_coordinate_boundaries(self):
        generated = self.generated(LOCATIONS)
        # Layout names are deliberately mangled C identifiers. The public
        # contract is retained coordinate metadata, not that implementation
        # naming convention or its current underscore encoding.
        self.assertIn("size_t lower, upper;", generated)
        self.assertIn(".lower =", generated)
        self.assertIn(".upper =", generated)
        self.assertIn("array index out of bounds", generated)
        self.assertIn("slice range out of bounds", generated)
        self.assertEqual(lower_to_c(LOCATIONS, "coordinate-access.xe"), generated)

    @unittest.skipUnless(CC and sys.platform.startswith("linux"), "sanitizer execution requires Linux C compiler")
    def test_valid_original_coordinate_accesses_under_asan_ubsan_and_default_lsan(self):
        self.execute(LOCATIONS, "40 40 50\n", sanitize=True)

    @unittest.skipUnless(CC and sys.platform.startswith("linux"), "sanitizer execution requires Linux C compiler")
    def test_dynamic_accesses_under_asan_ubsan_and_default_lsan(self):
        self.execute(DYNAMIC, "source\nlower\nupper\nindex\n30\n", sanitize=True)

    @unittest.skipUnless(CC and sys.platform.startswith("linux"), "sanitizer execution requires Linux C compiler")
    def test_empty_and_aliased_views_under_asan_ubsan_and_default_lsan(self):
        self.execute(ALIASES_AND_EMPTY, "70 70 70\n", sanitize=True)


if __name__ == "__main__":
    unittest.main()
