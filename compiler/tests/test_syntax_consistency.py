"""0.9 的类型/值运算对应，以及真正生成可执行文件的验收。

每次语法调整同时验证接受和拒绝的情况，避免只让解析器认识新符号。
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast import parse_source, Diagnostic
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source
from compiler.xe_ast.build import build_executable

CC = shutil.which("cc")


class SyntaxConsistencyTests(unittest.TestCase):
    def assert_ok(self, source):
        errors = check_source(source)
        self.assertEqual(errors, [], "\n".join(e.render() for e in errors))

    def assert_error(self, source, code):
        errors = check_source(source)
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def assert_pointer_warning(self, source):
        checker = Checker(Source(source), parse_source(source))
        self.assertEqual(checker.check(), [])
        self.assertTrue(checker.warnings)
        self.assertTrue(all(warning.severity == "warning" for warning in checker.warnings))
        self.assertTrue(any(warning.code == "XE-PTR-0001" for warning in checker.warnings))

    def test_function_types_support_grouping_and_postfix_composition(self):
        self.assert_ok("fn f(value: (fn(i32) -> i32)?) {}")
        tree = parse_source("fn f(value: (fn(i32) -> i32)@) {}")
        type_ = tree["items"][0]["parameters"][0]["type"]
        self.assertEqual(type_["kind"], "PointerType")
        self.assertEqual(type_["target"]["kind"], "FunctionType")

    def test_tuple_and_block_have_unambiguous_entry(self):
        self.assert_ok("fn f() { let p: tuple[i64, tuple[u8]] = tuple[10, tuple[20]]; let x: i64 = { p.0 }; }")
        for source in ("fn f() { let p = (1, 2); }", "fn f(p: (i32, i32)) {}",
                       "fn f() { let p = {1, 2}; }", "fn f(p: {i32, i32}) {}"):
            with self.assertRaises(Diagnostic) as error:
                parse_source(source)
            self.assertIn("元组", error.exception.message)
        self.assert_error("fn f() { let p: tuple[u8, i64] = tuple[256, 2]; }", "XE-TYPE-0004")
        self.assert_error("fn f() { let p: tuple[i32] = tuple[1, 2]; }", "XE-TYPE-0001")

    def test_unsafe_attachment_keeps_pointer_representation_and_copyability(self):
        self.assert_ok('fn f() { let[mut] x = 1; let p: i32@[unsafe] = x@[unsafe]; println("{}", p#); let q: i32@[unsafe, mut] = x@[mut, unsafe]; q# = 2; }')
        self.assert_error("fn f() { let x = 1; let p: i32@[mut, unsafe] = x@[mut, unsafe]; }", "XE-MUT-0001")
        self.assert_error("fn f(p: i32@[unsafe, unsafe]) {}", "XE-TYPE-0001")
        self.assert_error("fn f(p: i32@[unknown]) {}", "XE-TYPE-0001")
        self.assert_error("fn f(p: RawPtr[u8]) {}", "XE-NAME-0001")
        self.assert_error("fn f(x: i32) { x ?[@[unknown]] { _ :> _ -> 0, }; }", "XE-TYPE-0001")
        self.assert_error("fn f(x: i32) { x ?[@[unsafe, unsafe]] { _ :> _ -> 0, }; }", "XE-TYPE-0001")

    def test_str_borrow_is_descriptor_borrow_and_data_is_byte_borrow(self):
        self.assert_ok('fn f() { let text: str = "hello"; let p: str@ = text@; let data: u8@ = p.data(); println("{} {}", p#, data#); }')
        self.assert_pointer_warning('fn bad() -> str@ { let s: str = "hi"; s@ }')
        self.assert_pointer_warning('fn bad(p: str@[mut]) { let s << String::from("bad"); p# = s.as_str(); }')
        self.assert_pointer_warning('fn bad() { let[mut] outer: str = "ok"; { let s << String::from("bad"); let p: str@[mut] = outer@[mut]; p# = s.as_str(); }; println("{}", outer); }')
        self.assert_pointer_warning('enum E { Text[str], } fn bad(view: str) -> str@ { let e: E << E::Text[view]; e ?[@] { E::Text :> s: str@ -> s, } }')

    def test_user_structs_require_explicit_copy_and_members_must_support_it(self):
        self.assert_error('struct S { x: i32, } fn f() { let a = S { .x = 1; }; }', "XE-OWN-0001")
        self.assert_ok('struct Inner { x: i64, } impl Copy for Inner; struct Outer { p: Inner, } impl Copy for Outer; fn f() { let a = Outer { .p = Inner { .x = 1; }; }; let b = a; println("{} {}", a.p.x, b.p.x); }')
        self.assert_error('struct S { s: String, } fn f() { let a = S { .s << String::from("hi"); }; }', "XE-OWN-0001")
        self.assert_error('struct S { x: i32, } impl Drop for S { fn drop(self: Self@[mut]) {} } fn f() { let a = S { .x = 1; }; }', "XE-OWN-0001")
        self.assert_ok('struct S { view: str, } impl Copy for S; fn f() { let a = S { .view = "hi"; }; let b = a; println("{} {}", a.view, b.view); }')

    def test_match_selects_one_layer_and_second_match_is_explicit(self):
        self.assert_error('enum Inner { Number[i32], End, } enum Outer { Wrap[Inner], } fn f(outer: Outer) -> i32 { outer ? { Outer::Wrap[Inner::Number[0]] :> _ -> 0, } }', "XE-PARSE-0001")
        self.assert_ok('enum Inner { Number[i32], End, } enum Outer { Wrap[Inner], } fn f(outer: Outer) -> i32 { outer ? { Outer::Wrap :> inner -> inner ? { Inner::Number :> number -> number, Inner::End :> _ -> 0, }, } }')

    def test_closures_have_distinct_environment_types_and_move_captures(self):
        self.assert_error('fn f(flag: bool) { let a = 1; let b = 2; let callback << if flag { fn[a](x: i32) -> i32 { a + x } } else { fn[b](x: i32) -> i32 { b + x } }; }', "XE-TYPE-0001")
        self.assert_error('fn f() { let s << String::from("hi"); let callback << fn[s]() { println("{}", s); }; println("{}", s); }', "XE-MOVE-0001")
        self.assert_ok('fn f() { let s << String::from("hi"); let copied << s.clone(); let callback << fn[copied]() { println("{}", copied); }; println("{}", s); callback(); }')
        # 只读环境可重复调用；只有移出资源才使接收者变成一次性拥有调用。
        self.assert_error('fn f() { let a << String::from("x"); let callback << fn[a]() { println("{}", a); }; callback(); callback(); }', "XE-MOVE-0001")

    def test_numeric_conversions_are_explicit_and_lossless_by_default(self):
        self.assert_ok('fn f(x: i32) { let y: i64 = x as i64; let p: tuple[f32, i64] = tuple[-1.5, 10]; let b: u8?[ConversionError] = u8::try_from(x); let w: u8 = u8::try_from(x)?[panic]; }')
        for strategy in ("checked", "wrap"):
            self.assert_error(f'fn f(x: i32) {{ let b = x as[{strategy}] u8; }}', "XE-PARSE-0001")
        self.assert_error('fn f() { let p: tuple[f32] = tuple[1e39,]; }', "XE-TYPE-0004")
        self.assert_ok('fn f() { let p: tuple[f32, f64] = tuple[10, -20]; }')
        self.assert_error('fn f() { let p: tuple[f32] = tuple[16777217,]; }', "XE-TYPE-0004")
        self.assert_error('fn f() { let n: usize = 18446744073709551616; }', "XE-TYPE-0004")
        self.assert_error('fn f(x: i64) { let y: i32 = x as i32; }', "XE-TYPE-0001")
        self.assert_error('fn f(x: i32) { let y: u32 = x as u32; }', "XE-TYPE-0001")
        self.assert_error('fn f(x: i32) { let y: i64 = x; }', "XE-TYPE-0001")

    @unittest.skipUnless(CC, "需要系统 C 编译器")
    def test_new_rules_generate_and_run_executables(self):
        sources = [
            ('fn wrap(value: String?) -> String?? { value } fn main() { let result: String?? << wrap(Maybe::Yes[String::from("owned")]); result ? 1> inner -> { inner ? 1> text -> println("{}", text) 2> _ -> {}; } 2> _ -> {}; }', 'owned\n'),
            ('fn wrap(value: u8?[ConversionError]) -> u8?[ConversionError]? { value } fn main() { let result = wrap(u8::try_from(999)); result ? 1> inner -> { inner ? 1> n -> println("{}", n) 2> _ -> println("inner failure"); } 2> _ -> println("outer failure"); }', 'inner failure\n'),
            ('struct tuple_layout_0 { x: i32, } impl Copy for tuple_layout_0; fn main() { let s = tuple_layout_0 { .x = 1; }; let p: tuple[i32] = tuple[s.x,]; println("{}", p.0); }', '1\n'),
            ('fn main() { let p: tuple[i64, f32] = tuple[10, 1.5]; println("{} {}", p.0, p.1); let x: i32 = 3; let y: i64 = x as i64; println("{}", y); }', '10 1.5\n3\n'),
            ('enum E { Text[str], } fn main() { let[mut] e: E << E::Text["hello"]; e ?[@[mut]] { E::Text :> s: str@[mut] -> { s# = "world"; }, }; e ?[@] { E::Text :> s: str@ -> println("{} {}", s, s.data()#), }; }', 'world 119\n'),
            ('fn main() { let p: tuple[String, String] << tuple[String::from("left"), String::from("right")]; let first << p.0; println("{}", first); }', 'left\n'),
        ]
        for text, expected in sources:
            with self.subTest(source=text), tempfile.TemporaryDirectory() as directory:
                source, program = Path(directory) / "test.xe", Path(directory) / "program"
                source.write_text(text, encoding="utf-8")
                build_executable(source, program, cc=CC,
                                 extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
                result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
