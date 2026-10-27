"""Executable evidence for box indexing and source-coordinate View[P].

These tests enter through the public parser/checker/build path.  They keep
resource arrays and dangling-pointer programs out of execution; runtime bounds
failures abort before accessing storage.  Sanitizer runs retain default LSan.
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
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Source

CC = shutil.which(os.environ.get("CC", "gcc"))
BOXES_AND_VIEWS = '''fn main()->Unit {
    let[mut] a:Array[i32,4]=[10,20,30,40];
    let copied:i32=a⟦1⟧;
    let direct:i32@=a⟦1⟧@;
    let[mut] readonly:View[i32@]=a⟨1..3⟩[@];
    let writable:View[i32@[mut]]=a⟨1..3⟩[@[mut]];
    let child:View[i32@[mut]]=writable⟨1..2⟩;
    a⟦1⟧=77;
    child⟦1⟧#=99;
    println("{} {} {} {}",copied,direct#,readonly⟦1⟧#,a⟦1⟧);
}'''
EMPTY_VIEWS = '''fn main()->Unit {
    let[mut] a:Array[i32,4]=[10,20,30,40];
    let w:View[i32@[mut]]=a⟨1..4⟩[@[mut]];
    let alias:View[i32@[mut]]=w;
    let child:View[i32@[mut]]=alias⟨2..4⟩;
    let tail:View[i32@[mut]]=child⟨4..4⟩;
    let copy:View[i32@[mut]]=tail;
    let empty:View[i32@[mut]]=copy⟨4..4⟩;
    let zero:Array[i32,0]=[];
    let zero_view:View[i32@]=zero⟨..⟩[@];
    let same:View[i32@]=zero_view⟨0..0⟩;
    child⟦2⟧#=70;
    println("{} {}",a⟦2⟧,alias⟦2⟧#);
}'''
TEMPORARY_CALLS = '''fn make()->Array[i32,2] {
    println("make");
    [10,20]
}
fn read(p:i32@)->i32 { p# }
fn consume(p:i32@,ignored:i32)->i32 {
    println("read {}",p#);
    ignored
}
fn main()->Unit {
    let first:i32=make()⟦0⟧;
    let second:i32=read(make()⟦1⟧@);
    consume(make()⟦0⟧@,{
        println("later argument");
        let local:Array[i32,2]=[30,40];
        println("local {}",local⟦1⟧);
        7
    });
    println("{} {}",first,second);
}'''
EARLY_RETURN = '''fn make()->Array[i32,2] { [10,20] }
fn consume(text:String,p:i32@,last:i32)->i32 { p# }
fn compute()->i32 {
    let result:i32=consume(String::from("owned"),make()⟦0⟧@,{
        return 42;
        0
    });
    result
}
fn main()->Unit { println("{}",compute()); }'''


class BoxAccessBackendTests(unittest.TestCase):
    def generated(self, text):
        source = Source(text, "box-access.xe")
        tree = parse_source(text, source.filename)
        before = copy.deepcopy(tree)
        self.assertEqual(document(source, tree)["schema_version"], 1)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(error.render() for error in diagnostics))
        generated = CBackend(checker).generate()
        self.assertEqual(tree, before, "lowering must not annotate the public syntax tree")
        return generated

    def execution(self, text, *, sanitize=False):
        self.generated(text)
        if CC is None:
            self.skipTest("actual execution requires a C compiler")
        flags = (("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie")
                 if sanitize else ())
        with tempfile.TemporaryDirectory(prefix="xe-box-access-backend-") as directory:
            root = Path(directory)
            source, program = root / "main.xe", root / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, program, cc=CC, extra_flags=flags)
            # Keep the default leak detector. A sandbox/environment failure is
            # not evidence that the sanitizer accepted the program.
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
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
        self.assertTrue(result.stderr.splitlines()[1].endswith(
            f"/main.xe:{line}:{column}"), result.stderr)

    def test_array_boxes_and_view_pointer_values_share_original_storage(self):
        self.execute(BOXES_AND_VIEWS, "20 99 99 99\n")

    def test_explicit_mutable_address_and_pointer_array_element_layers(self):
        self.execute('''fn main()->Unit {
            let[mut] value:i32=10;
            let pointer:i32@[mut]=value@[mut];
            let pointers:Array[i32@[mut],2]=[pointer,pointer];
            let selected:i32@[mut]=pointers⟦1⟧;
            let slot:i32@[mut]@=pointers⟦1⟧@;
            let view:View[i32@[mut]@]=pointers⟨0..2⟩[@];
            selected#=77;
            println("{} {} {}",value,slot##,view⟦1⟧##);
        }''', "77 77 77\n")

    def test_array_element_write_and_address_use_one_evaluated_coordinate(self):
        self.execute('''fn coordinate()->usize { println("index"); 1 }
        fn main()->Unit {
            let[mut] a:Array[i32,3]=[10,20,30];
            a⟦coordinate()⟧=40;
            let p:i32@[mut]=a⟦coordinate()⟧@[mut];
            p#=50;
            println("{}",a⟦1⟧);
        }''', "index\nindex\n50\n")

    def test_mutable_subview_copy_keeps_access_and_empty_anchors(self):
        self.execute(EMPTY_VIEWS, "70 70\n")

    def test_inclusive_and_grouped_ranges_keep_source_coordinates(self):
        self.execute('''fn main()->Unit {
            let a:Array[i32,5]=[10,20,30,40,50];
            let first:View[i32@]=a⟨(1..=4)⟩[@];
            let last:View[i32@]=first⟨2..=3⟩;
            println("{} {}",last⟦2⟧#,last⟦3⟧#);
        }''', "30 40\n")

    def test_source_bounds_and_index_execute_once_in_written_order(self):
        self.execute('''fn source(p:Array[i32,4]@)->Array[i32,4]@ {
            println("source"); p
        }
        fn lower()->usize { println("lower"); 1 }
        fn upper()->usize { println("upper"); 4 }
        fn coordinate()->usize { println("index"); 2 }
        fn main()->Unit {
            let a:Array[i32,4]=[10,20,30,40];
            let value:i32=(source(a@)#)⟨lower()..upper()⟩[@]⟦coordinate()⟧#;
            println("{}",value);
        }''', "source\nlower\nupper\nindex\n30\n")

    def test_lower_endpoint_is_saved_before_upper_changes_its_box(self):
        self.execute('''fn upper(lower:usize@[mut])->usize { lower#=3; 4 }
        fn main()->Unit {
            let a:Array[i32,4]=[10,20,30,40];
            let[mut] lower:usize=1;
            let view:View[i32@]=a⟨lower..upper(lower@[mut])⟩[@];
            println("{} {}",view⟦1⟧#,lower);
        }''', "20 3\n")

    def test_view_descriptor_snapshot_precedes_index_rebinding(self):
        self.execute('''fn main()->Unit {
            let a:Array[i32,4]=[10,20,30,40];
            let[mut] view:View[i32@]=a⟨0..2⟩[@];
            let before:i32=view⟦{ view=a⟨2..4⟩[@]; 1 }⟧#;
            println("{} {}",before,view⟦3⟧#);
        }''', "20 40\n")

    def test_array_storage_snapshot_precedes_index_pointer_rebinding(self):
        self.execute('''fn main()->Unit {
            let first:Array[i32,3]=[10,20,30];
            let second:Array[i32,3]=[40,50,60];
            let[mut] selected:Array[i32,3]@=first@;
            let before:i32=(selected#)⟦{ selected=second@; 1 }⟧;
            println("{} {}",before,(selected#)⟦1⟧);
        }''', "20 50\n")

    def test_readonly_temporary_arrays_last_through_complete_initialization_and_call(self):
        self.execute(TEMPORARY_CALLS,
                     "make\nmake\nmake\nlater argument\nlocal 40\nread 10\n10 20\n")

    def test_copying_external_pointer_element_does_not_target_temporary_array_slot(self):
        self.execute('''fn pointers(p:i32@)->Array[i32@,1] { [p] }
        fn main()->Unit {
            let original:i32=9;
            let pointer:i32@=pointers(original@)⟦0⟧;
            println("{}",pointer#);
        }''', "9\n")

    def test_later_argument_return_cleans_previously_evaluated_owned_resource(self):
        self.execute(EARLY_RETURN, "42\n")

    def test_index_return_skips_the_actual_address_calculation(self):
        self.execute('''fn compute()->i32 {
            let a:Array[i32,2]=[10,20];
            a⟦{ return 42; }⟧;
            0
        }
        fn main()->Unit { println("{}",compute()); }''', "42\n")

    def test_dynamic_array_tail_fails_before_access(self):
        text = '''fn main()->Unit {
    let a:Array[i32,3]=[10,20,30];
    let coordinate:usize=3;
    let value:i32=a⟦coordinate⟧;
}'''
        self.runtime_failure(text, "a⟦coordinate⟧", "array index out of bounds")

    def test_dynamic_empty_range_cannot_expand_the_current_domain(self):
        text = '''fn main()->Unit {
    let a:Array[i32,4]=[10,20,30,40];
    let view:View[i32@]=a⟨1..3⟩[@];
    let outside:usize=0;
    let bad:View[i32@]=view⟨outside..outside⟩;
}'''
        self.runtime_failure(text, "view⟨outside..outside⟩", "slice range out of bounds")

    def test_inclusive_maximum_fails_before_normalization_overflows(self):
        maximum = str(2 ** (8 * struct.calcsize("P")) - 1)
        text = '''fn main()->Unit {
    let a:Array[i32,3]=[10,20,30];
    let upper:usize=MAXIMUM;
    let bad:View[i32@]=a⟨0..=upper⟩[@];
}'''.replace("MAXIMUM", maximum)
        self.runtime_failure(text, "a⟨0..=upper⟩[@]", "slice range out of bounds")

    def test_source_ast_remains_unannotated_and_layout_keeps_coordinate_bounds(self):
        generated = self.generated(BOXES_AND_VIEWS)
        self.assertIn("size_t lower, upper;", generated)
        self.assertIn("array index out of bounds", generated)
        self.assertIn("slice range out of bounds", generated)
        self.assertEqual(generated, lower_to_c(BOXES_AND_VIEWS, "box-access.xe"))

    @unittest.skipUnless(CC and sys.platform.startswith("linux"), "sanitizer execution requires Linux C compiler")
    def test_boxes_views_and_empty_aliases_under_sanitizers(self):
        self.execute(BOXES_AND_VIEWS, "20 99 99 99\n", sanitize=True)
        self.execute(EMPTY_VIEWS, "70 70\n", sanitize=True)

    @unittest.skipUnless(CC and sys.platform.startswith("linux"), "sanitizer execution requires Linux C compiler")
    def test_temporary_call_lifetimes_under_sanitizers(self):
        self.execute(TEMPORARY_CALLS,
                     "make\nmake\nmake\nlater argument\nlocal 40\nread 10\n10 20\n", sanitize=True)

    @unittest.skipUnless(CC and sys.platform.startswith("linux"), "sanitizer execution requires Linux C compiler")
    def test_early_return_resource_cleanup_under_sanitizers(self):
        self.execute(EARLY_RETURN, "42\n", sanitize=True)


if __name__ == "__main__":
    unittest.main()
