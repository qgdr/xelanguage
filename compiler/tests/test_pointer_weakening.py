"""可写地址可以浅层降为只读，但不能通过组合类型恢复写权限。

只读权限与 unsafe 风险独立；风险样例只检查，正常地址实际经过 C 执行。
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source

CC = shutil.which("cc") or ""


class PointerWeakeningTests(unittest.TestCase):
    def check(self, text):
        checker = Checker(Source(text), parse_source(text))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        return checker

    def assert_error(self, text, code="XE-TYPE-0001"):
        checker = Checker(Source(text), parse_source(text))
        errors = checker.check()
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, code, errors[0].render())

    def test_writable_pointer_weakens_in_all_value_contexts(self):
        checker = self.check('''
        fn read(p: i32@) -> i32 { p# }
        fn tail(p: i32@[mut]) -> i32@ { p }
        fn explicit(p: i32@[mut]) -> i32@ { return p; }
        fn main() {
            let[mut] value = 42;
            let writable = value@[mut];
            let initialized: i32@ = writable;
            let[mut] assigned: i32@ = value@;
            assigned = writable;
            read(writable); tail(writable); explicit(writable);
        }
        ''')
        self.assertEqual(checker.warnings, [])
        entries = {entry.get("name"): entry for entry in checker.inferred_types.values()
                   if "name" in entry}
        self.assertEqual(entries["initialized"]["type"], "i32@")
        self.assertEqual(entries["assigned"]["type"], "i32@")

    def test_readonly_pointer_never_upgrades_implicitly(self):
        texts = (
            "fn f(p: i32@) { let writable: i32@[mut] = p; }",
            "fn f(p: i32@, q: i32@[mut]) { let[mut] writable = q; writable = p; }",
            "fn write(p: i32@[mut]) {} fn f(p: i32@) { write(p); }",
            "fn f(p: i32@) -> i32@[mut] { p }",
            "fn f(p: i32@) -> i32@[mut] { return p; }",
        )
        for text in texts:
            with self.subTest(text=text):
                self.assert_error(text)

    def test_weakened_binding_cannot_write_or_take_writable_address(self):
        for operation in ("readonly# = 1;", "let writable = readonly#@[mut];"):
            with self.subTest(operation=operation):
                self.assert_error(f'''fn f(p: i32@[mut]) {{
                    let readonly: i32@ = p; {operation}
                }}''', "XE-MUT-0001")

    def test_if_common_type_is_readonly_in_both_branch_orders(self):
        for left, right in (("writable", "readonly"), ("readonly", "writable")):
            with self.subTest(left=left):
                checker = self.check(f'''fn f(writable: i32@[mut], readonly: i32@, choice: bool) {{
                    let selected = if choice {{ {left} }} else {{ {right} }};
                }}''')
                selected = next(entry for entry in checker.inferred_types.values()
                                if entry.get("name") == "selected")
                self.assertEqual(selected["type"], "i32@")
                self.assert_error(f'''fn f(writable: i32@[mut], readonly: i32@, choice: bool) {{
                    let selected = if choice {{ {left} }} else {{ {right} }};
                    selected# = 1;
                }}''', "XE-MUT-0001")

    def test_pointer_container_and_function_arguments_are_invariant(self):
        pairs = (
            ("i32@[mut]@", "i32@@"),
            ("i32@[mut]@[mut]", "i32@@[mut]"),
            ("Array[i32@[mut], 1]", "Array[i32@, 1]"),
            ("Slice[i32@[mut]]", "Slice[i32@]"),
            ("tuple[i32@[mut], i32]", "tuple[i32@, i32]"),
            ("i32@[mut]?", "i32@?"),
            ("fn(i32@[mut]) -> i32", "fn(i32@) -> i32"),
            ("fn() -> i32@[mut]", "fn() -> i32@"),
        )
        for actual, expected in pairs:
            with self.subTest(actual=actual, expected=expected):
                self.assert_error(f"fn f(value: {actual}) {{ let converted: {expected} = value; }}")

    def test_only_outer_pointer_permission_may_weaken(self):
        self.check('''fn f(pointer: i32@[mut]@[mut]) {
            let outer_readonly: i32@[mut]@ = pointer;
            outer_readonly## = 7;
        }''')
        self.assert_error('''fn f(pointer: i32@[mut]@[mut]) {
            let outer_readonly: i32@[mut]@ = pointer;
            outer_readonly# = pointer#;
        }''', "XE-MUT-0001")

    def test_user_generic_arguments_are_invariant(self):
        for declaration, name in (("struct[T] Holder { value: T, }", "Holder"),
                                  ("enum[T] Token { Value[T], Empty, }", "Token")):
            with self.subTest(name=name):
                self.assert_error(f'''{declaration}
                fn consume(value: {name}[i32@]) {{}}
                fn f(value: {name}[i32@[mut]]) {{ consume(value); }}''')

    def test_second_pointer_level_does_not_restore_readonly_pointee(self):
        for outer in ("i32@@", "i32@@[mut]"):
            with self.subTest(outer=outer):
                self.assert_error(f"fn f(pointer: {outer}) {{ pointer## = 1; }}", "XE-MUT-0001")
                self.assert_error(f"fn f(pointer: {outer}) {{ let writable = pointer##@[mut]; }}",
                                  "XE-MUT-0001")

    def test_unsafe_survives_initialization_assignment_return_and_branch(self):
        checker = self.check('''
        fn readonly(p: i32@[mut, unsafe]) -> i32@ { p }
        fn f(p: i32@[mut, unsafe], safe: i32@, choice: bool) {
            let initialized: i32@ = p;
            let[mut] assigned: i32@ = safe;
            assigned = p;
            let result = readonly(p);
            let selected = if choice { safe } else { p };
        }
        ''')
        entries = {entry.get("name"): entry for entry in checker.inferred_types.values()
                   if "name" in entry}
        for name in ("initialized", "assigned", "result", "selected"):
            with self.subTest(name=name):
                self.assertEqual(entries[name]["type"], "i32@[unsafe]")
                self.assertTrue(entries[name]["unsafe"])
        self.assertTrue(checker.functions["readonly"].unsafe_result)

    def test_local_address_warning_survives_readonly_return(self):
        checker = self.check("fn bad() -> i32@ { let[mut] local = 42; local@[mut] }")
        self.assertIn("XE-PTR-0001", [warning.code for warning in checker.warnings])
        self.assertTrue(checker.functions["bad"].unsafe_result)

    @unittest.skipUnless(CC, "需要系统 C 编译器")
    def test_weakened_addresses_execute_without_changing_source_permission(self):
        text = '''fn read(p: i32@) -> i32 { p# }
        fn readonly(p: i32@[mut]) -> i32@ { p }
        fn main() {
            let[mut] first = 10;
            let[mut] second = 20;
            let writable = first@[mut];
            let initialized: i32@ = writable;
            let[mut] assigned: i32@ = initialized;
            assigned = second@[mut];
            let selected = if true { writable } else { assigned };
            writable# = 42;
            println("{} {} {} {}", read(writable), readonly(writable)#, selected#, assigned#);
        }'''
        with tempfile.TemporaryDirectory() as directory:
            source, program = Path(directory) / "source.xe", Path(directory) / "program"
            source.write_text(text, encoding="utf-8")
            build_executable(source, program, cc=CC,
                             extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "42 42 42 20\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
