"""Confirmed core structs and receivers through the public C/build/run path.

These are ordinary owned resources and valid pointers. Cleanup tracing changes
only a test copy of the runtime; a separate sanitizer case executes unmodified
generated C with ASan, UBSan and default leak detection intact.
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

from compiler.tests.documentation_support import private_document
from compiler.tests.test_core_enum_backend import DROP_HEAD, DROP_TRACE
from compiler.tests.test_repository import fenced_blocks
from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend, identifier
from compiler.xe_ast.build import build_executable, compile_generated
from compiler.xe_ast.language_policy import current_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Source

CC = shutil.which(os.environ.get("CC", "gcc"))
ROOT = Path(__file__).resolve().parents[2]
POINT = '''struct Point {x:i32,y:i32,}
impl Copy for Point {}
impl Point {
    fn origin()->Self {Self{.x=0;.y=0;}}
    fn sum(self:Self@)->i32 {self.x+self.y}
    fn shift(self:Self@[mut],amount:i32)->Unit {
        self.x=self.x+amount;
        self.y=self.y+amount;
    }
    fn by_value(self:Self)->i32 {self.x+self.y}
}
'''
PAIR = '''struct Pair {left:String,right:String,}
fn pair(left:str,right:str)->Pair {
    Pair{.left<<String::from(left);.right<<String::from(right);}
}
'''


class CoreStructBackendTests(unittest.TestCase):
    def generated(self, text):
        self.assertEqual(current_profile(), "core")
        source = Source(text, "core-struct.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        exported = json.loads(json.dumps(document(source, tree), ensure_ascii=False))
        self.assertEqual(exported["schema_version"], 1)
        checker = AnnotatedChecker(source, exported["ast"])
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(error.render() for error in diagnostics))
        self.assertEqual(checker.warnings, [])
        generated = CBackend(checker).generate()
        self.assertEqual(tree, original)
        self.assertEqual(exported["ast"], original)
        return generated

    def execute(self, text, expected, *, trace=False, sanitize=False):
        generated = self.generated(text)
        if CC is None:
            self.skipTest("core struct execution needs a system C compiler")
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory(prefix="xe-core-struct-") as directory:
            root = Path(directory)
            source, program = root / "main.xe", root / "program"
            source.write_text(text, encoding="utf-8")
            if trace:
                self.assertEqual(generated.count(DROP_HEAD), 1)
                generated = generated.replace(DROP_HEAD, DROP_HEAD + DROP_TRACE, 1)
                compile_generated(generated, root / "traced.c", program, cc=CC, extra_flags=flags)
            else:
                build_executable(source, program, cc=CC, extra_flags=flags)
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ""))

    def test_copy_struct_methods_associated_call_and_pointer_receiver(self):
        self.execute(POINT + '''fn main()->Unit {
            let[mut] first:Point=Point{.x=20;.y=22;};
            let copied:Point=first;
            let pointer:Point@[mut]=first@[mut];
            pointer.shift(1);
            let zero:Point=Point::origin();
            println("{} {} {} {} {}",copied.sum(),first.sum(),pointer.x,pointer.by_value(),zero.sum());
            println("{}",first.sum());
        }''', "42 44 21 44 0\n44\n")

    def test_explicit_copy_move_and_noncopy_value_receiver(self):
        self.execute('''struct Ticket {number:i32,}
        impl Ticket {
            fn finish(self:Self)->i32 {self.number}
        }
        fn main()->Unit {
            let ticket:Ticket<<Ticket{.number=7;};
            println("{}",ticket.finish());
            let source:i32=42;
            let moved:i32<<source;
            println("{}",moved);
        }''', "7\n42\n")

    def test_copy_value_receiver_snapshots_before_later_argument_writes(self):
        self.execute('''struct Counter {number:i32,}
        impl Copy for Counter {}
        impl Counter {
            fn owned(self:Self,ticket:Unit)->i32{self.number}
            fn pointer(self:Self@,ticket:Unit)->i32{self.number}
        }
        fn main()->Unit {
            let[mut] value:Counter=Counter{.number=10;};
            let copied:i32=value.owned({value.number=20;unit});
            let pointed:i32=value.pointer({value.number=30;unit});
            println("{} {} {}",copied,pointed,value.number);
        }''', "10 30 30\n")

    def test_receiver_resource_is_cleaned_when_later_argument_returns(self):
        self.execute('''struct Message {text:String,}
        impl Message {
            fn finish(self:Self,ticket:Unit)->Unit{println("not called");}
        }
        fn early()->i32 {
            let value:Message<<Message{.text<<String::from("receiver");};
            value.finish({return 42;});
            0
        }
        fn main()->Unit{println("{}",early());}''', "drop:receiver\n42\n", trace=True)

    def test_associated_alias_call_preserves_the_canonical_layout(self):
        self.execute(POINT + '''type Coordinate=Point;
        fn main()->Unit {
            let value:Coordinate=Coordinate::origin();
            println("{}",value.sum());
        }''', "0\n")

    def test_language_chapter_has_one_runnable_core_example(self):
        page = private_document("docs/language/05-structs-and-methods.md")
        blocks = [code for info, _, code in fenced_blocks(page.read_text(encoding="utf-8")) if info == "xe-core"]
        self.assertEqual(len(blocks), 1, "do not silently pass with zero documentation programs")
        self.execute(blocks[0], "42 44 21\n")

    def test_standalone_struct_example(self):
        self.execute((ROOT / "examples/core/structs.xe").read_text(encoding="utf-8"),
                     "field 22\nfield 20\n42 44 21\nleft right\nfinish left right\ndone\n")

    def test_field_initializers_execute_in_source_order(self):
        self.execute('''struct Point {x:i32,y:i32,}
        impl Copy for Point {}
        fn mark(value:i32)->i32{println("mark {}",value);value}
        fn main()->Unit {
            let point:Point=Point{.y=mark(2);.x=mark(1);};
            println("{} {}",point.x,point.y);
        }''', "mark 2\nmark 1\n1 2\n")

    def test_resource_fields_clean_in_declaration_order_after_source_order_construction(self):
        # The injected trace records this backend's current traversal. It does
        # not define a new source contract for user Drop hooks (not admitted).
        self.execute('''struct Pair {left:String,right:String,}
        fn text(label:str)->String{println("build {}",label);String::from(label)}
        fn main()->Unit {
            let value:Pair<<Pair{.right<<text("right");.left<<text("left");};
            println("built");
        }''', "build right\nbuild left\nbuilt\ndrop:left\ndrop:right\n", trace=True)

    def test_nested_structs_and_active_enum_payload_clean_recursively(self):
        self.execute('''enum Event {Text[String],Empty,}
        struct Inner {event:Event,}
        struct Outer {head:String,inner:Inner,tail:String,}
        fn main()->Unit {
            let value:Outer<<Outer {
                .head<<String::from("head");
                .inner<<Inner{.event<<Event::Text[String::from("event")];};
                .tail<<String::from("tail");
            };
            println("built");
        }''', "built\ndrop:head\ndrop:event\ndrop:tail\n", trace=True)

    def test_incomplete_constructor_cleans_only_already_initialized_fields(self):
        self.execute(PAIR + '''fn early()->Unit {
            let prior:String<<String::from("prior");
            let value:Pair<<Pair{
                .left<<String::from("left");
                .right<<{return;};
            };
        }
        fn first()->Unit {
            let value:Pair<<Pair{
                .left<<{return;};
                .right<<String::from("not constructed");
            };
        }
        fn main()->Unit {early();first();println("done");}''',
                     "drop:left\ndrop:prior\ndone\n", trace=True)

    def test_nested_incomplete_constructor_releases_nested_and_prior_fields(self):
        self.execute('''struct Inner {first:String,second:String,}
        struct Outer {first:String,inner:Inner,}
        fn early()->Unit {
            let value:Outer<<Outer{
                .first<<String::from("outer first");
                .inner<<Inner{
                    .first<<String::from("inner first");
                    .second<<{return;};
                };
            };
        }
        fn main()->Unit{early();println("done");}''',
                     "drop:inner first\ndrop:outer first\ndone\n", trace=True)

    def test_later_argument_return_cleans_complete_struct_before_call_begins(self):
        self.execute(PAIR + '''fn receive(value:Pair,ticket:Unit)->Unit {println("not called");}
        fn early()->i32 {
            receive(pair("left","right"),{return 42;});
            0
        }
        fn main()->Unit{println("{}",early());}''',
                     "drop:left\ndrop:right\n42\n", trace=True)

    def test_whole_struct_move_parameter_return_and_pipe_share_cleanup(self):
        self.execute(PAIR + '''fn relay(value:Pair)->Pair{value}
        fn main()->Unit {
            let source:Pair<<pair("left","right");
            let moved:Pair<<source;
            let returned:Pair<<relay(moved);
            let piped:Pair<<returned |> relay;
            println("received");
        }''', "received\ndrop:left\ndrop:right\n", trace=True)

    def test_noncopy_receiver_by_value_drops_its_fields(self):
        self.execute('''struct Message {text:String,}
        impl Message {
            fn show(self:Self@)->Unit {println("{}",self.text@);}
            fn finish(self:Self)->Unit {println("finish {}",self.text@);}
        }
        fn main()->Unit {
            let value:Message<<Message{.text<<String::from("message");};
            value.show();
            value.finish();
        }''', "message\nfinish message\ndrop:message\n", trace=True)

    def test_resource_replacement_uses_rhs_then_cleans_current_old_value(self):
        self.execute(PAIR + '''fn consume(value:Pair)->Unit{unit}
        fn main()->Unit {
            let[mut] value:Pair<<pair("old left","old right");
            value<<Pair{.left<<value.left.clone();.right<<value.right.clone();};
            value<<{consume(value);pair("new left","new right")};
            println("replaced");
        }''', "drop:old left\ndrop:old right\ndrop:old left\ndrop:old right\n"
                     "replaced\ndrop:new left\ndrop:new right\n", trace=True)

    def test_deferred_conditional_struct_initialization_drops_actual_path_only(self):
        self.execute(PAIR + '''fn maybe(ready:bool)->Unit {
            let value:Pair;
            if ready{value<<pair("left","right");}
        }
        fn main()->Unit{maybe(false);println("empty");maybe(true);println("done");}''',
                     "empty\ndrop:left\ndrop:right\ndone\n", trace=True)

    def test_forward_value_layout_and_recursive_pointer_layouts_compile(self):
        text = '''struct Outer {inner:Inner,}
        impl Copy for Outer {}
        struct Inner {number:i32,}
        impl Copy for Inner {}
        struct Node {next:Node@,number:i32,}
        struct Left {right:Right@,}
        struct Right {left:Left@,}
        fn number(pointer:Node@)->i32{pointer.number}
        fn main()->Unit {
            let value:Outer=Outer{.inner=Inner{.number=42;};};
            println("{}",value.inner.number);
        }'''
        generated = self.generated(text)
        # The self/mutual pointers are declarations, never invalid runtime
        # pointer instances. C needs the typedefs before complete definitions.
        name = identifier("type_Outer")
        self.assertLess(generated.index(f"typedef struct {name} {name};"),
                        generated.index(f"struct {name} {{"))
        self.execute(text, "42\n")

    def test_explicit_copy_struct_array_and_original_coordinate_view(self):
        self.execute(POINT + '''fn main()->Unit {
            let point:Point=Point{.x=20;.y=22;};
            let[mut] points:Array[Point,3]=[point,point,point];
            let view:View[Point@[mut]]=points⟨1..3⟩[@[mut]];
            view⟦2⟧.shift(1);
            let selected:Point=points⟦2⟧;
            println("{} {}",points⟦1⟧.sum(),selected.sum());
        }''', "42 44\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "sanitizer no-pie test requires Linux")
    def test_unmodified_struct_resource_paths_under_sanitizers(self):
        self.execute(PAIR + '''fn consume(value:Pair)->Unit{unit}
        fn relay(value:Pair)->Pair{value}
        fn early()->Unit {
            let value:Pair<<Pair{.left<<String::from("partial");.right<<{return;};};
        }
        fn maybe(ready:bool)->Unit {
            let value:Pair;if ready{value<<pair("conditional left","conditional right");}
        }
        fn main()->Unit {
            early();maybe(false);maybe(true);
            let[mut] value:Pair<<pair("old left","old right");
            value<<Pair{.left<<value.left.clone();.right<<value.right.clone();};
            value<<{consume(value);pair("new left","new right")};
            let moved:Pair<<relay(value);
            consume(moved);
            println("clean");
        }''', "clean\n", sanitize=True)


if __name__ == "__main__":
    unittest.main()
