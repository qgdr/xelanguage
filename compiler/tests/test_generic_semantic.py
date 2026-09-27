"""具体泛型实例必须遵守普通类型/所有权规则，不借模板绕过检查。

这些测试不依赖 C 编译器，便于定位：实例缓存、模板类型代入还是具体
函数体有问题。执行/清理的跨阶段测试另见 test_generic_backend.py。
"""
import unittest

from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source


class GenericSemanticTests(unittest.TestCase):
    def checker(self, text, limit=256):
        checker = Checker(Source(text), parse_source(text))
        checker.instance_limit = limit
        return checker

    def succeeds(self, text):
        checker = self.checker(text)
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(e.render() for e in errors))
        return checker

    def fails(self, text, code, limit=256):
        errors = self.checker(text, limit).check()
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, code, errors[0].render())
        return errors[0]

    def test_same_type_inference_and_explicit_application_share_one_instance(self):
        checker = self.succeeds("fn[T] id(x: T) -> T { x } fn main() { id(1); id[i32](2); id(3); }")
        self.assertEqual(len(checker.generic_instances), 1)

    def test_different_types_have_separate_concrete_bodies(self):
        checker = self.succeeds('fn[T] id(x: T) -> T { x } fn main() { id(1); id(String::from("x")); }')
        self.assertEqual(len(checker.generic_instances), 2)
        instances = [checker.functions[name] for name in checker.generic_instances.values()]
        self.assertTrue(all(not signature.generics for signature in instances))
        self.assertEqual({str(s.result) for s in instances}, {"i32", "String"})

    def test_nested_call_discovers_another_instance(self):
        checker = self.succeeds("fn[T] id(x: T) -> T { x } fn[T] outer(x: T) -> T { id[T](x) } fn main() { outer(1); }")
        self.assertEqual(len(checker.generic_instances), 2)

    def test_unused_body_is_delayed_and_used_body_is_checked(self):
        self.succeeds("fn[T] broken(x: T) -> T { missing } fn main() {}")
        self.fails("fn[T] broken(x: T) -> T { missing } fn main() { broken(1); }", "XE-NAME-0001")

    def test_copy_assignment_does_not_turn_into_implicit_move(self):
        self.succeeds("fn[T] copied(x: T) -> T { let y: T = x; y } fn main() { copied(1); }")
        self.fails('fn[T] copied(x: T) -> T { let y: T = x; y } fn main() { copied(String::from("x")); }', "XE-OWN-0001")

    def test_move_assignment_does_not_accept_copy_instance(self):
        self.fails("fn[T] moved(x: T) -> T { let y: T << x; y } fn main() { moved(1); }", "XE-OWN-0001")

    def test_resource_cannot_be_duplicated_after_instantiation(self):
        self.fails('fn[T] twice(x: T) -> T { let y: T << x; x } fn main() { twice(String::from("x")); }', "XE-MOVE-0001")

    def test_copy_constraint_and_pointer_exception(self):
        self.succeeds("fn[T: Copy] copied(x: T) -> T { let y = x; y } fn main() { let[mut] n = 0; copied(n@[mut]); }")
        self.fails('fn[T: Copy] copied(x: T) -> T { x } fn main() { copied(String::from("x")); }', "XE-OWN-0001")

    def test_missing_explicit_arguments_and_conflicting_inference(self):
        self.fails("fn[T,U] first(x:T,y:U)->T{x} fn main(){first[i32](1,2);}", "XE-GENERIC-0001")
        self.fails('fn[T] same(x:T,y:T)->T{x} fn main(){same(1,String::from("x"));}', "XE-TYPE-0001")

    def test_return_only_type_parameter_requires_explicit_argument(self):
        self.fails("fn[T] impossible()->T{panic(\"unused\")} fn main(){impossible();}", "XE-GENERIC-0001")

    def test_expanding_generic_recursion_has_friendly_limit(self):
        text = "struct[T] Holder{value:T,} fn[T] grow(x:T){grow(Holder[T]{x >> .value;});} fn main(){grow(1);}"
        error = self.fails(text, "XE-GENERIC-0002", limit=8)
        self.assertIn("递归实例化", error.message)

    def test_generic_drop_fails_before_c_backend(self):
        self.fails("struct[T] Holder{value:T,} impl[T] Drop for Holder[T]{fn drop(self:Self@[mut]){}} fn main(){}", "XE-SEM-0001")

    def test_generic_copy_is_not_accidentally_global_by_type_name(self):
        self.fails("struct[T] Holder{value:T,} impl Copy for Holder[i32]; fn main(){}", "XE-SEM-0001")

    def test_bare_generic_struct_requires_context_or_explicit_types(self):
        self.fails("struct[T] Holder{value:T,} fn main(){let h<<Holder{42 >> .value;};}", "XE-GENERIC-0001")
        self.succeeds("struct[T] Holder{value:T,} fn main(){let h:Holder[i32]<<Holder{42 >> .value;};}")

    def test_bare_generic_enum_requires_context_or_explicit_types(self):
        self.fails("enum[T] Event{Value[T],End,} fn main(){let e<<Event::Value[42];}", "XE-GENERIC-0001")
        self.fails("enum[T] Event{Value[T],End,} fn main(){let e<<Event::End;}", "XE-GENERIC-0001")
        self.succeeds("enum[T] Event{Value[T],End,} fn main(){let e:Event[i32]<<Event::Value[42];}")

    def test_generic_function_value_requires_explicit_type_arguments(self):
        error = self.fails("fn[T] id(x:T)->T{x} fn main(){let f=id;}", "XE-GENERIC-0001")
        self.assertIn("id[i32]", error.hint)

    def test_concrete_method_cannot_be_called_on_wrong_generic_owner(self):
        self.fails('struct[T] Holder{value:T,} impl Holder[i32]{fn number(self:Self@)->i32{self.value}} fn main(){let h<<Holder[String]{String::from("x") >> .value;};h.number();}', "XE-TYPE-0001")


if __name__ == "__main__":
    unittest.main()
