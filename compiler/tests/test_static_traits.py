"""Trait 契约、具体 Copy 资格和析构必须形成前端到执行的闭环。"""
import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source
from compiler.xe_ast.typesys import I32, STRING, Type

CC = shutil.which("cc") or ""

MEASURE = "trait Measure { fn measure(self: Self@) -> i32; } struct Point { value: i32, }"


class StaticTraitSemanticTests(unittest.TestCase):
    def checked(self, text):
        checker = Checker(Source(text, "traits.xe"), parse_source(text, "traits.xe"))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        return checker

    def rejected(self, text, code):
        errors = Checker(Source(text, "traits.xe"), parse_source(text, "traits.xe")).check()
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())
        return errors[0]

    def test_trait_bound_requires_real_implementation_not_method_duck_typing(self):
        text = MEASURE + "impl Point { fn measure(self:Self@)->i32{self.value} } "
        self.rejected(text + "fn[T:Measure] inspect(p:T@)->i32{p.measure()} fn main(){let p<<Point{.value=42;};inspect(p@);}",
                      "XE-TRAIT-0001")

    def test_trait_bound_and_method_dispatch_share_concrete_instance(self):
        text = MEASURE + "impl Measure for Point { fn measure(self:Self@)->i32{self.value} } "
        checker = self.checked(text + "fn[T:Measure] inspect(p:T@)->i32{p.measure()} fn main(){let p<<Point{.value=42;};inspect(p@);inspect(p@);}")
        self.assertEqual(len(checker.generic_instances), 1)

    def test_missing_extra_and_duplicate_methods_are_rejected(self):
        self.rejected(MEASURE + "impl Measure for Point {}", "XE-TRAIT-0003")
        self.rejected(MEASURE + "impl Measure for Point { fn other(self:Self@){} }", "XE-TRAIT-0003")
        self.rejected(MEASURE + "impl Measure for Point {fn measure(self:Self@)->i32{0} fn measure(self:Self@)->i32{1}}",
                      "XE-NAME-0002")

    def test_parameter_result_and_receiver_contracts_are_exact(self):
        for method in ("fn measure(self:Self@[mut])->i32{0}",
                       "fn measure(self:Self@)->i64{0}",
                       "fn measure(self:Self@, extra:i32)->i32{0}",
                       "fn measure(pointer:Self@)->i32{0}"):
            with self.subTest(method=method):
                self.rejected(MEASURE + "impl Measure for Point {" + method + "}", "XE-TRAIT-0003")

    def test_unknown_trait_is_rejected_even_for_unused_function(self):
        self.rejected("fn[T:Missing] inspect(value:T){} fn main(){}", "XE-TRAIT-0001")

    def test_unused_trait_contract_still_checks_types_and_duplicate_methods(self):
        self.rejected("trait Broken{fn inspect(self:Self@)->Missing;} fn main(){}", "XE-NAME-0001")
        self.rejected("trait Broken{fn inspect(self:Self@);fn inspect(self:Self@);} fn main(){}", "XE-NAME-0002")

    def test_unknown_impl_trait_is_rejected(self):
        self.rejected("struct P{} impl Missing for P{} fn main(){}", "XE-TRAIT-0001")

    def test_trait_argument_arity_is_checked(self):
        self.rejected("trait[T] Convert{} struct P{} impl Convert for P{}", "XE-TRAIT-0001")
        self.rejected("trait Measure{} fn[T:Measure[i32]] inspect(v:T){}", "XE-TRAIT-0001")

    def test_trait_and_impl_type_parameters_with_same_name_are_independent(self):
        self.checked("trait[T] Compare{fn equal(self:Self@,other:T)->bool;} struct[T] H{value:T,} "
                     "impl[T] Compare[i32] for H[T]{fn equal(self:Self@,other:i32)->bool{true}}")

    def test_duplicate_and_overlapping_trait_implementations_are_rejected(self):
        self.rejected("trait Marker{} struct[T] H{value:T,} impl[T] Marker for H[T]; impl Marker for H[i32];",
                      "XE-TRAIT-0002")
        self.rejected("trait Marker{} struct P{} impl Marker for P; impl Marker for P;", "XE-TRAIT-0002")

    def test_conditional_copy_depends_on_concrete_field_type(self):
        checker = self.checked("struct[T] Holder{value:T,} impl[T] Copy for Holder[T] where T implements Copy {} fn main(){}")
        self.assertTrue(checker.copyable(Type("Holder", (I32,))))
        self.assertFalse(checker.copyable(Type("Holder", (STRING,))))
        self.assertTrue(checker.copyable(Type("Holder", (Type("ptr", (STRING,)),))))

    def test_copy_bound_must_be_present_and_resource_payload_cannot_be_promised_copy(self):
        self.rejected("struct[T] Holder{value:T,} impl[T] Copy for Holder[T];", "XE-OWN-0001")
        self.rejected("struct[T] Holder{value:T,} impl Copy for Holder[String];", "XE-OWN-0001")
        self.rejected("enum[T] Item{Some[T],End,} impl[T] Copy for Item[T];", "XE-OWN-0001")

    def test_conditional_copy_propagates_through_nested_explicit_copy_types(self):
        checker = self.checked("struct[T] H{value:T,} impl[T] Copy for H[T] where T implements Copy {} "
                               "struct[T] Outer{value:H[T],} impl[T] Copy for Outer[T] where T implements Copy {}")
        self.assertTrue(checker.copyable(Type("Outer", (I32,))))
        self.assertFalse(checker.copyable(Type("Outer", (STRING,))))

    def test_concrete_copy_does_not_apply_to_other_instantiations(self):
        self.rejected("struct[T] H{value:T,} impl Copy for H[i32]; fn main(){let h=H[String]{String::from(\"x\") >> .value;};}",
                      "XE-OWN-0001")

    def test_copy_drop_overlap_is_forbidden_but_disjoint_instantiations_are_valid(self):
        self.rejected("struct[T] H{value:T,} impl[T] Copy for H[T] where T implements Copy {} "
                      "impl[T] Drop for H[T]{fn drop(self:Self@[mut]){}}", "XE-OWN-0001")
        self.checked("struct[T] H{value:T,} impl Copy for H[i32]; "
                     "impl Drop for H[String]{fn drop(self:Self@[mut]){}} fn main(){}")

    def test_partial_move_is_checked_against_concrete_drop_not_type_name(self):
        self.rejected("struct[T] H{value:T,} impl[T] Drop for H[T]{fn drop(self:Self@[mut]){}} "
                      "fn main(){let h<<H[String]{String::from(\"x\") >> .value;};let s<<h.value;}", "XE-OWN-0002")
        self.checked("struct[T] H{value:T,} impl[T] Drop for H[T] where T implements Copy {fn drop(self:Self@[mut]){}} "
                     "fn main(){let h<<H[String]{String::from(\"x\") >> .value;};let s<<h.value;}")

    def test_invalid_concrete_destructor_body_is_checked_before_backend(self):
        error = self.rejected("struct[T] H{value:T,} impl[T] Drop for H[T]{fn drop(self:Self@[mut]){let wrong:i32=self.value;}} "
                              "fn main(){let h<<H[String]{String::from(\"x\") >> .value;};}", "XE-TYPE-0001")
        self.assertIn("具体实例", error.hint or "")

    def test_trait_and_drop_instantiation_do_not_rewrite_original_ast(self):
        text = "struct[T] H{value:T,} impl[T] Drop for H[T]{fn drop(self:Self@[mut]){}} fn main(){let h<<H[i32]{.value=42;};}"
        tree = parse_source(text)
        before = copy.deepcopy(tree)
        checker = Checker(Source(text), tree)
        self.assertEqual(checker.check(), [])
        self.assertEqual(tree, before)


