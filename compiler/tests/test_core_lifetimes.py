"""DEC-039: conservative lexical target lifetimes, not Rust borrowing.

Reject bad storage at the save/escape boundary, without executing any rejected
program. A short pointer variable may still copy its long-lived target address.
External parameter roots and the local parameter boxes are deliberately distinct.
"""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compiler.toolchain import main
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend, lower_program_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source

ERROR = "XE-PTR-0004"
CC = shutil.which(os.environ.get("CC", "gcc"))
SAFE = '''fn identity(pointer:i32@)->i32@ { pointer }
fn main()->Unit {
    let target:i32=42;
    let[mut] result:i32@=target@;
    {
        let short_pointer:i32@=target@;
        let copied:i32@=short_pointer;
        result=identity(copied);
    };
    println("{}",result#);
}'''
SHORT_STORE = '''fn main()->Unit {
    let outside:i32=0;
    let[mut] result:i32@=outside@;
    {
        let short_target:i32=42;
        result=short_target@;
        result=outside@;
    };
}'''


class CoreLifetimeTests(unittest.TestCase):
    def checked(self, text):
        source = Source(text, "lifetimes.xe")
        tree = parse_source(text, source.filename)
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        diagnostics = checker.check()
        self.assertEqual(diagnostics, [], "\n".join(diagnostic.render() for diagnostic in diagnostics))
        self.assertEqual(checker.warnings, [])
        self.assertEqual(tree, original)
        return checker, tree

    def rejected(self, text):
        diagnostics = check_source(text, "lifetimes.xe")
        self.assertTrue(diagnostics, text)
        self.assertEqual(diagnostics[0].code, ERROR, diagnostics[0].render())
        self.assertEqual(diagnostics[0].severity, "error")
        return diagnostics[0]

    def test_short_pointer_variable_can_copy_an_address_to_a_long_lived_target(self):
        self.checked(SAFE)

    def test_independent_copy_values_do_not_inherit_the_source_box_lifetime(self):
        self.checked("fn main()->Unit{let value:i32={let short:i32=42;short};println(\"{}\",value);}")

    def test_short_target_cannot_be_saved_outside_even_if_overwritten_without_reading(self):
        diagnostic = self.rejected(SHORT_STORE)
        self.assertEqual(SHORT_STORE[diagnostic.start:diagnostic.end], "result")
        self.assertIn("short_target", diagnostic.message)

    def test_nested_block_result_cannot_escape_a_scalar_target(self):
        self.rejected("fn main()->Unit{let pointer:i32@={let target:i32=42;target@};}")

    def test_returning_local_target_address_is_an_error_for_tail_and_return(self):
        for body in ("target@", "return target@;", "(target@)"):
            with self.subTest(body=body):
                self.rejected(f"fn invalid()->i32@{{let target:i32=42;{body}}}")

    def test_pointer_parameter_value_is_external_but_parameter_box_is_local(self):
        self.checked("fn identity(pointer:i32@)->i32@{pointer}")
        for body in ("pointer@", "return pointer@;"):
            with self.subTest(body=body):
                self.rejected(f"fn invalid(pointer:i32@)->i32@@{{{body}}}")
        self.rejected("fn invalid(value:i32)->i32@{value@}")

    def test_pointer_box_address_is_not_confused_with_its_long_target(self):
        self.rejected('''fn main()->Unit {
            let target:i32=42;
            let outer:i32@=target@;
            let[mut] result:i32@@=outer@;
            {let short_pointer:i32@=target@;result=short_pointer@;};
        }''')

    def test_a_local_pointer_alias_may_not_store_a_short_target_into_outer_box(self):
        self.rejected('''fn main()->Unit {
            let target:i32=0;
            let[mut] result:i32@=target@;
            {
                let short:i32=42;
                let alias:i32@@[mut]=result@[mut];
                alias#=short@;
            };
        }''')

    def test_same_scope_or_inner_destination_can_hold_the_outer_address(self):
        self.checked('''fn main()->Unit {
            let first:i32=1;
            let second:i32=2;
            let[mut] result:i32@=first@;
            result=second@;
            {let inner:i32@=result;println("{}",inner#);};
        }''')

    def test_storing_a_short_target_in_a_long_array_pointer_slot_is_an_error(self):
        self.rejected('''fn main()->Unit {
            let outer:i32=0;
            let[mut] pointers:Array[i32@,1]=[outer@];
            {let short:i32=42;pointers⟦0⟧=short@;};
        }''')

    def test_returned_parameter_target_is_checked_at_the_callers_destination(self):
        self.rejected('''fn identity(pointer:i32@)->i32@{pointer}
        fn main()->Unit {
            let outer:i32=0;
            let[mut] result:i32@=outer@;
            {let short:i32=42;result=identity(short@);};
        }''')

    def test_one_short_possibility_is_not_erased_by_a_long_possibility(self):
        self.rejected('''fn choose(flag:bool)->Unit {
            let outer:i32=0;
            let result:i32@={
                let short:i32=42;
                if flag {outer@} else {short@}
            };
        }''')

    def test_unsafe_type_annotation_does_not_suppress_a_known_local_error(self):
        self.rejected("fn main()->Unit{let pointer:i32@[unsafe]={let local:i32=1;local@};}")

    def test_unknown_external_pointer_store_stays_closed_until_effects_are_defined(self):
        text = "fn save(slot:i32@@[mut],pointer:i32@)->Unit{slot#=pointer;}"
        self.assertEqual([diagnostic.code for diagnostic in check_source(text)], ["XE-CORE-0002"])

    def test_legacy_keeps_the_preexisting_warning_policy(self):
        text = "fn local()->i32@{let value=42;value@}fn main(){}"
        with language_profile("legacy"):
            checker = Checker(Source(text), parse_source(text))
            self.assertEqual(checker.check(), [])
            self.assertIn("XE-PTR-0001", [warning.code for warning in checker.warnings])

    def test_direct_ast_consumers_cannot_lower_a_known_short_store(self):
        source = Source(SHORT_STORE, "lifetimes.xe")
        tree = parse_source(source.text, source.filename)
        original = copy.deepcopy(tree)
        checker = AnnotatedChecker(source, tree)
        self.assertEqual([diagnostic.code for diagnostic in checker.check()], [ERROR])
        with self.assertRaises(Diagnostic) as caught:
            CBackend(checker).generate()
        self.assertEqual(caught.exception.code, ERROR)
        with self.assertRaises(Diagnostic) as caught:
            lower_program_to_c(source, tree)
        self.assertEqual(caught.exception.code, ERROR)
        self.assertEqual(tree, original)


