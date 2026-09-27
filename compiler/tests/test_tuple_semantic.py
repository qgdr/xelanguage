"""tuple[] 构造、类型和解包共享既有值传递、写权限和所有权规则。"""
import copy
import unittest

from compiler.xe_ast import parse_source
from compiler.xe_ast.lexer import Lexer
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source
from compiler.xe_ast.typesys import I32, Type


class TupleSemanticTests(unittest.TestCase):
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

    def test_tuple_keyword_and_rendering(self):
        self.assertEqual(Lexer(Source("tuple")).scan()[0].kind, "tuple")
        self.assertEqual(str(Type("tuple", (I32, Type("bool")))), "tuple[i32, bool]")

    def test_tuple_constructor_and_single_element(self):
        self.check('fn f()->tuple[i32,bool] {tuple[1,true]} fn g()->tuple[i32] {tuple[2,]}')

    def test_declaration_ast_preserves_member_annotations(self):
        tree = parse_source('fn f() {let[mut] tuple[a:i8,b:bool] = tuple[1,true];}')
        statement = tree["items"][0]["body"]["statements"][0]
        self.assertEqual(statement["kind"], "Destructure")
        self.assertTrue(statement["declare"])
        self.assertTrue(statement["mutable"])
        self.assertEqual(statement["operator"], "=")
        self.assertEqual([target["name"] for target in statement["targets"]], ["a", "b"])
        self.assertTrue(all(target["kind"] == "TupleBinding" and target["type"] for target in statement["targets"]))

    def test_existing_assignments_and_transfer(self):
        self.check('''fn f() { let[mut] tuple[a,b] = tuple[1,2];
            tuple[a,b] = tuple[b,a]; tuple[3,4] >> tuple[a,b]; }''')
        self.check('''fn f() {let[mut] a << String::from("a"); let[mut] b << String::from("b");
            tuple[a,b] << tuple[b,a]; tuple[String::from("c"),String::from("d")] >> tuple[a,b];}''')

    def test_delayed_initialization(self):
        self.check('fn f() { let a:i32; let b:bool; tuple[a,b] = tuple[1,true]; println("{} {}",a,b); }')

    def test_member_types_contextualize_integer_literals(self):
        checker = self.check('fn f() { let tuple[a:u8,b:i64] = tuple[255,2147483648]; }')
        self.assertEqual([t.name for t in checker.destructure_types.values()], ["u8", "i64"])
        self.assert_error('fn f() {let tuple[a:u8,b] = tuple[256,0];}', "XE-TYPE-0004")

    def test_member_pointer_annotations_can_drop_write_permission(self):
        self.check('''fn f() {let[mut] x=1; let tuple[p:i32@,n] = tuple[x@[mut],0]; println("{}",p#);}''')
        self.assert_error('fn f() {let x=1; let tuple[p:i32@[mut],n] = tuple[x@,0];}', "XE-TYPE-0001")
        self.check('''fn pair(p:i32@[mut])->tuple[i32@[mut],i32]{tuple[p,0]}
            fn f(){let[mut] x=1; let tuple[p:i32@,n]=pair(x@[mut]);}''')
        self.assert_error('''fn pair(p:i32@[mut])->tuple[i32@[mut],i32]{tuple[p,0]}
            fn f(){let[mut] x=1; let p:tuple[i32@,i32]=pair(x@[mut]);}''', "XE-TYPE-0001")

    def test_unpack_requires_tuple_and_exact_arity(self):
        for text in ('fn f(){let tuple[a,b] = 1;}',
                     'fn f(){let tuple[a,b] = tuple[1];}',
                     'fn f(){let tuple[a] = tuple[1,2];}'):
            with self.subTest(text=text):
                self.assert_error(text, "XE-TYPE-0001")

    def test_copy_and_move_operators_use_whole_tuple_ownership(self):
        self.assert_error('fn f(){let tuple[a,b] << tuple[1,2];}', "XE-OWN-0001")
        self.assert_error('fn f(){let tuple[a,b] = tuple[String::from("x"),2];}', "XE-OWN-0001")
        self.check('fn f(){let tuple[a,b] << tuple[String::from("x"),2]; println("{} {}",a@,b);}')

    def test_owned_tuple_source_is_moved(self):
        self.assert_error('''fn f(p:tuple[String,i32]) {let tuple[a,b] << p; println("{}",p.1);}''', "XE-MOVE-0001")

    def test_nonowning_resource_sources_are_rejected(self):
        for operator in ("<<", ">>"):
            body = f'let tuple[a,b] {operator} p#;' if operator == "<<" else 'let[mut] a:String; let[mut] b:i32; p# >> tuple[a,b];'
            self.assert_error(f'fn f(p:tuple[String,i32]@) {{{body}}}', "XE-MOVE-0002")
        self.check('fn f(p:tuple[i32,bool]@) {let tuple[a,b] = p#;}')

    def test_duplicate_resource_values_do_not_copy_ownership(self):
        self.assert_error('fn f(x:String) {let pair << tuple[x,x];}', "XE-MOVE-0001")

    def test_duplicate_targets_but_repeated_ignores_are_allowed(self):
        self.assert_error('fn f(){let tuple[a,a] = tuple[1,2];}', "XE-NAME-0002")
        self.assert_error('fn f(){let[mut] a=1; tuple[a,a] = tuple[2,3];}', "XE-NAME-0002")
        checker = self.check('fn f(){let tuple[_,_] << tuple[String::from("x"),String::from("y")];}')
        self.assertEqual(len(checker.destructure_types), 2)
        self.assertTrue(all(t.name == "String" for t in checker.destructure_types.values()))

    def test_immutable_assignment_and_uninitialized_reads(self):
        self.assert_error('fn f(){let tuple[a,b]=tuple[1,2]; tuple[a,b]=tuple[3,4];}', "XE-MUT-0001")
        self.assert_error('fn f(){let a:i32; let b:i32; tuple[a,b]=tuple[b,a];}', "XE-INIT-0001")

    def test_targets_are_variable_names(self):
        self.assert_error('fn f(){let[mut] a=tuple[1,2]; tuple[a.0,a.1]=tuple[3,4];}', "XE-PARSE-0001")

    def test_ignore_and_annotations_are_not_ordinary_values(self):
        for text in ('fn f(){let a=tuple[_,1];}', 'fn f(){let a=tuple[x:i32,1];}'):
            self.assert_error(text, "XE-SEM-0001")

    def test_old_tuple_spellings_explain_migration(self):
        for text in ('fn f(x:{i32,bool}) {}', 'fn f(){let x={1,true};}',
                     'fn f(){let x=(1,true);}', 'fn f(x:(i32,bool)) {}',
                     'fn f(x:tuple[i32,bool]) {x? {{_,_} :> _ -> {},};}'):
            with self.subTest(text=text):
                error = self.assert_error(text, "XE-PARSE-0001")
                self.assertIn("tuple[", error.message + (error.hint or ""))

    def test_empty_tuples_are_not_added(self):
        for text in ('fn f(){let a=tuple[];}', 'fn f(x:tuple[]){}', 'fn f(){let tuple[]=tuple[1];}'):
            self.assert_error(text, "XE-PARSE-0001")

    def test_braces_remain_blocks(self):
        self.check('fn f()->i32 {{let x=1; x}}')

    def test_declared_mutability_attachment_rules_remain(self):
        for attachment in ("[]", "[readonly]", "[mut,mut]"):
            self.assert_error(f'fn f(){{let{attachment} tuple[a,b]=tuple[1,2];}}', "XE-PARSE-0001")

    def test_rhs_return_stops_declaring_and_later_statements(self):
        self.check('fn f(){let tuple[a,b] << tuple[String::from("x"),{return;}]; missing;}')

    def test_tuple_selector_still_reports_existing_support_limit(self):
        self.assert_error('fn f(p:tuple[i32,bool]) {p? {tuple[_,_] :> _ -> {},};}', "XE-SEM-0001")
        self.assert_error('fn f(p:tuple[i32,bool]) {p? {tuple[tuple[_],_] :> _ -> {},};}', "XE-PARSE-0001")


if __name__ == "__main__":
    unittest.main()
