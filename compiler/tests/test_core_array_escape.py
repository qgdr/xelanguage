"""DEC-038: reject known Array storage escapes, never execute bad programs.

The source uses DEC-035 box indexing and result-pointer-typed View.
Temporary pointer escape and resource Array operations remain rejected.
Invalid examples stop at checking/lowering/build boundaries.
The only program executed here keeps its Array alive throughout every access.
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
from compiler.xe_ast.ast import document
from compiler.xe_ast.backend_c import (
    AnnotatedChecker,
    CBackend,
    lower_program_to_c,
    lower_to_c,
)
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.language_policy import language_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Diagnostic, Source

ERROR = "XE-PTR-0004"
CC = shutil.which(os.environ.get("CC", "gcc"))
VIEW_ESCAPE = '''fn main()->Unit {
    let part:View[i32@]={
        let source:Array[i32,2]=[10,20];
        source⟨..⟩[@]
    };
}'''
POINTER_ESCAPE = '''fn main()->Unit {
    let pointer:i32@={
        let source:Array[i32,2]=[10,20];
        source⟦0⟧@
    };
}'''
SAFE = '''fn external(pointer:i32@)->i32@ {
    let pointers:Array[i32@,1]=[pointer];
    pointers⟦0⟧
}
fn from_array(pointer:Array[i32,2]@)->i32@ { pointer#⟦1⟧@ }
fn main()->Unit {
    let values:Array[i32,2]=[10,20];
    let first:i32@=external(values⟦0⟧@);
    let last:i32@=from_array(values@);
    let view:View[i32@]=values⟨..⟩[@];
    println("{} {} {}",first#,last#,view⟦1⟧#);
}'''
SAVE = "fn save(slot:i32@@[mut],pointer:i32@)->Unit{slot#=pointer;}"
SAVE_CALL = '''fn main()->Unit {
    let keep:Array[i32,1]=[1];
    let[mut] saved:i32@=keep⟦0⟧@;
    {
        let short:Array[i32,1]=[2];
        save(saved@[mut],short⟦0⟧@);
    };
}'''


class CoreArrayEscapeTests(unittest.TestCase):
    def rejected(self, text):
        diagnostics = check_source(text, "array-escape.xe")
        self.assertTrue(diagnostics, text)
        self.assertEqual(diagnostics[0].code, ERROR, diagnostics[0].render())
        self.assertEqual(diagnostics[0].severity, "error")
        self.assertIn("Array 来源", diagnostics[0].message)
        self.assertIn("不会自动延长", diagnostics[0].hint or "")
        return diagnostics[0]

    def test_block_result_escape_is_illegal_even_without_a_later_read(self):
        for text in (VIEW_ESCAPE, POINTER_ESCAPE):
            with self.subTest(text=text):
                self.rejected(text)

    def test_explicit_return_and_tail_return_share_the_same_error(self):
        for body in ("return source⟦0⟧@;", "source⟦0⟧@", "return source@;", "source@"):
            with self.subTest(body=body):
                target = "Array[i32,2]@" if "source@" in body else "i32@"
                self.rejected(f"fn escaped()->{target}{{let source:Array[i32,2]=[10,20];{body}}}")
        self.rejected("fn by_value(source:Array[i32,2])->i32@{source⟦0⟧@}")

    def test_transparent_array_alias_and_nested_view_copy_do_not_hide_the_owner(self):
        self.rejected('''type Values=Array[i32,2];
            fn main()->Unit {
                let part:View[i32@]={
                    let source:Values=[10,20];
                    let first:View[i32@]=source⟨..⟩[@];
                    let copied:View[i32@]=first;
                    copied
                };
            }''')

    def test_assignment_to_an_outer_pointer_or_view_is_illegal_without_future_use(self):
        cases = (
            'let[mut] pointer:i32@=outer⟦0⟧@;'
            '{let source:Array[i32,2]=[10,20];pointer=source⟦0⟧@;};',
            'let[mut] part:View[i32@]=outer⟨..⟩[@];'
            '{let source:Array[i32,2]=[10,20];part=source⟨..⟩[@];};',
        )
        for body in cases:
            with self.subTest(body=body):
                self.rejected(f"fn main()->Unit{{let outer:Array[i32,2]=[1,2];{body}}}")

    def test_only_the_ending_storage_is_rejected_not_valid_outer_array_targets(self):
        text = '''fn main()->Unit {
            let source:Array[i32,2]=[10,20];
            let pointer:i32@={let local:i32=1;source⟦0⟧@};
            let part:View[i32@]={let local:i32=1;source⟨..⟩[@]};
            println("{} {}",pointer#,part⟦1⟧#);
        }'''
        source = Source(text)
        checker = Checker(source, parse_source(text))
        self.assertEqual(checker.check(), [])
        self.assertEqual(checker.warnings, [])

    def test_copying_external_pointer_or_text_elements_does_not_copy_the_slot_address(self):
        cases = (
            "fn copy(pointer:i32@)->i32@{let source:Array[i32@,1]=[pointer];source⟦0⟧}",
            "fn copy(pointer:i32@)->i32@{let source:Array[i32@,1]=[pointer];"
            "let part:View[i32@@]=source⟨..⟩[@];part⟦0⟧#}",
            'fn copy()->str{let source:Array[str,1]=["hello"];source⟦0⟧}',
        )
        for text in cases:
            with self.subTest(text=text):
                checker = Checker(Source(text), parse_source(text))
                self.assertEqual(checker.check(), [])
                self.assertEqual(checker.warnings, [])

    def test_scalar_local_escape_uses_the_later_general_lexical_rule(self):
        text = "fn scalar()->i32@{let local:i32=1;local@}"
        self.assertEqual([diagnostic.code for diagnostic in check_source(text)], [ERROR])

    def test_array_pointer_box_escape_is_a_local_box_error_not_an_array_owner(self):
        # The local box contains an Array pointer. Returning its own address
        # is a local pointer-box escape under DEC-039, not an Array owner.
        # Check only: never execute the dangling pointer-to-pointer result.
        text = '''fn pointer_box(source:Array[i32,2]@)->Array[i32,2]@@ {
            let saved:Array[i32,2]@=source;
            saved@
        }'''
        diagnostics = check_source(text)
        self.assertEqual([diagnostic.code for diagnostic in diagnostics], [ERROR])
        self.assertIn("存储来源 saved", diagnostics[0].message)
        self.assertNotIn("Array 来源", diagnostics[0].message)

    def test_explicit_legacy_keeps_array_escape_as_a_warning(self):
        text = "fn escaped()->i32@{let source:Array[i32,2]=[10,20];source[0]@}"
        with language_profile("legacy"):
            checker = Checker(Source(text), parse_source(text))
            self.assertEqual(checker.check(), [])
            self.assertIn("XE-PTR-0001", [diagnostic.code for diagnostic in checker.warnings])

    def test_whole_resource_arrays_do_not_grant_nonowning_element_moves(self):
        whole = check_source('fn main()->Unit{let source:Array[String,1] << [String::from("x")];}')
        self.assertEqual(whole, [])
        blocked = check_source('fn main()->Unit{let source:Array[String,1] << [String::from("x")];source⟦0⟧;}')
        self.assertEqual([diagnostic.code for diagnostic in blocked], ["XE-CORE-0002"])
        moved = check_source('''fn main()->Unit {
            let owner:String << String::from("x");
            let pointer:String@=owner@;
            let stolen:String << pointer#;
        }''')
        self.assertEqual([diagnostic.code for diagnostic in moved], ["XE-MOVE-0002"])

    def test_unknown_external_pointer_storage_requires_a_writeback_contract(self):
        # No subsequent read is required. The unresolved effect is present in
        # save's body, so unused declarations and forwarding cannot hide it.
        cases = (
            SAVE + "fn main()->Unit{unit}",
            SAVE + SAVE_CALL,
            SAVE + SAVE_CALL.replace("    };", '    };println("{}",saved#);'),
            SAVE + "fn relay(slot:i32@@[mut],pointer:i32@)->Unit{save(slot,pointer);}",
        )
        for text in cases:
            with self.subTest(text=text):
                diagnostics = check_source(text)
                self.assertTrue(diagnostics)
                self.assertEqual(diagnostics[0].code, "XE-CORE-0002")
                self.assertIn("写回/留存摘要", diagnostics[0].message)

    def test_repeated_dereference_through_array_contents_is_not_a_local_slot(self):
        # The first # reads a pointer stored in this by-value Array; the second
        # # reaches its external target. Retaining the Array UID is not proof
        # that the written pointer box belongs to the local Array.
        text = "fn save(slots:Array[i32@@[mut],1],pointer:i32@)->Unit{slots⟦0⟧#=pointer;}"
        diagnostics = check_source(text)
        self.assertEqual([diagnostic.code for diagnostic in diagnostics], ["XE-CORE-0002"])
        self.assertIn("写回/留存摘要", diagnostics[0].message)

    def test_legacy_pointer_output_parameter_keeps_its_existing_contract(self):
        with language_profile("legacy"):
            checker = Checker(Source(SAVE), parse_source(SAVE))
            self.assertEqual(checker.check(), [])

    def test_pointer_reads_integer_writes_and_known_local_alias_stores_remain_open(self):
        text = '''fn read(slot:i32@@)->i32{slot##}
            fn write(slot:i32@[mut],value:i32)->Unit{slot#=value;}
            fn main()->Unit {
                let[mut] keep:Array[i32,2]=[1,2];
                let[mut] saved:i32@=keep⟦0⟧@;
                let alias:i32@@[mut]=saved@[mut];
                alias#=keep⟦1⟧@;
                let[mut] slots:Array[i32@,1]=[keep⟦0⟧@];
                slots⟦0⟧=keep⟦1⟧@;
                let[mut] nested:Array[Array[i32@,1],1]=[[keep⟦0⟧@]];
                nested⟦0⟧⟦0⟧=keep⟦1⟧@;
                let n:i32=read(saved@);
                write(keep⟦0⟧@[mut],n);
            }'''
        checker = Checker(Source(text), parse_source(text))
        self.assertEqual(checker.check(), [])
        # Reading an unknown external pointer slot is compilable but cannot
        # claim known target provenance; DEC-039 does not prove it safe.
        self.assertIn("XE-PTR-0003", [warning.code for warning in checker.warnings])

    def test_known_local_alias_store_merges_the_new_array_source_for_escape_checking(self):
        self.rejected('''fn main()->Unit {
            let keep:Array[i32,1]=[1];
            let[mut] saved:i32@=keep⟦0⟧@;
            {
                let short:Array[i32,1]=[2];
                let alias:i32@@[mut]=saved@[mut];
                alias#=short⟦0⟧@;
            };
        }''')

    def test_removed_call_indexing_and_temporary_escape_do_not_gain_a_fallback(self):
        cases = (
            ("fn make()->Array[i32,2]{[10,20]}fn main()->Unit{let p:i32@=make()(0);}", "XE-CORE-0001"),
            ("fn make()->Array[i32,2]{[10,20]}fn main()->Unit{let p:i32@=make()⟦0⟧@;}", "XE-PTR-0004"),
        )
        for text, code in cases:
            with self.subTest(text=text):
                self.assertEqual([diagnostic.code for diagnostic in check_source(text)], [code])

    def test_direct_ast_checker_and_public_c_lowering_reject_without_mutation(self):
        source = Source(VIEW_ESCAPE, "array-escape.xe")
        tree = parse_source(source.text, source.filename)
        original = copy.deepcopy(tree)
        # JSON describes syntax; semantic rejection belongs to the checker.
        self.assertEqual(document(source, tree)["ast"], tree)
        checker = AnnotatedChecker(source, tree)
        self.assertEqual([diagnostic.code for diagnostic in checker.check()], [ERROR])
        with self.assertRaises(Diagnostic) as caught:
            CBackend(checker).generate()
        self.assertEqual(caught.exception.code, ERROR)
        with self.assertRaises(Diagnostic) as caught:
            lower_program_to_c(source, tree)
        self.assertEqual(caught.exception.code, ERROR)
        with self.assertRaises(Diagnostic) as caught:
            lower_to_c(source.text, source.filename)
        self.assertEqual(caught.exception.code, ERROR)
        self.assertEqual(tree, original)

    def test_backend_guard_does_not_reject_unrelated_pointer_warnings(self):
        # Only compile: an explicit unsafe input still follows the separate
        # warning policy, not the new error for a known ending local target.
        text = "fn read(pointer:i32@[unsafe])->i32{pointer#}fn main()->Unit{unit}"
        source = Source(text)
        checker = AnnotatedChecker(source, parse_source(text))
        self.assertEqual(checker.check(), [])
        self.assertTrue(checker.warnings)
        self.assertIn("int main(", CBackend(checker).generate())


class CoreArrayEscapeToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xe-array-escape-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def file(self, name, text):
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main([str(argument) for argument in arguments])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_check_emit_c_build_and_run_reject_before_any_system_compiler_or_execution(self):
        source = self.file("escape.xe", VIEW_ESCAPE)
        for command in ("check", "emit-c", "build", "run"):
            output = self.directory / ("generated.c" if command == "emit-c" else "program")
            options = () if command == "check" else ("-o", output)
            with self.subTest(command=command), \
                    patch("compiler.driver.compile_generated") as compile_c, \
                    patch("compiler.toolchain.subprocess.run") as execute:
                status, _, stderr = self.cli(command, source, "--message-format=json", *options)
                self.assertEqual(status, 1, stderr)
                self.assertEqual(json.loads(stderr)["diagnostics"][0]["code"], ERROR)
                compile_c.assert_not_called()
                execute.assert_not_called()
            self.assertFalse(output.exists())

    def test_cli_rejects_unknown_pointer_writeback_before_build_or_execution(self):
        source = self.file("save.xe", SAVE + SAVE_CALL)
        for command in ("check", "emit-c", "build", "run"):
            output = self.directory / "blocked-output"
            options = () if command == "check" else ("-o", output)
            with self.subTest(command=command), \
                    patch("compiler.driver.compile_generated") as compile_c, \
                    patch("compiler.toolchain.subprocess.run") as execute:
                status, _, stderr = self.cli(command, source, "--message-format=json", *options)
                self.assertEqual(status, 1, stderr)
                diagnostic = json.loads(stderr)["diagnostics"][0]
                self.assertEqual(diagnostic["code"], "XE-CORE-0002")
                self.assertIn("写回/留存摘要", diagnostic["message"])
                compile_c.assert_not_called()
                execute.assert_not_called()
            self.assertFalse(output.exists())

    @unittest.skipUnless(CC, "cache evidence needs GCC/Clang, but never runs the invalid executable")
    def test_a_legacy_cached_executable_cannot_bypass_the_new_core_error(self):
        # Shared syntax deliberately reaches the lifetime rule under both
        # profiles. Build only under legacy; the dangling result is never read
        # or executed, even if the same output has an existing cache receipt.
        text = "fn main()->Unit{let p:Array[i32,2]@={let a:Array[i32,2]=[10,20];a@};}"
        source = self.file("cached.xe", text)
        output = self.directory / "program"
        arguments = ("build", source, "-o", output, "--cc", CC, "--message-format=json")
        first = self.cli("--language-profile=legacy", *arguments)
        self.assertEqual(first[0], 0, first[2])
        cached = self.cli("--language-profile=legacy", *arguments)
        self.assertEqual(cached[0], 0, cached[2])
        self.assertTrue(json.loads(cached[1])["cached"])
        previous = output.read_bytes()
        with patch("compiler.driver.compile_generated") as compile_c, \
                patch("compiler.toolchain.subprocess.run") as execute:
            for command in ("build", "run"):
                result = self.cli(command, source, "-o", output, "--message-format=json")
                self.assertEqual(result[0], 1, result[2])
                self.assertEqual(json.loads(result[2])["diagnostics"][0]["code"], ERROR)
            compile_c.assert_not_called()
            execute.assert_not_called()
        self.assertEqual(output.read_bytes(), previous)

    @unittest.skipUnless(CC, "safe positive execution needs GCC/Clang")
    def test_safe_array_and_external_pointer_sources_still_compile_and_run(self):
        assert CC is not None
        source = self.file("safe.xe", SAFE)
        output = self.directory / "safe"
        warnings = []
        build_executable(source, output, cc=CC, warnings=warnings)
        self.assertEqual(warnings, [])
        completed = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "10 20 20\n")
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
