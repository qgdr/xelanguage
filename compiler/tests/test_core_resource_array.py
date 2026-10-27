"""Whole resource Arrays own every initialized element, without public holes.

Only complete construction, whole transfer and cleanup are enabled here. The
source language still rejects resource element indexing/views in this increment.
Tracing observes cleanup responsibility without choosing a new element order;
the separate sanitizer run uses the unchanged public build path.
"""

import copy
import os
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from compiler.tests import test_core_enum_backend as enum_backend
from compiler.tests.documentation_support import private_document
from compiler.tests.test_repository import fenced_blocks
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend
from compiler.xe_ast.build import build_executable, compile_generated
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import check_source
from compiler.xe_ast.source import Source

CC = shutil.which(os.environ.get("CC", "gcc"))
ROOT = Path(__file__).resolve().parents[2]

WHOLE = '''type Pair=Array[String,2];
fn relay(value:Pair)->Pair{value}
fn consume(value:Pair)->Unit{println("consume");}
fn main()->Unit {
    let original:Pair<<[String::from("left"),String::from("right")];
    let moved:Pair<<relay(original);
    consume(moved);
    println("after");
}'''

REPLACE = '''type Pair=Array[String,2];
fn consume(value:Pair)->Unit{unit}
fn main()->Unit {
    let[mut] values:Pair;
    values<<[String::from("old1"),String::from("old2")];
    values<<{consume(values);[String::from("new1"),String::from("new2")]};
    consume(values);
    [String::from("last1"),String::from("last2")] >> values;
    println("done");
}'''

PARTIAL = '''fn first()->Unit {
    let values:Array[String,2]<<[
        {return;String::from("never1")},String::from("never2")
    ];
}
fn second()->Unit {
    let values:Array[String,2]<<[
        String::from("built"),{return;String::from("never3")}
    ];
}
fn main()->Unit {first();second();println("done");}'''


class CoreResourceArrayTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "core-resource-array.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        self.assertEqual(checker.warnings, [])
        generated = CBackend(checker).generate()
        self.assertEqual(tree, original)
        return generated

    def execute(self, text, expected, *, drops=None, sanitize=False):
        generated = self.checked(text)
        if CC is None:
            self.skipTest("whole resource Array execution needs GCC/Clang")
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory(prefix="xe-resource-array-") as directory:
            root = Path(directory)
            source, output = root / "main.xe", root / "program"
            source.write_text(text, encoding="utf-8")
            if drops is None:
                build_executable(source, output, cc=CC, extra_flags=flags)
            else:
                self.assertEqual(generated.count(enum_backend.DROP_HEAD), 1)
                generated = generated.replace(enum_backend.DROP_HEAD,
                                              enum_backend.DROP_HEAD + enum_backend.DROP_TRACE, 1)
                compile_generated(generated, root / "traced.c", output, cc=CC, extra_flags=flags)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        lines = result.stdout.splitlines()
        if drops is not None:
            self.assertEqual(Counter(line[5:] for line in lines if line.startswith("drop:")), Counter(drops))
            normal = [line for line in lines if not line.startswith("drop:")]
            self.assertEqual(normal, expected.splitlines())
        else:
            self.assertEqual(result.stdout, expected)
        return lines

    def rejected(self, text, code):
        errors = check_source(text, "core-resource-array.xe")
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def test_whole_parameter_and_return_transfer_cleanup_once(self):
        lines = self.execute(WHOLE, "consume\nafter\n", drops=["left", "right"])
        for value in ("left", "right"):
            self.assertLess(lines.index("consume"), lines.index("drop:" + value))
            self.assertLess(lines.index("drop:" + value), lines.index("after"))

    def test_partial_construction_cleans_only_completed_elements(self):
        self.execute(PARTIAL, "done\n", drops=["built"])

    def test_local_replacement_uses_rhs_actual_state_and_supports_reconstruction(self):
        self.execute(REPLACE, "done\n", drops=["old1", "old2", "new1", "new2", "last1", "last2"])

    def test_nested_resource_arrays_and_empty_array_need_no_element_move(self):
        text = '''type Row=Array[String,2];type Board=Array[Row,2];
        fn row(a:str,b:str)->Row{[String::from(a),String::from(b)]}
        fn main()->Unit {
            let board:Board<<[row("a","b"),row("c","d")];
            let empty:Array[String,0]<<[];
            println("done");
        }'''
        self.execute(text, "done\n", drops=["a", "b", "c", "d"])

    def test_whole_arrays_of_resource_structs_and_active_enum_payloads(self):
        text = '''struct Envelope {text:String,}
        enum Event {Text[String],End,}
        fn main()->Unit {
            let envelopes:Array[Envelope,2]<<[
                Envelope{.text<<String::from("first");},
                Envelope{.text<<String::from("second");}
            ];
            let events:Array[Event,2]<<[Event::Text[String::from("active")],Event::End];
            println("done");
        }'''
        self.execute(text, "done\n", drops=["first", "second", "active"])
        self.execute(text, "done\n", sanitize=True)

    def test_loop_break_and_continue_clean_whole_local_arrays(self):
        text = '''fn main()->Unit {
            let[mut] index:i32=0;
            while index<3 {
                let values:Array[String,2]<<[String::from("a"),String::from("b")];
                index=index+1;
                if index<3 {continue;}
                break;
            }
            println("done");
        }'''
        self.execute(text, "done\n", drops=["a", "b"] * 3)

    def test_later_argument_return_cleans_previously_built_array(self):
        text = '''fn receive(values:Array[String,2],marker:i32)->Unit{println("not called");}
        fn early()->i32 {
            receive([String::from("a"),String::from("b")],{return 42;0});
            0
        }
        fn main()->Unit{println("{}",early());}'''
        self.execute(text, "42\n", drops=["a", "b"])

    def test_resource_array_is_one_explicit_generic_argument(self):
        text = '''fn[T] identity(value:T)->T{value}
        fn main()->Unit {
            let values:Array[String,2]<<identity[Array[String,2]]([String::from("a"),String::from("b")]);
            println("done");
        }'''
        self.execute(text, "done\n", drops=["a", "b"])

    def test_copy_and_move_rules_are_not_bypassed_by_array_aliases(self):
        prefix = 'type Pair=Array[String,2];fn consume(value:Pair)->Unit{unit}fn main()->Unit{'
        initial = 'let values:Pair<<[String::from("a"),String::from("b")];'
        self.rejected(prefix + initial + 'let copied:Pair=values;}', "XE-OWN-0001")
        self.rejected(prefix + initial + 'let moved:Pair<<values;consume(values);}', "XE-MOVE-0001")
        self.rejected(prefix + initial + 'values<<[String::from("c"),String::from("d")];}', "XE-MUT-0001")

    def test_nonowning_array_pointer_cannot_transfer_target_ownership(self):
        self.rejected('fn take(values:Array[String,2]@)->Array[String,2]{values#}', "XE-MOVE-0002")

    def test_resource_element_index_and_view_capabilities_stay_closed(self):
        expressions = ("values⟦0⟧", "values⟦0⟧@", "values⟨..⟩[@]", "values⟨..⟩[@[mut]]")
        for expression in expressions:
            with self.subTest(expression=expression):
                self.rejected('''fn main()->Unit {
                    let[mut] values:Array[String,2]<<[String::from("a"),String::from("b")];
                ''' + expression + ';}', "XE-CORE-0002")

    def test_generic_array_template_does_not_gain_unreviewed_element_capabilities(self):
        self.rejected('fn[T] relay(values:Array[T,2])->Array[T,2]{values}', "XE-CORE-0002")

    def test_copy_scalar_array_behavior_is_preserved(self):
        self.execute('''fn main()->Unit {
            let values:Array[i32,2]=[1,2];let copied:Array[i32,2]=values;
            println("{} {}",values⟦0⟧,copied⟦1⟧);
        }''', "1 2\n")

    def test_resource_array_paths_under_asan_ubsan_and_default_lsan(self):
        for text, expected in ((WHOLE, "consume\nafter\n"), (PARTIAL, "done\n"), (REPLACE, "done\n")):
            with self.subTest(text=text):
                self.execute(text, expected, sanitize=True)

    def test_documentation_and_standalone_example_execute_real_core_programs(self):
        page = private_document("docs/language/resource-arrays.md")
        blocks = [code for info, _, code in fenced_blocks(page.read_text(encoding="utf-8"))
                  if info == "xe-core"]
        self.assertEqual(len(blocks), 1)
        self.execute(blocks[0], "pair\npair\ndone\n")
        self.execute((ROOT / "examples/core/resource-arrays.xe").read_text(encoding="utf-8"),
                     "pair\npair\ndone\n")


if __name__ == "__main__":
    unittest.main()