@unittest.skipUnless(CC, "需要系统 C 编译器")
class StaticTraitBackendTests(unittest.TestCase):
    def run_source(self, text, *, sanitize=False):
        flags = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie") if sanitize else ()
        with tempfile.TemporaryDirectory() as directory:
            source, executable = Path(directory) / "main.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, executable, cc=CC, extra_flags=flags)
            completed = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        return completed.stdout

    def test_trait_dispatch_and_default_method_execute(self):
        text = '''trait Measure {
            fn measure(self:Self@)->i32;
            fn doubled(self:Self@)->i32 { self.measure() * 2 }
        }
        struct Point{value:i32,}
        impl Measure for Point {fn measure(self:Self@)->i32{self.value}}
        fn[T:Measure] inspect(value:T@)->i32{value.doubled()}
        fn main(){let p<<Point{.value=21;};println("{}",inspect(p@));}'''
        self.assertEqual(self.run_source(text), "42\n")

    def test_generic_trait_attachment_and_impl_constraints_execute(self):
        text = '''trait[T] Read {fn read(self:Self@)->T;}
        struct[T] Holder{value:T,}
        impl[T] Read[T] for Holder[T] where T implements Copy {fn read(self:Self@)->T{self.value}}
        fn[T:Read[i32]] inspect(value:T@)->i32{value.read()}
        fn main(){let h<<Holder[i32]{.value=42;};println("{}",inspect(h@));}'''
        self.assertEqual(self.run_source(text), "42\n")

    def test_generic_trait_default_and_generic_method_execute(self):
        text = '''trait[T] Read {fn read(self:Self@)->T; fn twice(self:Self@)->tuple[T,T] where T implements Copy {tuple[self.read(),self.read()]}}
        struct[T] H{value:T,}
        impl[T] Read[T] for H[T] where T implements Copy {fn read(self:Self@)->T{self.value}}
        fn main(){let h<<H[i32]{.value=21;};let tuple[a,b]=h.twice();println("{}",a+b);}'''
        self.assertEqual(self.run_source(text), "42\n")

    def test_conditional_copy_struct_enum_and_copy_constraint_execute(self):
        text = '''struct[T] H{value:T,} impl[T] Copy for H[T] where T implements Copy {}
        enum[T] Item{Some[T],End,} impl[T] Copy for Item[T] where T implements Copy {}
        fn[T:Copy] copy(value:T)->T{let other=value;other}
        fn main(){let h=H[i32]{.value=42;};let other=copy(h);let e:Item[i32]=Item::Some[other.value];
            let another=e;println("{} {}",h.value,other.value);
            another? {Item::Some :> n -> println("{}",n),Item::End :> _ -> {},};}'''
        self.assertEqual(self.run_source(text), "42 42\n42\n")

    def test_generic_drop_releases_fields_once_in_nested_containers(self):
        text = '''struct Trace{number:i32,}
        impl Drop for Trace{fn drop(self:Self@[mut]){println("field {}",self.number);}}
        struct[T] H{value:T,}
        impl[T] Drop for H[T]{fn drop(self:Self@[mut]){println("holder");}}
        fn main(){
            let a<<Box[H[Trace]]::new(H[Trace]{Trace{.number=1;} >> .value;})?[panic];
            let[mut] values<<Vec[H[Trace]]::new();values.push(H[Trace]{Trace{.number=2;} >> .value;});
        }'''
        self.assertEqual(self.run_source(text), "holder\nfield 2\nholder\nfield 1\n")

    def test_generic_enum_drop_runs_before_only_active_payload(self):
        text = '''struct Trace{number:i32,}
        impl Drop for Trace{fn drop(self:Self@[mut]){println("field {}",self.number);}}
        enum[T] Item{Some[T],End,}
        impl[T] Drop for Item[T]{fn drop(self:Self@[mut]){println("item");}}
        fn main(){let a<<Item[Trace]::Some[Trace{.number=1;}];let b<<Item[Trace]::End;}'''
        self.assertEqual(self.run_source(text), "item\nitem\nfield 1\n")

    def test_drop_function_name_does_not_collide_with_free_function(self):
        text = '''fn drop(){println("free");} struct Trace{number:i32,}
        impl Drop for Trace{fn drop(self:Self@[mut]){println("owned {}",self.number);}}
        fn main(){drop();let trace<<Trace{.number=42;};}'''
        self.assertEqual(self.run_source(text), "free\nowned 42\n")

    def test_generic_drop_body_is_checked_and_runs_with_default_leak_sanitizer(self):
        text = '''struct[T] H{value:T,} impl[T] Drop for H[T]{fn drop(self:Self@[mut]){println("holder");}}
        fn main(){let h<<H[String]{String::from("owned") >> .value;};println("{}",h.value@);}'''
        self.assertEqual(self.run_source(text, sanitize=True), "owned\nholder\n")


if __name__ == "__main__":
    unittest.main()