class CoreLifetimeToolTests(unittest.TestCase):
    def test_cli_checks_short_store_before_compiling_or_launching(self):
        with tempfile.TemporaryDirectory(prefix="xe-lifetime-reject-") as directory:
            source = Path(directory) / "bad.xe"
            source.write_text(SHORT_STORE, encoding="utf-8")
            for command in ("check", "emit-c", "build", "run"):
                output = Path(directory) / "blocked"
                options = [] if command == "check" else ["-o", str(output)]
                stdout, stderr = io.StringIO(), io.StringIO()
                with self.subTest(command=command), contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr), \
                        patch("compiler.driver.compile_generated") as compile_c, \
                        patch("compiler.toolchain.subprocess.run") as execute:
                    status = main([command, str(source), "--message-format=json", *options])
                self.assertEqual(status, 1, stderr.getvalue())
                self.assertEqual(json.loads(stderr.getvalue())["diagnostics"][0]["code"], ERROR)
                compile_c.assert_not_called()
                execute.assert_not_called()
                self.assertFalse(output.exists())

    @unittest.skipUnless(CC, "safe lexical-lifetime execution requires GCC/Clang")
    def test_safe_target_lifetime_compiles_and_runs(self):
        assert CC is not None
        with tempfile.TemporaryDirectory(prefix="xe-lifetime-safe-") as directory:
            source, output = Path(directory) / "safe.xe", Path(directory) / "safe"
            source.write_text(SAFE, encoding="utf-8")
            warnings = []
            build_executable(source, output, cc=CC, warnings=warnings)
            self.assertEqual(warnings, [])
            result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "42\n")
            self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
