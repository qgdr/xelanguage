"""Local box state is not the same fact as initialization history.

DEC-011/030: Copy uses of ordinary arguments remain copies; explicit transfer
invalidates a direct source binding. Drop follows actual initialized flags,
including paths on which construction never happened and RHS early exits.
No unsafe pointer fixture is executed by this module.
"""

import unittest

from compiler.tests import test_core_enum_backend as enum_backend
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import check_source
from compiler.xe_ast.source import Diagnostic, Source

CLEANUP = '''enum Event {Text[String],Empty,}
fn consume(value:String)->Unit {unit}
fn maybe(ready:bool)->Unit {
    let text:String;
    if ready {text<<String::from("conditional");}
}
fn early()->Unit {
    let[mut] text:String<<String::from("kept until return");
    text << {return;String::from("never built")};
}
fn replace()->Unit {
    let[mut] text:String<<String::from("old");
    text << text.clone();
    text << {consume(text);String::from("new")};
    consume(text);
    text << String::from("rebuilt");
    let[mut] event:Event<<Event::Text[String::from("enum old")];
    Event::Empty >> event;
    event << Event::Text[String::from("enum new")];
}
fn main()->Unit {
    maybe(false);
    maybe(true);
    early();
    replace();
    println("done");
}'''


class CoreBindingStateTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "core-binding-state.xe")
        checker = AnnotatedChecker(source, parse_source(text, source.filename))
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(d.render() for d in diagnostics))
        self.assertEqual(checker.warnings, [])
        return checker

    def rejected(self, text, code):
        diagnostics = check_source(text, "core-binding-state.xe")
        self.assertTrue(diagnostics, text)
        self.assertEqual(diagnostics[0].code, code, diagnostics[0].render())

    def execute(self, text, expected, *, trace=False, sanitize=False):
        helper = enum_backend.CoreEnumBackendTests()
        helper.execute(text, expected, trace=trace, sanitize=sanitize)

    def test_explicit_transfers_invalidate_copy_direct_sources_in_both_directions(self):
        for transfer in ("let moved:i32<<source;", "let moved:i32;source >> moved;"):
            text = 'fn main()->Unit{let source:i32=42;' + transfer + 'println("{}",source);}'
            with self.subTest(transfer=transfer):
                self.rejected(text, "XE-MOVE-0001")

    def test_copy_calls_and_pipes_keep_sources_and_fresh_transfers_do_not_recurse(self):
        text = '''fn identity(value:i32)->i32{value}
        fn main()->Unit {
            let source:i32=42;
            let called:i32=identity(source);
            let piped:i32=source |> identity;
            let fresh:i32<<identity(source);
            let literal:i32<<7;
            println("{} {} {} {} {}",source,called,piped,fresh,literal);
        }'''
        self.checked(text)
        self.execute(text, "42 42 42 42 7\n")

    def test_moving_pointer_binding_does_not_move_its_target_or_existing_copies(self):
        text = '''fn main()->Unit {
            let number:i32=42;
            let original:i32@=number@;
            let copied:i32@=original;
            let moved:i32@<<original;
            println("{} {} {}",number,copied#,moved#);
        }'''
        self.checked(text)
        self.execute(text, "42 42 42\n")

    def test_nonowning_copy_locations_do_not_acquire_explicit_move_rules(self):
        for source in ("pointer#", "values⟦0⟧"):
            with self.subTest(source=source):
                self.rejected('''fn main()->Unit {
                    let number:i32=42;
                    let pointer:i32@=number@;
                    let values:Array[i32,1]=[42];
                    let moved:i32<<''' + source + ';}', "XE-CORE-0002")
        for transfer in ("pointer# << 1;", "1 >> pointer#;", "values⟦0⟧ << 1;", "1 >> values⟦0⟧;"):
            with self.subTest(transfer=transfer):
                self.rejected('''fn main()->Unit {
                    let[mut] number:i32=0;let pointer:i32@[mut]=number@[mut];
                    let[mut] values:Array[i32,1]=[0];''' + transfer + '}', "XE-CORE-0002")

    def test_direct_self_transfer_and_grouped_reverse_form_are_errors_for_copy_too(self):
        for statement in ("number<<number;", "(number)<<((number));", "((number))>>(number);"):
            with self.subTest(statement=statement):
                self.rejected('fn main()->Unit{let[mut] number:i32=42;' + statement + '}', "XE-MOVE-0001")

    def test_mutable_copy_box_can_be_reinitialized_after_explicit_transfer(self):
        self.execute('''fn main()->Unit {
            let[mut] source:i32=1;
            let moved:i32<<source;
            source=2;
            println("{} {}",moved,source);
        }''', "1 2\n")
        self.rejected('''fn main()->Unit {
            let source:i32=1;let moved:i32<<source;source=2;
        }''', "XE-MUT-0001")

    def test_deferred_initialization_requires_every_reaching_read_path(self):
        text = '''fn select(ready:bool)->i32 {
            let number:i32;
            if ready {number=42;} else {return 0;}
            number
        }
        fn main()->Unit{println("{} {}",select(true),select(false));}'''
        self.checked(text)
        self.execute(text, "42 0\n")
        self.rejected('''fn select(ready:bool)->i32 {
            let number:i32;if ready{number=42;}number
        }fn main()->Unit{unit}''', "XE-INIT-0001")
        self.rejected('fn main()->Unit{let number:i32;println("{}",number);}', "XE-INIT-0001")

    def test_history_does_not_disappear_at_conditional_or_rhs_initialization(self):
        self.rejected('''fn maybe(ready:bool)->Unit {
            let number:i32;if ready{number=1;}number=2;
        }fn main()->Unit{unit}''', "XE-MUT-0001")
        self.rejected('fn main()->Unit{let number:i32;number={number=1;2};}', "XE-MUT-0001")
        self.checked('''fn maybe(ready:bool)->Unit {
            let number:i32;if ready{number=1;}
        }fn main()->Unit{unit}''')

    def test_unimplemented_loop_backedge_is_explicitly_closed(self):
        self.rejected('fn main()->Unit{let number:i32;while false{number=1;}}', "XE-CORE-0002")
        self.checked('fn main()->Unit{while false{let number:i32;number=1;}}')
        self.checked('fn main()->Unit{let[mut] number:i32;while false{number=1;}}')

    def test_resource_reconstruction_still_requires_mutable_binding(self):
        self.rejected('''fn consume(value:String)->Unit{unit}
        fn main()->Unit{let text:String<<String::from("old");consume(text);text<<String::from("new");}''',
                      "XE-MUT-0001")
        self.checked(CLEANUP)

    def test_actual_path_cleanup_and_replacement_order(self):
        self.execute(CLEANUP,
                     "drop:conditional\ndrop:kept until return\ndrop:old\ndrop:old\n"
                     "drop:new\ndrop:enum old\ndrop:enum new\ndrop:rebuilt\ndone\n",
                     trace=True)

    def test_actual_path_cleanup_under_asan_ubsan_default_lsan(self):
        self.execute(CLEANUP, "done\n", sanitize=True)

    def test_never_place_computation_propagates_through_address_taking(self):
        text = 'fn main()->Unit{let a:Array[i32,2]=[1,2];({return;a})⟦0⟧@;}'
        self.checked(text)
        self.execute(text, "")

    def test_direct_backend_cannot_lower_a_failed_moved_binding_check(self):
        text = 'fn main()->Unit{let source:i32=42;let moved:i32<<source;println("{}",source);}'
        source = Source(text, "bad-binding.xe")
        checker = AnnotatedChecker(source, parse_source(text, source.filename))
        self.assertEqual(checker.check()[0].code, "XE-MOVE-0001")
        with self.assertRaises(Diagnostic) as caught:
            CBackend(checker).generate()
        self.assertEqual(caught.exception.code, "XE-MOVE-0001")


if __name__ == "__main__":
    unittest.main()
