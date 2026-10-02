"""发布范围门禁：不把历史 AST 能解析误称为语义检查和后端都支持。

能力缺口应由 check 与 emit-C 给出同一前端诊断。与之相反，已确认的
普通指针 unsafe 附件是风险元数据，只能警告，不能按旧 unsafe 块拒绝。
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source

CC = shutil.which("cc") or ""


class ReleaseCapabilityTests(unittest.TestCase):
    def run_source(self, text):
        if not CC:
            self.skipTest("需要系统 C 编译器")
        with tempfile.TemporaryDirectory() as directory:
            source, executable = Path(directory) / "main.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, executable, cc=CC)
            completed = subprocess.run([str(executable)], text=True, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def assert_same_frontend_failure(self, text, code):
        errors = check_source(text, "release.xe")
        self.assertTrue(errors, text)
        self.assertEqual(errors[0].code, code, errors[0].render())
        with self.assertRaises(Diagnostic) as caught:
            lower_to_c(text, "release.xe")
        self.assertEqual(caught.exception.code, errors[0].code)
        self.assertEqual(caught.exception.message, errors[0].message)
        self.assertEqual(caught.exception.to_dict()["span"], errors[0].to_dict()["span"])
        return errors[0]

    def test_historical_unsafe_block_still_has_an_ast_for_migration(self):
        tree = parse_source("fn main() { unsafe { 42 }; }")
        expression = tree["items"][0]["body"]["statements"][0]["expression"]
        self.assertEqual(expression["kind"], "Unsafe")
        self.assertEqual(expression["body"]["tail"]["value"], 42)

    def test_unsafe_block_is_rejected_before_c_lowering(self):
        for text in ("fn main() { unsafe { 42 }; }",
                     "fn main() -> i32 { unsafe { 42 } }",
                     "fn main() { if false { unsafe { 42 }; } }"):
            with self.subTest(source=text):
                error = self.assert_same_frontend_failure(text, "XE-SEM-0001")
                self.assertIn("unsafe 代码块", error.message)
                self.assertIn("T@[unsafe]", error.hint or "")
                self.assertIn("T@[mut, unsafe]", error.hint or "")
                self.assertNotIn("C 后端", error.message)

    def test_used_generic_unsafe_block_receives_the_same_frontend_diagnostic(self):
        text = "fn[T] old(value: T) -> T { unsafe { value } } fn main() { old(42); }"
        self.assert_same_frontend_failure(text, "XE-SEM-0001")

    def test_ordinary_block_and_unsafe_pointer_metadata_remain_supported(self):
        text = '''fn main() {
            let[mut] value = { 41 };
            let pointer: i32@[mut, unsafe] = value@[mut, unsafe];
            pointer# = pointer# + 1;
            println("{}", pointer#);
        }'''
        checker = Checker(Source(text), parse_source(text))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        self.assertTrue(checker.warnings)
        self.assertTrue(all(warning.severity == "warning" for warning in checker.warnings))
        warnings = []
        generated = lower_to_c(text, warnings=warnings)
        self.assertIn("int main(", generated)
        self.assertTrue(warnings)
        self.assertEqual(self.run_source(text), "42\n")

    def test_builtin_placeholder_types_are_ast_only_current_capabilities(self):
        for type_ in ("Map[i32,i32]", "Set[i32]", "Iterator[i32]", "Formatter"):
            for text in (f"fn inspect(value:{type_}){{}} fn main(){{}}",
                         f"fn main(){{let value:{type_};}}"):
                with self.subTest(type=type_, source=text):
                    self.assertEqual(parse_source(text)["kind"], "Module")
                    error = self.assert_same_frontend_failure(text, "XE-SEM-0001")
                    self.assertIn("当前版本尚未实现内建", error.message)
                    self.assertIn("后续目标能力", error.message)

    def test_user_types_with_placeholder_names_keep_existing_name_precedence(self):
        text = '''struct[T,U] Map {value:T,}
            impl[T,U] Copy for Map[T,U] where T implements Copy {}
            struct[T] Set {value:T,}
            impl[T] Copy for Set[T] where T implements Copy {}
            struct[T] Iterator {value:T,}
            impl[T] Copy for Iterator[T] where T implements Copy {}
            struct Formatter {value:i32,} impl Copy for Formatter;
            fn main(){
                let map=Map[i32,i32]{.value=10;};let set=Set[i32]{.value=11;};
                let iterator=Iterator[i32]{.value=12;};let output=Formatter{.value=9;};
                println("{}",map.value+set.value+iterator.value+output.value);
            }'''
        self.assertEqual(check_source(text), [])
        self.assertEqual(self.run_source(text), "42\n")

    def test_method_self_type_contract_is_checked_before_backend(self):
        for receiver in ("i32", "i32@", "Self@@", "Other"):
            text = f"struct P{{value:i32,}} struct Other{{}} impl P{{fn bad(self:{receiver}){{}}}} fn main(){{}}"
            with self.subTest(receiver=receiver):
                error = self.assert_same_frontend_failure(text, "XE-TYPE-0001")
                self.assertIn("方法 self 类型", error.message)
                self.assertIn("self: Self", error.hint or "")

    def test_approved_self_forms_and_associated_functions_still_compile(self):
        text = '''struct P{value:i32,}impl Copy for P;
            impl P{
                fn own(self:Self)->i32{self.value}
                fn read(self:P@)->i32{self.value}
                fn write(self:Self@[mut]){self.value=self.value+1;}
                fn constant()->i32{1}
            }
            fn main(){let[mut] value=P{.value=12;};value.write();
                println("{}",value.own()+value.read()+value.own()+P::constant()+2);}
        '''
        self.assertEqual(self.run_source(text), "42\n")

    def test_unimplemented_output_types_are_frontend_capability_diagnostics(self):
        sources = (
            'struct P{value:i32,}impl Copy for P;fn main(){let p=P{.value=1;};println("{}",p);}',
            'fn main(){println("{}",[1,2]);}',
            'fn main(){println("{}",tuple[1,2]);}',
            'fn f(){}fn main(){println("{}",f);}',
            'fn main(){let n=1;let p=n@;println("{}",p@);}',
        )
        for text in sources:
            with self.subTest(source=text):
                error = self.assert_same_frontend_failure(text, "XE-SEM-0001")
                self.assertIn("{} 格式化", error.message)

    def test_only_existing_anonymous_and_pointer_format_specs_are_accepted(self):
        for template in ("{:x}", "{named}", "{0}", "{!r}", "{:?}", "{:08}"):
            with self.subTest(template=template):
                text = 'fn main(){println("' + template + '",42);}'
                error = self.assert_same_frontend_failure(text, "XE-SEM-0001")
                self.assertIn("只支持匿名 {} 与指针 {:p}", error.message)
        self.assert_same_frontend_failure('fn main(){println("{:p}",42);}', "XE-TYPE-0001")

    def test_address_printing_and_explicit_display_method_keep_normal_semantics(self):
        text = '''trait Display{fn display(self:Self@)->str;}
            struct P{} impl Display for P{fn display(self:Self@)->str{"hello"}}
            fn main(){let p<<P{};let number=42;let text<<String::from("owned");
                println("{} {} {} {{}}",p.display(),number@,text@);
                println("{:p}",p@);println("{}",text);}
        '''
        output = self.run_source(text)
        lines = output.splitlines()
        self.assertEqual(lines[0], "hello 42 owned {}")
        self.assertRegex(lines[1], r"^0x[0-9a-f]+$")
        self.assertEqual(lines[2], "owned")
        # 指针打印不消耗资源；最后按值 println 仍消耗 String，不能再使用。
        self.assert_same_frontend_failure('fn main(){let text<<String::from("x");println("{}",text);println("{}",text@);}',
                                          "XE-MOVE-0001")

    def test_format_string_construction_is_a_declared_capability_limit(self):
        self.assert_same_frontend_failure('fn main(){let text<<format("{}",42);}', "XE-SEM-0001")

    def test_ordinary_functions_and_local_values_can_shadow_format(self):
        self.assertEqual(self.run_source('fn format(n:i32)->i32{n+1}fn main(){println("{}",format(41));}'), "42\n")
        self.assertEqual(self.run_source('fn main(){let format=fn(n:i32)->i32{n+1};println("{}",format(41));}'), "42\n")

    def test_other_explicit_capability_boundaries_fail_at_the_frontend(self):
        # 只固定已有规则，不把这些能力追加到本次发布范围。
        cases = (
            ("fn main(){let n = 1.0 % 0.5;}", "XE-TYPE-0004"),
            ("trait Measure{} fn inspect(value:Measure){} fn main(){}", "XE-SEM-0001"),
            ('extern "other" {fn external()->i32;} fn main(){external();}', "XE-FFI-0001"),
            ("fn main(){u8::try_from(1.5);}", "XE-SEM-0001"),
        )
        for text, code in cases:
            with self.subTest(source=text):
                self.assert_same_frontend_failure(text, code)


if __name__ == "__main__":
    unittest.main()
