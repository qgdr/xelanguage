"""普通指针与非阻断风险提示。

悬垂例只检查/编译，从不解引用执行；正常别名例实际编译运行。
unsafe 是数据流注记，不是免除类型、写权限或资源所有权检查的许可。
"""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.cli import main
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source
from compiler.xe_ast.typesys import Type, has_unsafe, ptr

CC = shutil.which("cc")


class PointerWarningsTests(unittest.TestCase):
    def check(self, text):
        checker = Checker(Source(text), parse_source(text))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(e.render() for e in errors))
        return checker

    def warning(self, text, code):
        checker = self.check(text)
        self.assertIn(code, [w.code for w in checker.warnings])
        self.assertTrue(all(w.severity == "warning" for w in checker.warnings))
        self.assertTrue(any(e["unsafe"] for e in checker.inferred_types.values()))
        return checker

    def test_pointer_aliases_are_not_exclusive_loans(self):
        checker = self.check('fn f() { let[mut] x = 1; let p = x@; let q = x@[mut]; q# = 2; println("{}", p#); }')
        self.assertEqual(checker.warnings, [])

    def test_mutable_pointer_can_be_passed_repeatedly(self):
        checker = self.check('fn increase(p: i32@[mut]) { p# = p# + 1; } fn main() { let[mut] x = 0; let p = x@[mut]; increase(p); increase(p); println("{}", p#); }')
        self.assertEqual(checker.warnings, [])

    def test_multiple_pointer_levels_follow_address_operator(self):
        checker = self.check('fn main() { let x = 42; let p: i32@ = x@; let pp: i32@@ = p@; println("{}", pp##); }')
        self.assertEqual(checker.warnings, [])

    def test_unsafe_does_not_change_storage_type_or_copy(self):
        self.assertEqual(ptr(Type("i32")), ptr(Type("i32"), unsafe=True))
        checker = self.check('fn f(p: i32@[mut, unsafe]) { let q: i32@[mut] = p; let r = q; }')
        entries = {e.get("name"): e for e in checker.inferred_types.values() if "name" in e}
        self.assertEqual(entries["q"]["type"], "i32@[mut, unsafe]")
        self.assertTrue(entries["r"]["unsafe"])

    def test_unsafe_does_not_grant_write_permission(self):
        text = "fn f(p: i32@[unsafe]) { p# = 1; }"
        checker = Checker(Source(text), parse_source(text))
        self.assertEqual(checker.check()[0].code, "XE-MUT-0001")

    def test_unsafe_does_not_grant_resource_ownership(self):
        text = "fn consume(s: String) {} fn f(p: String@[mut, unsafe]) { consume(p#); }"
        checker = Checker(Source(text), parse_source(text))
        self.assertEqual(checker.check()[0].code, "XE-MOVE-0002")

    def test_local_address_return_is_warning(self):
        checker = self.warning("fn bad() -> i32@ { let x = 42; x@ } fn f() { let p = bad(); }", "XE-PTR-0001")
        self.assertTrue(checker.functions["bad"].unsafe_result)
        self.assertTrue(next(e for e in checker.inferred_types.values() if e.get("name") == "p")["unsafe"])

    def test_explicit_return_and_forwarding_preserve_risk(self):
        checker = self.warning("fn bad() -> i32@ { let x = 1; return x@; } fn identity(p: i32@) -> i32@ { p } fn f() { let p = bad(); let q: i32@ = identity(p); }", "XE-PTR-0001")
        entry = next(e for e in checker.inferred_types.values() if e.get("name") == "q")
        self.assertEqual(entry["type"], "i32@[unsafe]")

    def test_view_return_is_warning(self):
        self.warning('fn bad() -> str { let s << String::from("x"); s.as_str() }', "XE-PTR-0001")

    def test_outer_assignment_keeps_warning_after_inner_scope(self):
        checker = self.warning('fn main() { let[mut] p: i32@; { let x = 1; p = x@; }; let q = p; }', "XE-PTR-0001")
        self.assertTrue(next(e for e in checker.inferred_types.values() if e.get("name") == "q")["unsafe"])

    def test_moving_owner_warns_without_stopping_compilation(self):
        checker = self.warning('fn consume(s: String) {} fn f() { let s << String::from("x"); let p = s@; consume(s); let q = p; }', "XE-PTR-0002")
        self.assertTrue(next(e for e in checker.inferred_types.values() if e.get("name") == "p")["unsafe"])

    def test_resource_replacement_warns_for_old_view(self):
        self.warning('fn f() { let[mut] s << String::from("old"); let view = s.as_str(); s << String::from("new"); println("{}", view); }', "XE-PTR-0002")

    def test_new_pointer_after_replacement_is_not_tainted(self):
        checker = self.check('fn f() { let[mut] s << String::from("old"); s << String::from("new"); let fresh = s@; }')
        fresh = next(e for e in checker.inferred_types.values() if e.get("name") == "fresh")
        self.assertFalse(fresh["unsafe"])

    def test_temporary_view_is_warning(self):
        self.warning('fn bad() -> str { String::from("x").as_str() }', "XE-PTR-0003")

    def test_unsafe_annotation_survives_branch_merge(self):
        checker = self.check('fn f(a: i32@, b: i32@[unsafe], choose: bool) { let p: i32@ = if choose { a } else { b }; }')
        self.assertTrue(next(e for e in checker.inferred_types.values() if e.get("name") == "p")["unsafe"])

    def test_wrapping_pointer_does_not_erase_risk(self):
        checker = self.check('fn f(p: i32@[unsafe]) { let wrapped: i32@? = Maybe::Yes[p]; let q = wrapped? 1> value -> value 2> _ -> p; }')
        self.assertTrue(next(e for e in checker.inferred_types.values() if e.get("name") == "q")["unsafe"])

    def test_user_enum_payload_and_struct_field_preserve_risk(self):
        texts = [
            'enum E { Pointer[i32@], Empty, } impl Copy for E; fn f(p: i32@[unsafe]) -> i32@ { let e = E::Pointer[p]; e? { E::Pointer :> value -> value, E::Empty :> _ -> panic("empty"), } }',
            'struct W { pointer: i32@, } fn f(p: i32@[unsafe]) -> i32@ { let w << W { p >> .pointer; }; w.pointer }',
        ]
        for text in texts:
            with self.subTest(text=text):
                checker = self.check(text)
                self.assertTrue(checker.functions["f"].unsafe_result)

    def test_inline_and_named_pipeline_preserve_risk(self):
        texts = [
            'fn f(p: i32@[unsafe]) -> i32@ { p |> q: i32@ -> q }',
            'fn identity(p: i32@) -> i32@ { p } fn f(p: i32@[unsafe]) -> i32@ { p |> identity }',
        ]
        for text in texts:
            with self.subTest(text=text):
                self.assertTrue(self.check(text).functions["f"].unsafe_result)

    def test_pointer_loaded_from_unsafe_pointer_preserves_risk(self):
        checker = self.check('fn f(p: (i32@)@[unsafe]) -> i32@ { p# }')
        self.assertTrue(checker.functions["f"].unsafe_result)

    def test_function_value_factory_preserves_risky_return_signature(self):
        checker = self.warning('fn bad() -> i32@ { let x = 1; x@ } fn factory() -> fn() -> i32@ { bad } fn f() { let callable = factory(); let p = callable(); }', "XE-PTR-0001")
        self.assertTrue(next(e for e in checker.inferred_types.values() if e.get("name") == "p")["unsafe"])

    def test_string_append_warns_for_existing_data_view(self):
        self.warning('fn f() { let[mut] s << String::from("a"); let view = s.as_str(); s.push_str("b"); println("{}", view); }', "XE-PTR-0002")

    def test_external_function_value_cannot_erase_pointer_risk(self):
        checker = self.warning('extern "C" { fn address() -> i32@; } fn f() { let p = (address)(); }', "XE-PTR-0003")
        self.assertTrue(next(e for e in checker.inferred_types.values() if e.get("name") == "p")["unsafe"])

    def test_no_capture_function_value_keeps_its_own_risky_return(self):
        checker = self.warning('fn main() { let callable = fn() -> i32@ { let local = 1; local@ }; let p = callable(); }', "XE-PTR-0001")
        self.assertTrue(next(e for e in checker.inferred_types.values() if e.get("name") == "p")["unsafe"])

    def test_warning_is_not_raised_by_c_lowering(self):
        warnings = []
        generated = lower_to_c("fn bad() -> i32@ { let x = 1; x@ } fn main() {}", warnings=warnings)
        self.assertIn("Generated by Xe", generated)
        self.assertIn("XE-PTR-0001", [w.code for w in warnings])

    def test_json_check_shows_warning_and_inferred_type_with_success_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "warning.xe"
            source.write_text("fn bad() -> i32@ { let x = 1; x@ } fn main() { let p = bad(); }", encoding="utf-8")
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = main([str(source), "--check", "--diagnostic-format", "json"])
            self.assertEqual(status, 0)
            report = json.loads(err.getvalue())
            self.assertEqual(report["diagnostics"][0]["severity"], "warning")
            self.assertTrue(any(e["type"] == "i32@[unsafe]" for e in report["inferred_types"]))

    @unittest.skipUnless(CC, "需要系统 C 编译器")
    def test_build_with_warning_succeeds_without_running_invalid_access(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "warning.xe", Path(directory) / "program"
            source.write_text("fn bad() -> i32@ { let x = 1; x@ } fn main() {}", encoding="utf-8")
            warnings = []
            build_executable(source, output, cc=CC, warnings=warnings)
            self.assertTrue(output.exists())
            self.assertTrue(warnings)

    @unittest.skipUnless(CC, "需要系统 C 编译器")
    def test_plain_pointer_aliases_and_multiple_levels_execute(self):
        text = '''fn increase(p: i32@[mut]) { p# = p# + 1; }
        fn main() {
            let[mut] x = 0;
            let p: i32@[mut] = x@[mut];
            let alias: i32@[mut] = p;
            increase(p); increase(p); increase(alias);
            let pp: i32@[mut]@ = p@;
            println("{} {}", x, pp##);
        }'''
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "plain.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            warnings = []
            build_executable(source, output, cc=CC, warnings=warnings)
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "3 3\n")
            self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
