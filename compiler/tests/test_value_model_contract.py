"""Keep value-model distinctions visible without choosing new type-system rules.

These tests pin the current contract: no payload is not one Unit parameter,
ignoring a handler's inputs does not invent a value, and an uninhabited element
type does not make every enclosing container or function value uninhabited.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import check_source


class ValueModelContractTests(LegacyTestCase):
    def assert_rejected(self, source, code):
        errors = check_source(source)
        self.assertTrue(errors, source)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def assert_execution(self, source, expected):
        self.assertEqual(check_source(source), [])
        with tempfile.TemporaryDirectory(prefix="xe-value-model-") as directory:
            root = Path(directory)
            entry = root / "main.xe"
            entry.write_text(source, encoding="utf-8")
            executable = build_executable(entry, root / "program", cc="gcc")
            result = subprocess.run([str(executable)], capture_output=True,
                                    text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)

    def test_none_and_unit_have_different_function_target_arities(self):
        for source in (
            "fn h()->i32{10} fn f(x:i32?)->i32{x?1>n->n 2>h}",
            "fn h(x:Unit)->i32{20} fn f(x:i32?[Unit])->i32{x?1>n->n 2>h}",
            "fn f(x:i32?)->i32{x?1>n->n 2>fn()->i32{10}}",
            "fn f(x:i32?[Unit])->i32{x?1>n->n 2>fn(x:Unit)->i32{20}}",
        ):
            with self.subTest(source=source):
                self.assertEqual(check_source(source + " fn main(){}"), [])
        for source in (
            "fn h(x:Unit)->i32{10} fn f(x:i32?)->i32{x?1>n->n 2>h}",
            "fn h()->i32{20} fn f(x:i32?[Unit])->i32{x?1>n->n 2>h}",
            "fn f(x:i32?)->i32{x?1>n->n 2>fn(x:Unit)->i32{10}}",
            "fn f(x:i32?[Unit])->i32{x?1>n->n 2>fn()->i32{20}}",
        ):
            with self.subTest(source=source):
                self.assert_rejected(source + " fn main(){}", "XE-CALL-0001")

    def test_untyped_ignore_all_is_not_a_typed_unit_binding(self):
        for source in (
            "fn f(x:i32?)->i32{x?1>n->n 2>_ ->0}",
            "fn f(x:i32?[Unit])->i32{x?1>n->n 2>_ ->0}",
            "fn f(x:i32?[Unit])->i32{x?1>n->n 2>_:Unit ->0}",
            "fn f(x:i32?[Unit])->i32{x?1>n->n 2>value:Unit ->0}",
        ):
            with self.subTest(source=source):
                self.assertEqual(check_source(source + " fn main(){}"), [])
        for source in (
            "fn f(x:i32?)->i32{x?1>n->n 2>_:Unit ->0}",
            "fn f(x:i32?)->i32{x?1>n->n 2>value ->0}",
        ):
            with self.subTest(source=source):
                self.assert_rejected(source + " fn main(){}", "XE-CALL-0001")

    def test_ignore_all_accepts_an_entire_multi_payload_input(self):
        source = '''enum Pair { Both[i32, Unit], End, }
        fn inspect(value:Pair)->i32 {
            value ? {
                Pair::Both :> _ -> 7,
                Pair::End :> _ -> 8,
            }
        }
        fn main() {}'''
        self.assertEqual(check_source(source), [])
        self.assertTrue(lower_to_c(source))

    def test_none_marker_does_not_become_an_ordinary_payload_type(self):
        self.assert_rejected("fn inspect(value:None){} fn main(){}", "XE-RESULT-0001")
        self.assert_rejected("fn main(){let x:i32?=Maybe::None[unit];}", "XE-CALL-0001")
        self.assert_rejected("fn main(){let x:i32?[Unit]=Maybe::None;}", "XE-TYPE-0001")

    def test_never_parameter_and_function_value_exist_without_a_normal_argument(self):
        source = '''fn impossible(value:Never)->i32 { 42 }
        fn main() {
            let callback:fn(Never)->i32 = impossible;
        }'''
        self.assertEqual(check_source(source), [])
        self.assertTrue(lower_to_c(source))
        for argument in ("1", "unit"):
            with self.subTest(argument=argument):
                self.assert_rejected(
                    "fn impossible(value:Never)->i32{42} "
                    "fn main(){impossible(" + argument + ");}", "XE-TYPE-0001")

    def test_empty_never_array_does_not_require_a_never_element(self):
        source = "fn main(){let a:Array[Never,0]=[]; let b:Array[Never,0]=a;}"
        self.assertEqual(check_source(source), [])
        self.assertTrue(lower_to_c(source))
        for source in (
            "fn main(){let a:Array[Never,1]=[];}",
            "fn main(){let a:Array[Never,1]=[unit];}",
        ):
            with self.subTest(source=source):
                self.assert_rejected(source, "XE-TYPE-0001")

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 验证值模型的分支区别")
    def test_none_unit_unit_optional_and_empty_array_execute(self):
        self.assert_execution('''fn zero()->i32 { 10 }
        fn one(value:Unit)->i32 { 20 }
        fn present(value:Unit)->i32 { 1 }
        fn absent()->i32 { 2 }
        fn main() {
            let a:i32? = Maybe::None;
            let b:i32?[Unit] = Maybe::No[unit];
            let c:i32? = Maybe::None;
            let d:i32?[Unit] = Maybe::No[unit];
            let e:i32? = Maybe::None;
            let f:i32?[Unit] = Maybe::No[unit];
            let v1 = a?1>n->n 2>zero;
            let v2 = b?1>n->n 2>one;
            let v3 = c?1>n->n 2>_ ->30;
            let v4 = d?1>n->n 2>_ ->40;
            let v5 = e?1>n->n 2>fn()->i32{50};
            let v6 = f?1>n->n 2>value:Unit ->60;
            println("{} {} {} {} {} {}",v1,v2,v3,v4,v5,v6);
            // Yes[unit] and None are two different normal values of Unit?.
            let yes:Unit? = Maybe::Yes[unit];
            let no:Unit? = Maybe::None;
            let v7 = yes?1>present 2>absent;
            let v8 = no?1>present 2>absent;
            println("{} {}",v7,v8);
            let empty:Array[Never,0] = [];
            println("{}",empty.len());
        }''', "10 20 30 40 50 60\n1 2\n0\n")

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 验证不可发生分支的处理函数")
    def test_never_parameter_eliminates_an_impossible_alternative(self):
        # The No arm has a valid signature, but no value can select it here.
        # This does not request automatic uninhabited-pattern exhaustiveness.
        self.assert_execution('''fn absurd(impossible:Never)->i32 { impossible }
        fn certain()->i32?[Never] { 42 }
        fn extract(value:i32?[Never])->i32 {
            value?1>number->number 2>absurd
        }
        fn main(){println("{}",extract(certain()));}
        ''', "42\n")

    @unittest.skipUnless(shutil.which("gcc"), "需要 GCC 验证被忽略载荷的清理")
    def test_ignored_resource_payload_still_has_an_owner_and_cleanup(self):
        self.assert_execution('''struct Trace { id:i32, text:String, }
        impl Drop for Trace {
            fn drop(self:Self@[mut]) { println("drop {}",self.id); }
        }
        fn main() {
            let value:i32?[Trace] << Maybe::No[Trace {
                .id=7;
                .text << String::from("owned");
            }];
            let result=value?1>_ ->1 2>_ ->{println("handler");2};
            println("result {}",result);
        }''', "handler\ndrop 7\nresult 2\n")


if __name__ == "__main__":
    unittest.main()
