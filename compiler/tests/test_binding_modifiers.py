"""可变声明的公开附件与隐藏别名：解析、语义、真正编译执行。"""
import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast import Diagnostic, parse_source
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.cli import main
from compiler.xe_ast.semantic import check_source

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which("cc") or ""


def shape(value):
    """源码位置随拼写变化，除此之外 AST 应当完全相同。"""
    if isinstance(value, dict):
        return {key: shape(child) for key, child in value.items() if key != "span"}
    if isinstance(value, list):
        return [shape(child) for child in value]
    return value


class BindingModifierTests(unittest.TestCase):
    def test_parameter_mutability_syntax_is_rejected_in_every_context(self):
        for parameter in ("let[mut] index: i32", "let index: i32", "var index: i32", "index[mut]: i32"):
            for source in (f"fn f({parameter}) {{}}",
                           f"fn main() {{ let callback = fn({parameter}) {{}}; }}",
                           f"fn main() {{ 1 |> [{parameter}] -> 0; }}"):
                with self.subTest(source=source):
                    with self.assertRaises(Diagnostic) as caught:
                        parse_source(source)
                    self.assertEqual(caught.exception.code, "XE-PARSE-0001")
                    self.assertIn("参数", caught.exception.message)

    def test_parameter_binding_readonly_does_not_make_mutable_pointee_readonly(self):
        positive = '''fn write(pointer: i32@[mut]) { pointer# = 9; }
        fn main() { let[mut] value = 1; write(value@[mut]); println("{}", value); }'''
        self.assertEqual(check_source(positive, check_borrows=True), [])
        rebind = "fn f(pointer: i32@[mut], other: i32@[mut]) { pointer = other; }"
        self.assertEqual(check_source(rebind, check_borrows=True)[0].code, "XE-MUT-0001")
        direct = "fn f(index: i32) { index = index + 1; }"
        self.assertEqual(check_source(direct)[0].code, "XE-MUT-0001")
        self.assertEqual(check_source(direct, check_borrows=True)[0].code, "XE-MUT-0001")

    @unittest.skipUnless(CC, "运行验收需要系统 C 编译器")
    def test_shadowed_resource_parameter_transfers_ownership(self):
        source_text = '''fn append(text: String) -> String {
            let[mut] text << text;
            text.push_str("!");
            text
        }
        fn main() {
            let input << String::from("hello");
            let output << append(input);
            println("{}", output);
        }'''
        with tempfile.TemporaryDirectory() as directory:
            source, program = Path(directory) / "test.xe", Path(directory) / "program"
            source.write_text(source_text, encoding="utf-8")
            build_executable(source, program, True, CC)
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "hello!\n")
        self.assertEqual(result.stderr, "")
        copied = source_text.replace("let[mut] text << text;", "let[mut] text = text;")
        self.assertEqual(check_source(copied, check_borrows=True)[0].code, "XE-OWN-0001")

    def test_default_mode_requires_mutability_annotations(self):
        text = (ROOT / "tests/language/mutable_permissions.xe").read_text()
        self.assertEqual(check_source(text), [])
        invalid = "fn f(x: i32) { x = 1; }"
        self.assertEqual(check_source(invalid)[0].code, "XE-MUT-0001")

    def test_new_and_old_safety_options_are_equivalent(self):
        source = ROOT / "tests/language/mutable_permissions.xe"
        statuses = []
        for flags in ([], ["--check-safety"], ["--check-borrows"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                statuses.append(main([str(source), "--check", *flags]))
        self.assertEqual(statuses, [0, 0, 0])

    def test_default_mode_does_not_upgrade_shared_pointer_permissions(self):
        source = '''fn write(p: i32@[mut]) { p# = 8; }
        fn main() { let x = 1; let p: i32@[mut] = x@; write(p); }'''
        self.assertEqual(check_source(source)[0].code, "XE-TYPE-0001")
        self.assertEqual(check_source(source, check_borrows=True)[0].code, "XE-TYPE-0001")

    def test_handler_parameters_use_local_mutable_bindings(self):
        source = "fn main() { 1 |> value: i32 -> { let[mut] value = value; value = 2; value }; }"
        tree = parse_source(source)
        handler = tree["items"][0]["body"]["statements"][0]["expression"]["handler"]
        self.assertFalse(handler["parameters"][0]["mutable"])
        self.assertEqual(check_source(source, check_borrows=True), [])
        self.assertRaises(Diagnostic, parse_source, "fn main() { 1 |> value[mut]: i32 -> value; }")

    @unittest.skipUnless(CC, "运行验收需要系统 C 编译器")
    def test_default_mode_executes_mutable_pointer(self):
        text = (ROOT / "tests/language/mutable_permissions.xe").read_text()
        annotated = '''fn write(p: i32@[mut]) { p# = 8; }
        fn main() { let[mut] x = 1; let p: i32@[mut] = x@[mut]; write(p); println("{}", x); }'''
        for source_text, expected in ((text, "9 hello, world\n"), (annotated, "8\n")):
            with tempfile.TemporaryDirectory() as directory:
                source, program = Path(directory) / "test.xe", Path(directory) / "program"
                source.write_text(source_text, encoding="utf-8")
                build_executable(source, program, False, CC)
                result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)
            self.assertEqual(result.stderr, "")

    def test_alias_and_public_syntax_have_identical_ast(self):
        public = '''fn advance(index: i32) -> i32 {
            let[mut] total: i32;
            total = index;
            total = total + 1;
            total
        }'''
        alias = public.replace("let[mut]", "var")
        self.assertEqual(shape(parse_source(public)), shape(parse_source(alias)))
        function = parse_source(public)["items"][0]
        self.assertFalse(function["parameters"][0]["mutable"])
        binding = function["body"]["statements"][0]
        self.assertTrue(binding["mutable"])
        span = binding["span"]
        self.assertEqual(public[span["start"]["offset"]:span["end"]["offset"]], "let[mut] total: i32;")

    def test_plain_let_and_parameters_remain_immutable(self):
        fn = parse_source("fn f(x: i32) { let value = x; }")["items"][0]
        self.assertFalse(fn["parameters"][0]["mutable"])
        self.assertFalse(fn["body"]["statements"][0]["mutable"])

    def test_attachment_whitespace_and_anonymous_parameter(self):
        tree = parse_source("fn f() { let [ mut ] x = 1; let callback = fn(n: i32) -> i32 { let[mut] n = n; n = n + 1; n }; }")
        statements = tree["items"][0]["body"]["statements"]
        self.assertTrue(statements[0]["mutable"])
        self.assertFalse(statements[1]["value"]["parameters"][0]["mutable"])

    def test_invalid_attachments_report_source_diagnostic(self):
        for prefix in ("let[]", "let[mutable]", "let[mut,mut]", "let[mut,]", "let[mut mut]", "let[mut"):
            for source in (f"fn f() {{ {prefix} x = 0; }}",):
                with self.subTest(source=source):
                    with self.assertRaises(Diagnostic) as caught:
                        parse_source(source, "attachment.xe")
                    self.assertEqual(caught.exception.code, "XE-PARSE-0001")
                    self.assertIn("声明附件", caught.exception.message)
                    self.assertIn("attachment.xe:", caught.exception.render())

    def test_mutability_is_not_a_type_modifier_or_rust_prefix(self):
        for source in ("fn f() { let mut x = 0; }", "fn f(let x: i32) {}", "fn f(let[mut] x: i32) {}", "fn f(mut x: i32) {}"):
            with self.subTest(source=source):
                self.assertRaises(Diagnostic, parse_source, source)
        errors = check_source("fn f() { let x: i32[mut] = 0; }", check_borrows=True)
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-NAME-0001")

    def test_semantic_mutability_and_pointer_permissions_are_independent(self):
        positive = '''fn advance(n: i32) -> i32 { let[mut] n = n; n = n + 1; n }
        fn main() { let original = 1; let[mut] count = advance(original);
            let pointer: i32@[mut] = count@[mut]; pointer# = 3;
            println("{}", count); }'''
        for spelling in (positive, positive.replace("let[mut]", "var")):
            self.assertEqual(check_source(spelling, check_borrows=True), [])
        negative = positive.replace("pointer# = 3;", "pointer = count@[mut];")
        self.assertEqual(check_source(negative, check_borrows=True)[0].code, "XE-MUT-0001")
        immutable = "fn f() { let value = 0; value = 1; }"
        self.assertEqual(check_source(immutable, check_borrows=False)[0].code, "XE-MUT-0001")
        self.assertEqual(check_source(immutable, check_borrows=True)[0].code, "XE-MUT-0001")

    @unittest.skipUnless(CC, "运行验收需要系统 C 编译器")
    def test_public_sample_and_hidden_alias_execute_identically(self):
        text = (ROOT / "tests/language/mutable_binding.xe").read_text()
        for spelling in (text, text.replace("let[mut]", "var")):
            with tempfile.TemporaryDirectory() as directory:
                source, program = Path(directory) / "test.xe", Path(directory) / "program"
                source.write_text(spelling, encoding="utf-8")
                build_executable(source, program, True, CC)
                result = subprocess.run([str(program)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "1 3\n9\nhello, world\n")
            self.assertEqual(result.stderr, "")
