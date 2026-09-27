"""透明类型别名属于模块名称解析，不产生新布局或新的所有权身份。"""
import copy
import unittest

from compiler.xe_ast import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source
from compiler.xe_ast.typesys import I32, Type


class TypeAliasTests(unittest.TestCase):
    def check(self, text):
        tree = parse_source(text)
        original = copy.deepcopy(tree)
        checker = Checker(Source(text), tree)
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        self.assertEqual(tree, original)
        return checker

    def assert_error(self, text, code):
        errors = check_source(text)
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())
        return errors[0]

    def test_public_alias_ast_and_transparent_type(self):
        text = 'pub type Count = i32; fn f(x:Count)->i32 {x}'
        checker = self.check(text)
        declaration = checker.tree["items"][0]
        self.assertEqual(declaration["kind"], "TypeAlias")
        self.assertTrue(declaration["public"])
        self.assertEqual(declaration["name"], "Count")
        self.assertEqual(checker.alias_types["Count"], I32)
        self.assertNotIn("Count", checker.types)
        self.assertEqual(checker.functions["f"].parameters, [I32])

    def test_forward_references_and_chains(self):
        checker = self.check('fn f(x:A)->C {x} type A=B; type B=C; type C=i32;')
        self.assertEqual(set(checker.alias_types.values()), {I32})

    def test_composite_aliases_function_pointer_tuple_result_and_array(self):
        text = '''type handler=fn(i32)->i32; type Read=i32@; type Pair=tuple[Read,handler];
            type Outcome=Pair?[String]; type Items=Array[Pair,2];
            fn f(x:handler,p:Read)->Pair {tuple[p,x]}'''
        checker = self.check(text)
        self.assertEqual(checker.alias_types["handler"].name, "fn")
        self.assertEqual(checker.alias_types["Pair"].name, "tuple")
        self.assertEqual(checker.alias_types["Items"].args[0], checker.alias_types["Pair"])

    def test_aliases_are_valid_generic_type_arguments(self):
        self.check('''type Number=i32; type Pair=tuple[Number,bool];
            fn[T] identity(x:T)->T{x} fn f()->Pair{identity[Pair](tuple[1,true])}''')

    def test_alias_target_can_be_concrete_generic_instance(self):
        checker = self.check('type IntBox=Holder[i32]; struct[T] Holder{value:T,} fn f(x:IntBox)->Holder[i32]{x}')
        self.assertEqual(checker.alias_types["IntBox"], Type("Holder", (I32,)))

    def test_generic_alias_syntax_and_application_are_rejected(self):
        for text in ('type[T] Alias=T;', 'type Alias[T]=T;'):
            error = self.assert_error(text, "XE-PARSE-0001")
            self.assertIn("泛型类型别名", error.message)
        self.assert_error('type Alias=i32; fn f(x:Alias[i32]){}', "XE-GENERIC-0001")

    def test_unknown_targets_fail_even_if_unused(self):
        self.assert_error('type Typo=Unknown; fn main(){}', "XE-NAME-0001")

    def test_cycles_have_source_diagnostic_and_chain(self):
        for text in ('type A=A;', 'type A=B; type B=A;', 'type A=B@; type B=tuple[i32,A];'):
            error = self.assert_error(text, "XE-TYPE-0007")
            self.assertIn("循环引用", error.message)
            self.assertIn("A", error.message)

    def test_extremely_long_alias_chain_is_a_diagnostic(self):
        text = " ".join(f'type A{i}=A{i+1};' for i in range(150)) + ' type A150=i32;'
        self.assert_error(text, "XE-TYPE-0007")

    def test_duplicates_across_top_level_names_and_extern(self):
        for text in ('type A=i32; type A=bool;', 'type A=i32; struct A;',
                     'fn A(){} type A=i32;', 'type A=i32; const A:i32=1;',
                     'type A=i32; extern "C" {fn A();}'):
            with self.subTest(text=text):
                self.assert_error(text, "XE-NAME-0002")

    def test_reserved_type_names_cannot_be_redefined(self):
        for name in ("i32", "String", "Self", "Maybe", "Unit"):
            self.assert_error(f'type {name}=bool;', "XE-NAME-0002")

    def test_aliases_do_not_capture_function_generic_or_self(self):
        self.assert_error('type A=T; fn[T] f(x:A){}', "XE-NAME-0001")
        self.assert_error('struct S; impl S {fn f(x:A){}} type A=Self;', "XE-NAME-0001")

    def test_function_generic_name_shadows_alias_in_function_scope(self):
        checker = self.check('type T=i32; fn[T] identity(x:T)->T{x} fn f()->bool{identity(true)}')
        self.assertEqual(checker.functions["identity"].parameters, [Type("$T")])

    def test_alias_struct_literals_and_associated_function_values(self):
        self.check('''type Alias=Point; struct Point{x:i32,} impl Copy for Point;
            impl Point{fn new(x:i32)->Self{Self{.x=x;}}}
            fn f(){let p=Alias{.x=1;}; let constructor:fn(i32)->Alias=Alias::new; let q=constructor(2);}''')

    def test_alias_of_generic_struct_works_with_associated_methods(self):
        self.check('''type IntHolder=Holder[i32]; struct[T] Holder{x:T,}
            impl[T] Holder[T]{fn new(x:T)->Self{Self{x >> .x;}}}
            fn f()->IntHolder{IntHolder::new(42)}''')

    def test_alias_enum_variants_and_selectors_use_canonical_owner(self):
        self.check('''enum[T] Event{Value[T],Empty,} type Message=Event[i32];
            fn f()->i32{let message << Message::Value[42];
            message?{Message::Value :> x -> x, Message::Empty :> _ -> 0,}}''')

    def test_alias_selector_does_not_match_wrong_generic_instance(self):
        self.assert_error('''enum[T] Event{Value[T],Empty,} type Numbers=Event[i32];
            fn f(x:Event[bool]){x?{Numbers::Value :> n -> {}, Numbers::Empty :> _ -> {},};}''', "XE-MATCH-0001")

    def test_alias_copy_and_drop_apply_to_original_type(self):
        checker = self.check('type A=S; struct S{x:i32,} impl Copy for A; fn f(x:A){let y=x;}')
        self.assertEqual(checker.copy_types, {"S"})
        self.assert_error('type A=S; struct S{x:String,} impl Copy for A;', "XE-OWN-0001")
        checker = self.check('''type A=S; struct S{x:i32,}
            impl Drop for A{fn drop(self:Self@[mut]){}} fn f(){let x << A{.x=1;};}''')
        self.assertEqual(checker.drop_types, {"S"})

    def test_pointer_alias_keeps_mutability_and_resource_rules(self):
        self.assert_error('type Read=i32@; fn f(x:Read){x#=1;}', "XE-MUT-0001")
        self.assert_error('type Text=String; fn f(x:Text){let y=x;}', "XE-OWN-0001")

    def test_alias_does_not_create_nominal_recursion(self):
        self.check('type Link=Node@; struct Node{next:Link,} fn main(){}')
        self.assert_error('type Value=Node; struct Node{next:Value,}', "XE-TYPE-0006")

    def test_trait_and_none_are_not_aliasable_value_types(self):
        self.assert_error('trait Marker{} type Object=Marker;', "XE-SEM-0001")
        self.assert_error('type Nothing=None;', "XE-RESULT-0001")

    def test_alias_is_top_level_only(self):
        self.assert_error('fn f(){type Local=i32;}', "XE-PARSE-0001")


if __name__ == "__main__":
    unittest.main()
