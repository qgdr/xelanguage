"""Nominal core contracts and conservative field-address provenance.

Pointer escape counterexamples are checked only. Actual executable resource
cleanup is tested independently by test_core_struct_backend with default LSan.
"""
import copy
import sys
import unittest

from compiler.tests import test_core_struct_backend as struct_backend
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import check_source
from compiler.xe_ast.source import Diagnostic, Source

POINT = '''struct Point {x:i32,y:i32,}
impl Copy for Point {}
impl Point {
    fn sum(self:Self@)->i32{self.x+self.y}
    fn edit(self:Self@[mut])->Unit{self.x=42;}
    fn finish(self:Self)->i32{self.x+self.y}
}
'''
HOLDER = "struct Holder {p:i32@,}"


class CoreStructSemanticTests(unittest.TestCase):
    def checked(self, text, *, warnings=()):
        source = Source(text, "core-struct-semantic.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(e.render() for e in errors))
        self.assertEqual(set(w.code for w in checker.warnings), set(warnings))
        self.assertEqual(tree, original)
        return checker

    def rejected(self, text, code):
        errors = check_source(text, "core-struct-semantic.xe")
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())
        return errors[0]

    def test_copy_is_explicit_and_requires_copy_fields(self):
        self.checked(POINT + '''fn main()->Unit{
            let first:Point=Point{.x=20;.y=22;};let second:Point=first;
            println("{} {}",first.sum(),second.sum());
        }''')
        self.rejected('''struct Point{x:i32,}fn main()->Unit{
            let first:Point<<Point{.x=42;};let second:Point=first;
        }''', "XE-OWN-0001")
        self.rejected("struct Text{text:String,}impl Copy for Text{}fn main()->Unit{}", "XE-OWN-0001")

    def test_nominal_identity_is_not_structural_equality(self):
        self.rejected('''struct A{x:i32,}struct B{x:i32,}
        impl Copy for A{}impl Copy for B{}
        fn main()->Unit{let a:A=A{.x=1;};let b:B=a;}''', "XE-TYPE-0001")
        self.checked('''type Alias=Point;''' + POINT + '''fn main()->Unit{
            let first:Alias=Point{.x=20;.y=22;};let second:Point=first;
        }''')

    def test_copy_receiver_through_pointer_keeps_owner_usable(self):
        self.checked(POINT + '''fn main()->Unit {
            let point:Point=Point{.x=20;.y=22;};let pointer:Point@=point@;
            let result:i32=pointer.finish();println("{} {}",result,point.sum());
        }''')

    def test_copy_pointer_receiver_matches_explicit_dereference_argument_provenance(self):
        declarations = '''struct Holder{p:String@,}impl Copy for Holder{}
        fn discard(owner:String)->Unit{unit}
        impl Holder{fn show(self:Self,owner:String)->Unit{
            discard(owner);let copy:String<<self.p.clone();println("{}",copy);
        }}
        '''
        for receiver, invocation in (
                ("hp", "hp.show(text)"),
                ("hp", "Holder::show(hp#,text)"),
                ("view⟦0⟧", "view⟦0⟧.show(text)"),
                ("view⟦0⟧", "Holder::show(view⟦0⟧#,text)")):
            with self.subTest(receiver=receiver, invocation=invocation):
                text = declarations + '''fn main()->Unit{
                    let text:String<<String::from("not executed");
                    let holder:Holder=Holder{.p=text@;};
                    let hp:Holder@=holder@;
                    let holders:Array[Holder,1]=[holder];
                    let view:View[Holder@]=holders⟨..⟩[@];
                    ''' + invocation + ";}"
                checker = self.checked(text, warnings={"XE-PTR-0002"})
                self.assertIn("int main(", CBackend(checker).generate())
                # The pointee is intentionally invalidated: no execution.

    def test_external_copy_receiver_and_explicit_argument_share_unknown_origin_risk(self):
        declarations = '''struct Holder{p:String@,}impl Copy for Holder{}
        impl Holder{fn show(self:Self)->Unit{let copy:String<<self.p.clone();println("{}",copy);}}
        '''
        for type_ in ("Holder@", "Holder@[unsafe]"):
            for invocation in ("pointer.show()", "Holder::show(pointer#)"):
                with self.subTest(type=type_, invocation=invocation):
                    self.checked(declarations + "fn external(pointer:" + type_ + ")->Unit{" +
                                 invocation + ";}fn main()->Unit{}", warnings={"XE-PTR-0003"})

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan/default LSan execution requires Linux")
    def test_safe_copy_receiver_commutes_with_explicit_dereference_at_runtime(self):
        text = '''struct Holder{p:String@,}impl Copy for Holder{}
        fn discard(owner:String)->Unit{unit}
        impl Holder{fn show(self:Self,owner:String)->Unit{
            discard(owner);let copy:String<<self.p.clone();println("{}",copy);
        }}
        fn main()->Unit{
            let kept:String<<String::from("kept");
            let holder:Holder=Holder{.p=kept@;};let pointer:Holder@=holder@;
            pointer.show(String::from("first independent owner"));
            Holder::show(pointer#,String::from("second independent owner"));
            println("{}",kept@);
        }'''
        struct_backend.CoreStructBackendTests().execute(text, "kept\nkept\nkept\n", sanitize=True)

    def test_noncopy_receiver_cannot_obtain_ownership_through_pointer(self):
        self.rejected('''struct Message{text:String,}
        impl Message{fn finish(self:Self)->Unit{unit}}
        fn main()->Unit{let message:Message<<Message{.text<<String::from("text");};
            let pointer:Message@=message@;pointer.finish();}''', "XE-MOVE-0002")

    def test_mutable_receiver_and_field_write_require_actual_access_permission(self):
        self.rejected(POINT + "fn main()->Unit{let point:Point=Point{.x=1;.y=2;};point.edit();}", "XE-BORROW-0004")
        self.rejected(POINT + '''fn main()->Unit{let[mut] point:Point=Point{.x=1;.y=2;};
            let pointer:Point@=point@;pointer.x=3;}''', "XE-MUT-0001")
        self.checked(POINT + '''fn main()->Unit{let[mut] point:Point=Point{.x=1;.y=2;};
            let pointer:Point@[mut]=point@[mut];pointer.edit();pointer.y=0;}
        ''')

    def test_field_shorthand_dereferences_exactly_one_pointer_layer(self):
        self.checked(POINT + '''fn value(pointer:Point@)->i32{pointer.x}
        fn explicit(pointer:Point@@)->i32{pointer#.x}
        fn main()->Unit{}''', warnings={"XE-PTR-0003"})
        self.rejected(POINT + "fn value(pointer:Point@@)->i32{pointer.x}fn main()->Unit{}", "XE-TYPE-0001")

    def test_forward_layouts_self_pointers_and_aliases_keep_finite_storage(self):
        self.checked('''type Alias=Outer;
        struct Outer{inner:Inner,}struct Inner{number:i32,}
        impl Copy for Outer{}impl Copy for Inner{}
        struct Node{next:Node@,}struct Left{right:Right@,}struct Right{left:Left@,}
        fn main()->Unit{let value:Alias=Outer{.inner=Inner{.number=42;};};}
        ''')
        for declarations in ("struct Bad{next:Bad,}", "struct A{b:B,}struct B{a:A,}"):
            with self.subTest(declarations=declarations):
                self.rejected(declarations + "fn main()->Unit{}", "XE-TYPE-0006")

    def test_field_copy_quantity_does_not_depend_on_the_local_container_box(self):
        self.checked(HOLDER + '''fn main()->Unit {
            let number:i32=42;
            let pointer:i32@={
                let holder:Holder<<Holder{.p=number@;};
                let holder_pointer:Holder@=holder@;
                holder_pointer.p
            };
            println("{}",pointer#);
        }''')

    def test_field_address_still_depends_on_the_local_container_box(self):
        self.rejected(HOLDER + '''fn main()->Unit {
            let number:i32=42;
            let pointer:i32@@={let holder:Holder<<Holder{.p=number@;};holder.p@};
        }''', "XE-PTR-0004")

    def test_external_pointer_field_read_is_a_visible_unknown_origin_risk(self):
        checker = self.checked(HOLDER + '''fn load(pointer:Holder@)->i32@{pointer.p}
        fn main()->Unit{}''', warnings={"XE-PTR-0003"})
        self.assertTrue(checker.functions["load"].unsafe_result)

    def test_method_self_address_tracks_receiver_box_not_its_pointer_field_targets(self):
        declarations = HOLDER + '''impl Copy for Holder{}
        impl Holder{fn addr(self:Self@)->Self@{self}}
        '''
        self.rejected(declarations + '''fn bad(pointer:i32@)->Holder@{
            let holder:Holder=Holder{.p=pointer;};holder.addr()
        }''', "XE-PTR-0004")
        self.checked(declarations + '''fn main()->Unit{
            let number:i32=42;let holder:Holder=Holder{.p=number@;};
            let pointer:Holder@=holder.addr();println("{}",pointer.p#);
        }''')

    def test_anonymous_method_receiver_cannot_export_its_own_address(self):
        declarations = HOLDER + '''impl Copy for Holder{}
        impl Holder{fn addr(self:Self@)->Self@{self}}
        fn make(pointer:i32@)->Holder{Holder{.p=pointer;}}
        '''
        for receiver in ("Holder{.p=pointer;}", "make(pointer)"):
            with self.subTest(receiver=receiver):
                self.rejected(declarations + "fn bad(pointer:i32@)->Holder@{" + receiver + ".addr()}",
                              "XE-CORE-0002")

    def test_anonymous_receiver_independent_results_do_not_gain_a_storage_dependency(self):
        self.checked(HOLDER + '''impl Holder{
            fn answer(self:Self@)->i32{42}
            fn owned(self:Self@)->String{String::from("independent")}
            fn keep(self:Self@,pointer:i32@)->i32@{pointer}
        }
        fn main()->Unit {
            let number:i32=42;
            let answer:i32=Holder{.p=number@;}.answer();
            let text:String<<Holder{.p=number@;}.owned();
            let pointer:i32@=Holder{.p=number@;}.keep(number@);
            println("{} {} {}",answer,text@,pointer#);
        }''')

    def test_pointer_receiver_method_does_not_change_external_target_identity(self):
        self.checked(HOLDER + '''impl Copy for Holder{}
        impl Holder{fn addr(self:Self@)->Self@{self}}
        fn identity(pointer:Holder@)->Holder@{pointer.addr()}
        fn main()->Unit{
            let number:i32=42;let holder:Holder=Holder{.p=number@;};
            let pointer:Holder@=identity(holder@);println("{}",pointer.p#);
        }''')

    def test_method_pointer_field_getter_keeps_conservative_unknown_origin_warning(self):
        checker = self.checked(HOLDER + '''impl Holder{
            fn get(self:Self@)->i32@{self.p}
        }
        fn external(pointer:Holder@)->i32@{pointer.get()}
        fn main()->Unit{}
        ''', warnings={"XE-PTR-0003"})
        self.assertTrue(checker.methods["Holder", "get"].unsafe_result)
        self.assertTrue(checker.functions["external"].unsafe_result)

    def test_method_call_through_unknown_external_pointer_field_reads_the_pointer_quantity(self):
        text = '''struct Holder{p:String@,}impl Copy for Holder{}
        fn discard(owner:String)->Unit{unit}
        impl Holder{
            fn show(self:Self@,owner:String)->Unit{
                discard(owner);
                let copy:String<<self.p.clone();
                println("{}",copy);
            }
        }
        fn main()->Unit{
            let text:String<<String::from("not executed");
            Holder{.p=text@;}.show(text);
        }'''
        self.checked(text, warnings={"XE-PTR-0003"})
        # Deliberate resource-alias invalidation: static checking only.

    def test_multiple_field_shorthands_follow_stored_pointer_targets(self):
        declarations = '''struct Node{number:i32,}impl Copy for Node{}
        struct Wrapper{next:Node@,}impl Copy for Wrapper{}
        impl Node{fn address(self:Self@)->i32@{self.number@}}
        '''
        self.checked(declarations + '''fn main()->Unit{
            let node:Node=Node{.number=42;};
            let direct:i32@={
                let wrapper:Wrapper=Wrapper{.next=node@;};
                let pointer:Wrapper@=wrapper@;
                pointer.next.number@
            };
            let called:i32@={
                let wrapper:Wrapper=Wrapper{.next=node@;};
                let pointer:Wrapper@=wrapper@;
                pointer.next.address()
            };
            println("{} {}",direct#,called#);
        }''')
        error = self.rejected(declarations + '''fn bad()->i32@{
            let node:Node=Node{.number=42;};
            let wrapper:Wrapper=Wrapper{.next=node@;};
            let pointer:Wrapper@=wrapper@;
            pointer.next.number@
        }''', "XE-PTR-0004")
        self.assertIn("node", error.message)

    def test_taking_stored_pointer_field_address_keeps_the_containing_box_lifetime(self):
        self.rejected('''struct Node{number:i32,}impl Copy for Node{}
        struct Wrapper{next:Node@,}impl Copy for Wrapper{}
        fn main()->Unit{
            let node:Node=Node{.number=42;};
            let pointer:Node@@={
                let wrapper:Wrapper=Wrapper{.next=node@;};
                let receiver:Wrapper@=wrapper@;
                receiver.next@
            };
        }''', "XE-PTR-0004")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan/default LSan execution requires Linux")
    def test_safe_local_pointer_field_method_and_multilevel_field_access_execute(self):
        text = '''struct Node{number:i32,}impl Copy for Node{}
        struct Wrapper{next:Node@,}impl Copy for Wrapper{}
        struct Text{text:String@,}impl Copy for Text{}
        impl Node{fn address(self:Self@)->i32@{self.number@}}
        fn main()->Unit{
            let node:Node=Node{.number=42;};
            let pointer:i32@={
                let wrapper:Wrapper=Wrapper{.next=node@;};
                let receiver:Wrapper@=wrapper@;
                receiver.next.address()
            };
            let wrapper:Wrapper=Wrapper{.next=node@;};
            let receiver:Wrapper@=wrapper@;
            let original:String<<String::from("alive");
            let text:Text=Text{.text=original@;};
            let borrowed:Text@=text@;
            let copy:String<<borrowed.text.clone();
            println("{} {} {}",pointer#,receiver.next.number,copy);
        }'''
        struct_backend.CoreStructBackendTests().execute(text, "42 42 alive\n", sanitize=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan/default LSan execution requires Linux")
    def test_named_receiver_address_and_independent_temporary_results_execute_safely(self):
        text = HOLDER + '''impl Copy for Holder{}
        impl Holder{
            fn addr(self:Self@)->Self@{self}
            fn answer(self:Self@)->i32{42}
            fn owned(self:Self@)->String{String::from("independent")}
            fn keep(self:Self@,pointer:i32@)->i32@{pointer}
        }
        fn identity(pointer:Holder@)->Holder@{pointer.addr()}
        fn main()->Unit{
            let number:i32=42;let holder:Holder=Holder{.p=number@;};
            let receiver:Holder@=identity(holder@);
            let answer:i32=Holder{.p=number@;}.answer();
            let text:String<<Holder{.p=number@;}.owned();
            let independent:i32@=Holder{.p=number@;}.keep(number@);
            println("{} {} {} {}",receiver.p#,answer,text@,independent#);
        }'''
        struct_backend.CoreStructBackendTests().execute(text, "42 42 independent 42\n", sanitize=True)

    def test_field_assignment_does_not_erase_other_field_address_dependencies(self):
        self.rejected('''struct Pair{first:i32@,second:i32@,}
        fn main()->Unit {
            let number:i32=0;
            let[mut] escaped:Pair<<Pair{.first=number@;.second=number@;};
            {
                let short:i32=1;
                let[mut] inside:Pair<<Pair{.first=short@;.second=number@;};
                inside.second=number@;
                escaped<<inside;
            }
        }''', "XE-PTR-0004")

    def test_indirect_joined_field_write_checks_every_possible_destination_lifetime(self):
        self.rejected('''struct Pair{first:i32@,second:i32@,}
        fn choose(ready:bool)->Unit {
            let number:i32=0;
            let[mut] outside:Pair<<Pair{.first=number@;.second=number@;};
            {
                let short:i32=1;
                let[mut] inside:Pair<<Pair{.first=number@;.second=number@;};
                let pointer:Pair@[mut]=if ready{inside@[mut]}else{outside@[mut]};
                pointer.first=short@;
            }
        }fn main()->Unit{}''', "XE-PTR-0004")

    def test_unknown_external_pointer_field_write_stays_behind_retention_gate(self):
        self.rejected(HOLDER + '''fn store(holder:Holder@[mut],pointer:i32@)->Unit{holder.p=pointer;}
        fn main()->Unit{}''', "XE-CORE-0002")

    def test_constructor_both_explicit_field_transfer_directions_consume_copy_source(self):
        for field in (".x << source;", "source >> .x;"):
            with self.subTest(field=field):
                self.rejected('''struct Point{x:i32,}impl Copy for Point{}
                fn main()->Unit{let source:i32=42;let point:Point=Point{''' + field +
                              '''};println("{}",source);}''', "XE-MOVE-0001")
        self.checked('''struct Point{x:i32,}impl Copy for Point{}
        fn main()->Unit{let source:i32=42;let point:Point=Point{.x=source;};println("{}",source);}''')

    def test_partial_resource_field_moves_and_uninitialized_field_steps_stay_closed(self):
        for source in ("value.text", "(value.text)"):
            with self.subTest(source=source):
                self.rejected('''struct Message{text:String,}fn main()->Unit{
                    let value:Message<<Message{.text<<String::from("text");};
                    let text:String<<''' + source + ';}', "XE-CORE-0002")
        self.rejected("struct Point{x:i32,}fn main()->Unit{let point:Point;point.x=1;}", "XE-CORE-0002")

    def test_temporary_struct_resource_field_moves_do_not_bypass_partial_move_gate(self):
        declarations = '''struct Pair{left:String,right:String,}
        fn make()->Pair{Pair{.left<<String::from("a");.right<<String::from("b");}}
        '''
        for source in ("Pair{.left<<String::from(\"a\");.right<<String::from(\"b\");}.left",
                       "make().left", "(make().left)"):
            with self.subTest(source=source):
                self.rejected(declarations + "fn main()->Unit{let text:String<<" + source + ";}",
                              "XE-CORE-0002")

    def test_field_access_propagates_noncompletion_without_inventing_a_box(self):
        self.checked(POINT + '''fn main()->Unit {
            let point:Point=Point{.x=1;.y=2;};({return;point}).x;
        }''')

    def test_failed_struct_state_cannot_reach_direct_backend_generation(self):
        text = '''struct Value{number:i32,}fn main()->Unit{
            let value:Value<<Value{.number=42;};let moved:Value<<value;
            println("{}",value.number);
        }'''
        source = Source(text)
        checker = AnnotatedChecker(source, parse_source(text))
        errors = checker.check()
        self.assertTrue(errors)
        with self.assertRaises(Diagnostic):
            CBackend(checker).generate()


if __name__ == "__main__":
    unittest.main()
