"""Formatting uses ordinary ordered value arguments and pointer warnings.

All resource-alias counterexamples stop at checking/C emission. The only
executed fixture keeps every pointer target alive and owns distinct strings.
"""
import copy
import sys
import unittest

from compiler.tests import test_core_struct_backend as struct_backend
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend, lower_to_c
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import check_source
from compiler.xe_ast.source import Diagnostic, Source


class CoreFormattedArgumentTests(unittest.TestCase):
    def checked(self, text, warning):
        source = Source(text, "formatted-arguments.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        self.assertEqual(checker.check(), [])
        self.assertIn(warning, {w.code for w in checker.warnings})
        self.assertEqual(tree, original)
        warnings = []
        lower_to_c(text, source.filename, warnings=warnings)
        self.assertIn(warning, {w.code for w in warnings})
        return checker

    def test_external_pointer_array_slot_keeps_the_same_unknown_source_warning(self):
        # Static/C only: owner is freed before clone reads the stored pointer.
        # The Array box's lifetime does not identify its elements' targets.
        text = '''fn discard(value:String)->Unit{unit}
        fn show(pointers:Array[String@,1]@,owner:String)->Unit {
            discard(owner);
            let copy:String<<pointers#⟦0⟧.clone();
            println("{}",copy);
        }
        fn main()->Unit {
            let text:String<<String::from("not executed");
            let pointers:Array[String@,1]=[text@];
            show(pointers@,text);
        }'''
        self.checked(text, "XE-PTR-0003")

    def test_second_resource_argument_cannot_read_a_resource_already_transferred(self):
        for second in ("text", "(text)", "{text}"):
            text = ('fn main()->Unit{let text:String<<String::from("not executed");'
                    'println("{} {}",text,' + second + ');}')
            with self.subTest(second=second):
                errors = check_source(text)
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-MOVE-0001")

    def test_direct_ast_backend_cannot_lower_duplicate_resource_arguments(self):
        text = '''fn main()->Unit{let text:String<<String::from("not executed");
            println("{} {}",text,text);
        }'''
        source = Source(text)
        tree = parse_source(text)
        checker = AnnotatedChecker(source, tree)
        self.assertEqual(checker.check()[0].code, "XE-MOVE-0001")
        with self.assertRaises(Diagnostic) as raised:
            CBackend(checker).generate()
        self.assertEqual(raised.exception.code, "XE-MOVE-0001")

    def test_printing_unknown_external_pointer_field_performs_value_read_refinement(self):
        text = '''struct Holder{p:String@,}impl Copy for Holder{}
        fn discard(owner:String)->Unit{unit}
        impl Holder{
            fn show(self:Self@,owner:String)->Unit{
                discard(owner);
                println("{}",self.p);
            }
        }
        fn main()->Unit{
            let text:String<<String::from("not executed");
            Holder{.p=text@;}.show(text);
        }'''
        self.checked(text, "XE-PTR-0003")

    def test_earlier_pointer_is_pending_while_later_resource_argument_moves_its_target(self):
        self.checked('''fn main()->Unit{
            let text:String<<String::from("not executed");
            println("{} {}",text@,text);
        }''', "XE-PTR-0002")

    def test_later_argument_block_can_invalidate_an_earlier_pointer(self):
        self.checked('''fn discard(owner:String)->Unit{unit}
        fn main()->Unit{
            let text:String<<String::from("not executed");
            println("{} {}",text@,{discard(text);let number:i32=0;number});
        }''', "XE-PTR-0002")

    def test_nested_formatted_call_keeps_outer_pending_pointer_dependencies(self):
        self.checked('''fn main()->Unit{
            let text:String<<String::from("not executed");
            println("{} {}",text@,{println("{}",text);let number:i32=0;number});
        }''', "XE-PTR-0002")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan/default LSan execution requires Linux")
    def test_safe_distinct_owners_and_known_pointer_fields_execute_without_warnings(self):
        text = '''struct Holder{p:String@,}impl Copy for Holder{}
        fn main()->Unit{
            let first:String<<String::from("first");
            let second:String<<String::from("second");
            let one:Holder=Holder{.p=first@;};let two:Holder=Holder{.p=second@;};
            let one_pointer:Holder@=one@;
            println("{} {}",one_pointer.p,two.p);
            let copy_one:String<<String::from("copy one");
            let copy_two:String<<String::from("copy two");
            println("{} {}",copy_one,copy_two);
        }'''
        struct_backend.CoreStructBackendTests().execute(text, "first second\ncopy one copy two\n", sanitize=True)


if __name__ == "__main__":
    unittest.main()
