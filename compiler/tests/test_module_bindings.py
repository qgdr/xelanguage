"""模块 let 复用 Constant 语义；真实包验证导出路径和 C 后端边界。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast import Diagnostic, check_source, parse_source
from compiler.xe_ast.build import build_executable, emit_c
from compiler.xe_ast.modules import load_program
from compiler.xe_ast.semantic import Checker

CC = shutil.which("cc") or ""


def shape(value):
    """关键字长度会改变位置；比较其余 AST 字段以保证旧 const 兼容。"""
    if isinstance(value, dict):
        return {key: shape(child) for key, child in value.items() if key != "span"}
    if isinstance(value, list):
        return [shape(child) for child in value]
    return value


class ModuleBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.entry = self.root / "src/main.xe"
        self.write({"xe.toml": '[package]\nname = "module_bindings"\n',
                    "src/main.xe": "fn main() {}"})

    def write(self, files):
        for relative, text in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def check_program(self):
        source, tree = load_program(self.entry)
        errors = Checker(source, tree).check()
        if errors:
            raise errors[0]
        return tree

    def test_module_let_and_legacy_const_have_identical_ast(self):
        text = '''let COUNT: i32 = 42;
            pub let LABEL: str = "module";
            let NEGATIVE: i32 = -7;
            let READY: bool = true;
            fn main() {}'''
        tree = parse_source(text)
        self.assertEqual(shape(tree), shape(parse_source(text.replace("let ", "const "))))
        bindings = tree["items"][:-1]
        self.assertEqual([node["kind"] for node in bindings], ["Constant"] * 4)
        self.assertEqual([node["public"] for node in bindings], [False, True, False, False])
        for node in bindings:
            start, end = node["span"]["start"]["offset"], node["span"]["end"]["offset"]
            declaration = text[start:end]
            self.assertTrue(declaration.startswith("pub let " if node["public"] else "let "))
            self.assertTrue(declaration.endswith(";"))

    @unittest.skipUnless(CC, "运行验收需要系统 C 编译器")
    def test_public_bindings_execute_through_imports_aliases_and_reexports(self):
        # crate、模块别名、分组导入和重导出都应引用同一个只读模块值。
        self.write({
            "src/main.xe": '''use crate::settings::{BASE as imported, label, private_value};
                use crate::settings as settings;
                use crate::api::PUBLIC_ALIAS;
                let ROOT: i32 = -2;
                fn main() {
                    let BASE = 5;
                    let[mut] total = imported;
                    total = total + BASE;
                    println("{} {} {} {} {} {} {}", imported, settings::BASE,
                        crate::settings::BASE, PUBLIC_ALIAS, total, ROOT, private_value());
                    println("{}", label());
                }''',
            "src/settings.xe": '''pub let BASE: i32 = 40;
                let SECRET: i32 = 2;
                pub let LABEL: str = "settings";
                pub fn label() -> str { LABEL }
                pub fn private_value() -> i32 { SECRET }''',
            "src/api.xe": "pub use crate::settings::BASE as PUBLIC_ALIAS;",
        })
        tree = self.check_program()
        self.assertEqual(sum(node["kind"] == "Constant" for node in tree["items"]), 4)
        self.assertIn(str(self.root / "src/settings.xe"), tree["_sources"])
        self.assertIn(str(self.root / "src/api.xe"), tree["_sources"])
        output = self.root / "program"
        build_executable(self.entry, output, cc=CC)
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "40 40 40 40 45 -2 2\nsettings\n")
        self.assertEqual(result.stderr, "")

    def test_private_module_binding_is_not_exported(self):
        self.write({"src/settings.xe": "let SECRET: i32 = 2;"})
        for text in (
                "use crate::settings::SECRET; fn main() {}",
                'fn main() { println("{}", crate::settings::SECRET); }',
                'use crate::settings as settings; fn main() { println("{}", settings::SECRET); }'):
            with self.subTest(source=text):
                self.write({"src/main.xe": text})
                with self.assertRaises(Diagnostic) as caught:
                    self.check_program()
                self.assertIn("私有", caught.exception.message)
                self.assertEqual(caught.exception.source.filename, str(self.entry))

    def test_duplicate_module_names_share_legacy_constant_namespace(self):
        for repeated in ("let VALUE: i32 = 2;", "const VALUE: i32 = 2;",
                         "pub let VALUE: i32 = 2;", "fn VALUE() {}"):
            with self.subTest(repeated=repeated):
                errors = check_source("let VALUE: i32 = 1; " + repeated + " fn main() {}")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-NAME-0002")
                self.assertIn("重复", errors[0].message)

    def test_module_binding_rejects_assignment(self):
        for action in ("VALUE = 2;", "VALUE << 2;", "2 >> VALUE;"):
            with self.subTest(action=action):
                errors = check_source("let VALUE: i32 = 1; fn main() { " + action + " }")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-MUT-0001")

    def test_module_binding_has_no_borrowable_storage(self):
        # Constant 当前按值使用，没有模块静态存储；两类借用均不得制造地址。
        for address in ("VALUE@", "VALUE@[mut]"):
            with self.subTest(address=address):
                errors = check_source("let VALUE: i32 = 1; fn main() { let pointer = " + address + "; }")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-BORROW-0001")

    def test_imported_module_binding_remains_readonly(self):
        self.write({"src/settings.xe": "pub let VALUE: i32 = 1;"})
        for text, code in (
                ("use crate::settings::VALUE as imported; fn main() { imported = 2; }", "XE-MUT-0001"),
                ("fn main() { crate::settings::VALUE = 2; }", "XE-MUT-0001"),
                ("use crate::settings as settings; fn main() { settings::VALUE << 2; }", "XE-MUT-0001"),
                ("use crate::settings::VALUE; fn main() { let pointer = VALUE@[mut]; }", "XE-BORROW-0001")):
            with self.subTest(source=text):
                self.write({"src/main.xe": text})
                with self.assertRaises(Diagnostic) as caught:
                    self.check_program()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.source.filename, str(self.entry))

    def test_local_let_inference_mutability_and_resource_move_are_unchanged(self):
        text = '''let MODULE: i32 = 1;
            fn main() {
                let inferred = MODULE;
                let[mut] writable = 0;
                var alias = 1;
                let text << String::from("owned");
                writable = inferred;
                alias = alias + writable;
                println("{} {}", alias, text);
            }'''
        tree = parse_source(text)
        bindings = tree["items"][1]["body"]["statements"][:4]
        self.assertEqual([node["kind"] for node in bindings], ["Binding"] * 4)
        self.assertEqual([node["type"] for node in bindings], [None] * 4)
        self.assertEqual([node["mutable"] for node in bindings], [False, True, True, False])
        self.assertEqual([node["operator"] for node in bindings], ["=", "=", "=", "<<"])
        self.assertEqual(check_source(text), [])

    def test_local_binding_can_shadow_module_value_with_mutable_storage(self):
        text = '''let VALUE: i32 = 1;
            fn main() {
                let[mut] VALUE = VALUE;
                VALUE = 2;
                let pointer = VALUE@[mut];
                pointer# = 3;
            }'''
        self.assertEqual(check_source(text), [])

    def test_unsupported_module_forms_report_actionable_parse_errors(self):
        for declaration in (
                "let[mut] VALUE: i32 = 1;", "var VALUE: i32 = 1;",
                "let VALUE: i32 << 1;", "pub let[mut] VALUE: i32 = 1;",
                "pub var VALUE: i32 = 1;", "pub let VALUE: i32 << 1;",
                "let VALUE = 1;", "pub let VALUE = 1;", "let VALUE: i32;",
                "let VALUE;"):
            with self.subTest(declaration=declaration):
                with self.assertRaises(Diagnostic) as caught:
                    parse_source(declaration + " fn main() {}", "module_binding.xe")
                error = caught.exception
                self.assertEqual(error.code, "XE-PARSE-0001")
                self.assertIn("模块", error.message)
                self.assertTrue(error.hint)
                self.assertIn("module_binding.xe:", error.render())

    def test_trait_and_impl_reject_module_bindings_as_methods(self):
        for text in ("trait Invalid { pub let VALUE: i32 = 1; }",
                     "struct Item {} impl Item { pub let VALUE: i32 = 1; }"):
            with self.subTest(source=text):
                with self.assertRaises(Diagnostic) as caught:
                    parse_source(text, "method_binding.xe")
                self.assertEqual(caught.exception.code, "XE-PARSE-0001")
                self.assertIn("方法", caught.exception.message)
                self.assertIn("method_binding.xe:", caught.exception.render())

    def test_module_resources_keep_existing_constant_semantic_boundary(self):
        text = 'let RESOURCE: String = String::from("module"); fn main() {}'
        self.assertEqual(parse_source(text)["items"][0]["kind"], "Constant")
        errors = check_source(text)
        self.assertTrue(errors)
        self.assertEqual(errors[0].code, "XE-SEM-0001")
        self.assertIn("可复制", errors[0].message)

    def test_nonliteral_initializers_keep_existing_backend_boundary(self):
        for initializer in ("1 + 2", "value()"):
            with self.subTest(initializer=initializer):
                text = (f"let VALUE: i32 = {initializer}; "
                        "fn value() -> i32 { 3 } fn main() {}")
                self.assertEqual(check_source(text), [])
                self.write({"src/main.xe": text})
                with self.assertRaises(Diagnostic) as caught:
                    emit_c(self.entry, self.root / "program.c")
                self.assertEqual(caught.exception.code, "XE-BACKEND-0001")
                self.assertIn("字面量", caught.exception.message)
                self.assertEqual(caught.exception.source.filename, str(self.entry))
