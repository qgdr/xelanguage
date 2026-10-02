"""全局可变存储的语法、模块身份、地址生命期与真实 C 执行回归。

全局初值必须静态可构造；只支持 Copy，不引入顶层运行顺序或资源析构。
潜在悬垂指针程序仅检查和编译，绝不运行。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast import Diagnostic, check_source, parse_source
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.modules import load_program
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which("cc") or ""


class GlobalBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xe-global-bindings-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.entry = self.root / "src/main.xe"
        self.write({"xe.toml": '[package]\nname = "global_bindings"\n',
                    "src/main.xe": "fn main() {}"})

    def write(self, files):
        for relative, text in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def check(self, text):
        checker = Checker(Source(text, "globals.xe"), parse_source(text, "globals.xe"))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        return checker

    def check_program(self):
        source, tree = load_program(self.entry)
        checker = Checker(source, tree)
        errors = checker.check()
        if errors:
            raise errors[0]
        return checker

    def run_program(self, expected, flags=()):
        program = self.root / "program"
        warnings = []
        build_executable(self.entry, program, cc=CC,
                         extra_flags=("-pedantic-errors", *flags), warnings=warnings)
        self.assertEqual(warnings, [])
        result = subprocess.run([str(program)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, ASAN_OPTIONS="detect_leaks=1:abort_on_error=1"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ""))

    def run_text(self, text, expected, flags=()):
        self.write({"src/main.xe": text})
        self.run_program(expected, flags)

    def assert_error(self, text, code=None):
        errors = check_source(text, "globals.xe")
        self.assertTrue(errors, text)
        if code:
            self.assertEqual(errors[0].code, code, errors[0].render())
        return errors[0]

    def test_mutable_declarations_have_global_storage_ast(self):
        text = '''let BASE: i32 = 1;
            let[mut] COUNT: i32 = BASE;
            pub let[mut] READY: bool = true;
            var LEGACY: i32 = -2;
            pub var LEGACY_PUBLIC: i32 = +3;
            fn main() {}'''
        bindings = parse_source(text)["items"][:-1]
        self.assertEqual([node["kind"] for node in bindings],
                         ["Constant", "GlobalBinding", "GlobalBinding", "GlobalBinding", "GlobalBinding"])
        self.assertEqual([node["public"] for node in bindings], [False, False, True, False, True])
        for node in bindings[1:]:
            self.assertTrue(node["mutable"])
            self.assertEqual(node["operator"], "=")
            start, end = node["span"]["start"]["offset"], node["span"]["end"]["offset"]
            self.assertTrue(text[start:end].endswith(";"))
        self.check(text)

    def test_global_requires_type_and_initializer(self):
        for declaration in ("let[mut] COUNT = 0;", "let[mut] COUNT: i32;",
                            "pub let[mut] COUNT = 0;", "pub let[mut] COUNT: i32;",
                            "var COUNT = 0;", "var COUNT: i32;", "let[mut] COUNT: i32 << 0;"):
            with self.subTest(declaration=declaration):
                with self.assertRaises(Diagnostic) as caught:
                    parse_source(declaration + " fn main() {}", "globals.xe")
                self.assertEqual(caught.exception.code, "XE-PARSE-0001")
                self.assertTrue(caught.exception.hint)
                self.assertIn("globals.xe:", caught.exception.render())

    def test_global_names_share_module_namespace(self):
        for duplicate in ("let COUNT: i32 = 1;", "const COUNT: i32 = 1;",
                          "let[mut] COUNT: i32 = 1;", "fn COUNT() {}"):
            with self.subTest(duplicate=duplicate):
                self.assert_error("let[mut] COUNT: i32 = 0; " + duplicate + " fn main() {}",
                                  "XE-NAME-0002")

    def test_resource_and_noncopy_globals_report_user_diagnostic(self):
        for text in (
                'let[mut] TEXT: String = String::from("owned"); fn main() {}',
                "struct Item { value: i32, } let[mut] ITEM: Item = Item { .value = 1; }; fn main() {}",
                "struct Item { value: i32, } impl Drop for Item { fn drop(self: Self@[mut]) {} } "
                "let[mut] ITEM: Item = Item { .value = 1; }; fn main() {}"):
            with self.subTest(text=text):
                error = self.assert_error(text)
                self.assertTrue("Copy" in error.message or "复制" in error.message, error.render())
                self.assertTrue(error.hint)

    def test_dynamic_initializer_is_rejected_before_c_backend(self):
        for initializer in ("value()", "{ 3 }", "if true { 1 } else { 2 }"):
            with self.subTest(initializer=initializer):
                error = self.assert_error(f"let[mut] COUNT: i32 = {initializer}; "
                                          "fn value() -> i32 { 3 } fn main() {}")
                self.assertIn("初始", error.message)
                self.assertTrue(error.hint)

    def test_initializer_cannot_read_mutable_global_runtime_value(self):
        for initializer in ("FIRST", "POINTER#"):
            with self.subTest(initializer=initializer):
                error = self.assert_error("let[mut] FIRST: i32 = 1; "
                                          "let[mut] POINTER: i32@ = FIRST@; "
                                          f"let[mut] SECOND: i32 = {initializer}; fn main() {{}}")
                self.assertIn("初始", error.message)

    def test_static_initializer_type_mismatch_is_checked(self):
        for text in ("let[mut] COUNT: i32 = true; fn main() {}",
                     "let[mut] VALUES: Array[i32, 2] = [1]; fn main() {}",
                     "let[mut] PAIR: tuple[i32, bool] = tuple[1, 2]; fn main() {}"):
            with self.subTest(text=text):
                self.assert_error(text, "XE-TYPE-0001")

    def test_copy_globals_use_equal_or_forward_assignment_not_move(self):
        self.check("let[mut] COUNT: i32 = 0; fn main() { COUNT = 1; 2 >> COUNT; }")
        self.assert_error("let[mut] COUNT: i32 = 0; fn main() { COUNT << 1; }", "XE-OWN-0001")

    def test_global_readonly_pointer_does_not_allow_writes(self):
        self.assert_error("let[mut] COUNT: i32 = 0; fn main() { let pointer = COUNT@; pointer# = 1; }",
                          "XE-MUT-0001")
        self.assert_error("let[mut] COUNT: i32 = 0; let[mut] POINTER: i32@ = COUNT@; "
                          "fn main() { POINTER# = 1; }", "XE-MUT-0001")

    def test_returning_global_addresses_does_not_warn(self):
        checker = self.check('''struct State { value: i32, } impl Copy for State;
            let[mut] COUNT: i32 = 0;
            let[mut] VALUES: Array[i32, 2] = [1, 2];
            let[mut] STATE: State = State { .value = 3; };
            fn count() -> i32@ { COUNT@ }
            fn count_mut() -> i32@[mut] { COUNT@[mut] }
            fn element() -> i32@ { VALUES[0]@ }
            fn field() -> i32@[mut] { STATE.value@[mut] }
            fn main() {}''')
        self.assertEqual(checker.warnings, [])
        self.assertTrue(all(not checker.functions[name].unsafe_result
                            for name in ("count", "count_mut", "element", "field")))

    def test_storing_local_address_into_global_reports_nonblocking_warning(self):
        for declaration, assignment in (
                ("let[mut] SAVED: i32@ = INITIAL@;", "SAVED = local@;"),
                ("let[mut] SAVED: i32@[mut] = INITIAL@[mut];", "SAVED = local@[mut];")):
            with self.subTest(declaration=declaration):
                text = ("let[mut] INITIAL: i32 = 0; " + declaration +
                        " fn save() { let[mut] local = 1; " + assignment + " } fn main() {}")
                checker = self.check(text)
                self.assertIn("XE-PTR-0001", [warning.code for warning in checker.warnings])
                self.assertTrue(all(warning.severity == "warning" for warning in checker.warnings))
                self.assertTrue(any(entry["unsafe"] for entry in checker.inferred_types.values()))

    def test_private_global_is_not_exported(self):
        self.write({"src/state.xe": "let[mut] SECRET: i32 = 0;"})
        for text in ("use crate::state::SECRET; fn main() {}",
                     "fn main() { crate::state::SECRET = 1; }",
                     "use crate::state as state; fn main() { let pointer = state::SECRET@; }"):
            with self.subTest(text=text):
                self.write({"src/main.xe": text})
                with self.assertRaises(Diagnostic) as caught:
                    self.check_program()
                self.assertIn("私有", caught.exception.message)
                self.assertEqual(caught.exception.source.filename, str(self.entry))

    @unittest.skipUnless(CC, "运行全局变量验收需要系统 C 编译器")
    def test_repeated_calls_and_early_return_preserve_storage(self):
        self.run_text('''let[mut] COUNT: i32 = 0;
            fn tick() -> i32 { COUNT = COUNT + 1; COUNT }
            fn stop() { COUNT = COUNT + 10; return; }
            fn main() {
                println("{} {}", tick(), tick());
                stop();
                println("{} {}", COUNT, tick());
            }''', "1 2\n12 13\n")

    @unittest.skipUnless(CC, "运行全局变量验收需要系统 C 编译器")
    def test_public_imports_aliases_and_reexports_share_one_storage(self):
        self.write({
            "src/main.xe": '''use crate::state::{COUNT as imported, bump};
                use crate::state as state;
                use crate::api::EXPORTED;
                fn main() {
                    imported = 1;
                    bump();
                    state::COUNT = state::COUNT + 10;
                    40 >> crate::state::COUNT;
                    let pointer = EXPORTED@[mut];
                    pointer# = pointer# + 2;
                    println("{} {} {} {}", imported, state::COUNT, crate::state::COUNT, EXPORTED);
                }''',
            "src/state.xe": '''pub let[mut] COUNT: i32 = 0;
                pub fn bump() { COUNT = COUNT + 1; }''',
            "src/api.xe": "pub use crate::state::COUNT as EXPORTED;",
        })
        self.check_program()
        self.run_program("42 42 42 42\n")

    @unittest.skipUnless(CC, "运行全局变量验收需要系统 C 编译器")
    def test_separate_modules_with_same_name_have_independent_storage(self):
        self.write({
            "src/main.xe": '''use crate::first as first;
                use crate::second as second;
                fn main() {
                    first::COUNT = 7;
                    println("{} {}", first::COUNT, second::COUNT);
                    second::COUNT = 9;
                    println("{} {}", first::COUNT, second::COUNT);
                }''',
            "src/first.xe": "pub let[mut] COUNT: i32 = 1;",
            "src/second.xe": "pub let[mut] COUNT: i32 = 2;",
        })
        self.run_program("7 2\n7 9\n")

    @unittest.skipUnless(CC, "运行全局变量验收需要系统 C 编译器")
    def test_local_shadow_and_function_parameters_do_not_replace_global(self):
        self.run_text('''let[mut] COUNT: i32 = 1;
            fn read() -> i32 { COUNT }
            fn parameter(COUNT: i32) -> i32 { COUNT }
            fn main() {
                let[mut] COUNT = COUNT;
                COUNT = 9;
                println("{} {} {}", COUNT, read(), parameter(3));
            }''', "9 1 3\n")

    @unittest.skipUnless(CC, "运行全局变量验收需要系统 C 编译器")
    def test_globals_are_accessible_in_uncaptured_and_captured_closures(self):
        self.run_text('''let[mut] COUNT: i32 = 0;
            fn main() {
                let callback = fn() -> i32 { COUNT = COUNT + 1; COUNT };
                let amount = 10;
                let add << fn[amount]() -> i32 { COUNT = COUNT + amount; COUNT };
                println("{} {} {} {}", callback(), add(), callback(), COUNT);
            }''', "1 11 12 12\n")

    @unittest.skipUnless(CC, "运行全局变量验收需要系统 C 编译器")
    def test_global_addresses_are_static_even_with_forward_declarations(self):
        self.run_text('''let[mut] POINTER: i32@[mut] = COUNT@[mut];
            let[mut] COUNT: i32 = 1;
            fn address() -> i32@[mut] { COUNT@[mut] }
            fn main() {
                POINTER# = 2;
                let pointer = address();
                pointer# = pointer# + 3;
                println("{} {}", COUNT, POINTER#);
            }''', "5 5\n")

    @unittest.skipUnless(CC, "运行全局变量验收需要系统 C 编译器")
    def test_static_field_and_array_addresses_in_aggregate_initializers(self):
        self.run_text('''struct State { value: i32, } impl Copy for State;
            struct Handles { field: i32@[mut], element: i32@[mut], } impl Copy for Handles;
            let[mut] HANDLES: Handles = Handles {
                .field = STATE.value@[mut]; .element = VALUES[1]@[mut];
            };
            let[mut] VALUES: Array[i32, 2] = [1, 2];
            let[mut] STATE: State = State { .value = 3; };
            fn main() {
                HANDLES.field# = 4;
                HANDLES.element# = 5;
                println("{} {} {}", STATE.value, VALUES[0], VALUES[1]);
            }''', "4 1 5\n")

    @unittest.skipUnless(CC, "运行全局变量验收需要系统 C 编译器")
    def test_readonly_constant_initializers_work_across_modules(self):
        self.write({
            "src/main.xe": '''use crate::settings::BASE as INITIAL;
                use crate::settings as settings;
                let[mut] COUNT: i32 = INITIAL;
                let[mut] SECOND: i32 = -settings::BASE;
                fn main() { COUNT = COUNT + 2; println("{} {}", COUNT, SECOND); }''',
            "src/settings.xe": "pub let BASE: i32 = 40;",
        })
        self.run_program("42 -40\n")

    @unittest.skipUnless(CC, "运行全局变量验收需要系统 C 编译器")
    def test_aggregate_and_unicode_initializers_are_valid_strict_c(self):
        self.run_text('''struct State { pair: tuple[i32, bool], labels: Array[str, 2], }
            impl Copy for State;
            let[mut] STATE: State = State {
                .labels = ["first", "猫\\0tail"];
                .pair = tuple[(+20), true];
            };
            let[mut] EMPTY: Array[i32, 0] = [];
            let[mut] LETTER: char = '猫';
            let[mut] MINIMUM: i64 = -9223372036854775808;
            let[mut] MAXIMUM: u64 = 18446744073709551615;
            fn main() {
                STATE.pair.0 = 42;
                STATE.labels[0] = "changed";
                println("{} {} {} {} {} {}", STATE.pair.0, STATE.pair.1,
                    STATE.labels[0], STATE.labels[1], LETTER, MINIMUM);
                println("{} {}", MAXIMUM, EMPTY.len());
            }''', "42 true changed 猫\0tail 猫 -9223372036854775808\n18446744073709551615 0\n")

    @unittest.skipUnless(CC, "运行全局变量验收需要系统 C 编译器")
    def test_backend_fixture_and_example_execute(self):
        for relative, expected in (
                ("tests/backend/global_variables.xe", "count=2 address=2\npoint=7,9 pair=20,40 values=3,4\n"
                 "labels=first,changed\nshadow=99 global=2\nclosure=3 global=3\n"),
                ("examples/globals/main.xe", "tick=1\ntick=2\nthrough-pointer=10\nlocal=100 global=10\n")):
            with self.subTest(relative=relative):
                self.run_text((ROOT / relative).read_text(encoding="utf-8"), expected)

    @unittest.skipUnless(CC and sys.platform.startswith("linux"), "ASan/no-pie 运行验收当前针对 Linux")
    def test_global_aggregate_fixture_passes_address_and_undefined_sanitizers(self):
        text = (ROOT / "tests/backend/global_variables.xe").read_text(encoding="utf-8")
        self.run_text(text, "count=2 address=2\npoint=7,9 pair=20,40 values=3,4\n"
                      "labels=first,changed\nshadow=99 global=2\nclosure=3 global=3\n",
                      flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))

    @unittest.skipUnless(CC, "全局指针警告编译验收需要系统 C 编译器")
    def test_global_local_address_warning_still_builds_without_execution(self):
        self.write({"src/main.xe": '''let[mut] INITIAL: i32 = 0;
            let[mut] SAVED: i32@ = INITIAL@;
            fn save() { let local = 1; SAVED = local@; }
            fn main() {}'''})
        warnings = []
        output = self.root / "warning-only"
        build_executable(self.entry, output, cc=CC, extra_flags=("-pedantic-errors",), warnings=warnings)
        self.assertTrue(output.exists())
        self.assertIn("XE-PTR-0001", [warning.code for warning in warnings])


if __name__ == "__main__":
    unittest.main()
