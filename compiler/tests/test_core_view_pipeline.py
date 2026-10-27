"""Explicit View spelling crosses CLI and cache boundaries.

The public spelling names the existing coordinate-bearing descriptor. These
tests do not grant View parameters/results or a conversion to an element
pointer. P includes writable pointer qualification under DEC-035.
Cached output must not bypass annotations or permission checks.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compiler.driver import build
from compiler.formatting import format_source, semantic_shape
from compiler.project import project_at
from compiler.toolchain import main
from compiler.xe_ast.language_policy import current_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Diagnostic

CC = shutil.which(os.environ.get("CC", "gcc"))
SOURCE = '''fn main()->Unit {
    let values:Array[i32,4]=[10,20,30,40];
    let part:View[i32@]=values⟨1..4⟩[@];
    let other:View[i32@]=part;
    let narrowed:View[i32@]=other⟨2..3⟩;
    println("{} {}",part⟦2⟧#,narrowed⟦2⟧#);
}'''
MUTABLE_SOURCE = '''fn main()->Unit {
    let[mut] values:Array[i32,4]=[10,20,30,40];
    let part:View[i32@[mut]]=values⟨1..4⟩[@[mut]];
    let other:View[i32@[mut]]=part;
    other⟦2⟧#=99;
    let readonly:View[i32@]=values⟨1..4⟩[@];
    println("{} {}",values⟦2⟧,readonly⟦2⟧#);
}'''


class CoreViewPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xe-core-view-pipeline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "main.xe"
        self.source.write_text(SOURCE, encoding="utf-8")
        self.assertEqual(current_profile(), "core")

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(list(map(str, arguments)))
        self.assertEqual(current_profile(), "core")
        return status, stdout.getvalue(), stderr.getvalue()

    def test_explicit_type_survives_public_json_check_and_c_generation(self):
        status, output, error = self.cli("ast", self.source, "-o", "-")
        self.assertEqual(status, 0, error)
        exported = json.loads(output)
        self.assertEqual(exported["schema_version"], 1)
        annotation = exported["ast"]["items"][0]["body"]["statements"][1]["type"]
        self.assertEqual(annotation["kind"], "NamedType")
        self.assertEqual(annotation["path"]["parts"], ["View"])
        self.assertEqual(annotation["arguments"][0]["kind"], "PointerType")
        self.assertEqual(annotation["arguments"][0]["target"]["path"]["parts"], ["i32"])
        self.assertEqual(self.cli("check", self.source)[0], 0)
        status, generated, error = self.cli("emit-c", self.source, "-o", "-")
        self.assertEqual(status, 0, error)
        self.assertIn("size_t lower, upper;", generated)

    def test_missing_annotation_is_rejected_at_every_cli_entry(self):
        self.source.write_text(SOURCE.replace("part:View[i32@]", "part"), encoding="utf-8")
        for command in ("ast", "check", "emit-c", "build", "run"):
            with self.subTest(command=command), patch("compiler.driver.cc_identity") as identity, \
                    patch("compiler.toolchain.subprocess.run") as execute:
                status, _, error = self.cli(command, self.source)
                self.assertEqual(status, 1, error)
                self.assertIn("XE-CORE-", error)
                self.assertIn("类型", error)
                identity.assert_not_called()
                execute.assert_not_called()

    def test_writable_type_attachment_survives_json_format_check_and_c_generation(self):
        self.source.write_text(MUTABLE_SOURCE, encoding="utf-8")
        status, output, error = self.cli("ast", self.source, "-o", "-")
        self.assertEqual(status, 0, error)
        exported = json.loads(output)
        self.assertEqual(exported["schema_version"], 1)
        annotation = exported["ast"]["items"][0]["body"]["statements"][1]["type"]
        self.assertEqual(annotation["kind"], "NamedType")
        self.assertEqual(annotation["path"]["parts"], ["View"])
        pointer = annotation["arguments"][0]
        self.assertEqual(pointer["kind"], "PointerType")
        self.assertEqual([node["name"] for node in pointer["modifiers"]], ["mut"])
        self.assertEqual(self.cli("check", self.source)[0], 0)
        status, generated, error = self.cli("emit-c", self.source, "-o", "-")
        self.assertEqual(status, 0, error)
        self.assertIn("size_t lower, upper;", generated)
        formatted = format_source(MUTABLE_SOURCE, str(self.source))
        self.assertEqual(semantic_shape(parse_source(formatted)),
                         semantic_shape(parse_source(MUTABLE_SOURCE)))
        self.assertEqual(format_source(formatted), formatted)

    def test_permission_mismatch_is_rejected_before_any_cli_build_or_run(self):
        # Dropping permission from P alone does not select a descriptor-wide
        # conversion. Restricting an existing View preserves its complete P.
        invalid = MUTABLE_SOURCE.replace("part:View[i32@[mut]]", "part:View[i32@]")
        self.source.write_text(invalid, encoding="utf-8")
        # The AST command validates reviewed surface shapes, not all type
        # relations. This source is syntactically valid but ill-typed.
        self.assertEqual(self.cli("ast", self.source, "-o", "-")[0], 0)
        for command in ("check", "emit-c", "build", "run"):
            with self.subTest(command=command), patch("compiler.driver.cc_identity") as identity, \
                    patch("compiler.toolchain.subprocess.run") as execute:
                status, _, error = self.cli(command, self.source)
                self.assertEqual(status, 1, error)
                self.assertIn("XE-TYPE-0001", error)
                identity.assert_not_called()
                execute.assert_not_called()

    def test_unconfirmed_type_attachment_is_rejected_at_every_cli_entry(self):
        invalid = MUTABLE_SOURCE.replace("part:View[i32@[mut]]", "part:View[i32@][unsafe]")
        self.source.write_text(invalid, encoding="utf-8")
        for command in ("ast", "check", "emit-c", "build", "run"):
            with self.subTest(command=command), patch("compiler.driver.cc_identity") as identity, \
                    patch("compiler.toolchain.subprocess.run") as execute:
                status, _, error = self.cli(command, self.source)
                self.assertEqual(status, 1, error)
                self.assertIn("XE-PARSE-0001", error)
                identity.assert_not_called()
                execute.assert_not_called()

    @unittest.skipUnless(CC, "view execution requires a system C compiler")
    def test_cache_rechecks_source_and_preserves_previous_artifact_on_rejection(self):
        assert CC is not None
        project, output = project_at(self.root), self.root / "program"
        first = build(project, self.source, output, cc=CC)
        second = build(project, self.source, output, cc=CC)
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "30 30\n", ""))
        previous = output.read_bytes()
        self.source.write_text(SOURCE.replace("part:View[i32@]", "part"), encoding="utf-8")
        with patch("compiler.driver.cc_identity") as identity, \
                patch("compiler.toolchain.subprocess.run") as execute:
            with self.assertRaises(Diagnostic):
                build(project, self.source, output, cc=CC)
            status, _, error = self.cli("run", self.source, "--cc", CC, "-o", output)
            self.assertEqual(status, 1, error)
            identity.assert_not_called()
            execute.assert_not_called()
        self.assertEqual(output.read_bytes(), previous)

    @unittest.skipUnless(CC, "writable view cache execution requires a system C compiler")
    def test_writable_descriptor_copy_runs_and_cache_cannot_bypass_permission_failure(self):
        assert CC is not None
        self.source.write_text(MUTABLE_SOURCE, encoding="utf-8")
        project, output = project_at(self.root), self.root / "writable-view"
        first = build(project, self.source, output, cc=CC)
        second = build(project, self.source, output, cc=CC)
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "99 99\n", ""))
        previous = output.read_bytes()
        invalid = MUTABLE_SOURCE.replace("part:View[i32@[mut]]", "part:View[i32@]")
        self.source.write_text(invalid, encoding="utf-8")
        with patch("compiler.driver.cc_identity") as identity, \
                patch("compiler.toolchain.subprocess.run") as execute:
            with self.assertRaises(Diagnostic) as caught:
                build(project, self.source, output, cc=CC)
            self.assertEqual(caught.exception.code, "XE-TYPE-0001")
            status, _, error = self.cli("run", self.source, "--cc", CC, "-o", output)
            self.assertEqual(status, 1, error)
            self.assertIn("XE-TYPE-0001", error)
            identity.assert_not_called()
            execute.assert_not_called()
        self.assertEqual(output.read_bytes(), previous)

    @unittest.skipUnless(CC, "view sanitizer execution requires a system C compiler")
    def test_explicit_view_runs_under_address_undefined_and_default_leak_sanitizers(self):
        assert CC is not None
        output = self.root / "sanitized"
        build(project_at(self.root), self.source, output, cc=CC, sanitize=True)
        # Do not alter ASAN_OPTIONS/LSAN_OPTIONS: leak checking remains enabled.
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "30 30\n", ""))

    @unittest.skipUnless(CC, "pointer element views require a system C compiler")
    def test_readonly_view_of_writable_pointers_keeps_both_pointer_levels(self):
        # Readonly describes the array slot, not the object that the stored
        # pointer already permits writing. No View-level [mut] is involved.
        assert CC is not None
        self.source.write_text('''fn main()->Unit {
            let[mut] value:i32=10;
            let pointer:i32@[mut]=value@[mut];
            let slots:Array[i32@[mut],1]=[pointer];
            let pointers:View[i32@[mut]@]=slots⟨..⟩[@];
            let slot:i32@[mut]@=pointers⟦0⟧;
            let stored:i32@[mut]=slot#;
            stored#=99;
            println("{} {}",value,slot##);
        }''', encoding="utf-8")
        output = self.root / "pointer-elements"
        build(project_at(self.root), self.source, output, cc=CC, sanitize=True)
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "99 99\n", ""))
