"""Accepted extern C syntax must check, link and run without Xe ABI guesses."""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.driver import build
from compiler.project import project_at
from compiler.toolchain import main
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import BuildError, build_executable
from compiler.xe_ast.cli import main as legacy_main
from compiler.xe_ast.modules import load_program
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source

CC = shutil.which("cc") or ""
AR = shutil.which("ar") or ""


class FfiSemanticTests(unittest.TestCase):
    def check(self, text):
        return Checker(Source(text, "ffi.xe"), parse_source(text, "ffi.xe")).check()

    def test_scalar_pointer_void_and_compatible_callback_declarations(self):
        self.assertEqual(self.check('''extern "C" {
            fn number(value: f64, flag: bool, scalar: char, length: usize) -> i64;
            fn pointer(value: i32@[mut]) -> i32@[mut];
            fn store(value: i32);
            fn apply(callback: fn(i32) -> i32, value: i32) -> i32;
            fn choose() -> fn(i32) -> i32;
        } fn main() {}'''), [])

    def test_unimplemented_abi_and_generics_are_rejected_during_check(self):
        for source in ('extern "Rust" { fn foreign(); }',
                       'extern "C" { fn[T] foreign(value: T) -> T; }',
                       'extern "C" { fn auto(); }',
                       'extern "C" { fn main(); }',
                       'extern "C" { fn xe_panic(value: i32); }',
                       'extern "C" { fn 中文(); }'):
            with self.subTest(source=source):
                errors = self.check(source + ("" if "fn main" in source else " fn main() {}"))
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-FFI-0001", errors[0].render())

    def test_resources_aggregates_and_incompatible_callbacks_are_rejected(self):
        for type_ in ("str", "String", "tuple[i32, bool]", "Array[i32, 2]", "i32?",
                      "Box[i32]", "String@", "str@", "Unit", "fn(i32) -> Unit",
                      "fn(Unit) -> i32", "fn(i32) -> Never"):
            with self.subTest(type_=type_):
                errors = self.check(f'extern "C" {{ fn foreign(value: {type_}); }} fn main() {{}}')
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-FFI-0001", errors[0].render())
                self.assertIn("参数 ABI", errors[0].message)
        for type_ in ("String", "i32?", "Never", "fn() -> Unit"):
            with self.subTest(result=type_):
                errors = self.check(f'extern "C" {{ fn foreign() -> {type_}; }} fn main() {{}}')
                self.assertEqual(errors[0].code, "XE-FFI-0001", errors[0].render())

    def test_external_void_uses_a_unit_wrapper_and_unmangled_c_symbol(self):
        generated = lower_to_c('extern "C" { fn native_store(value: i32); } fn main() { native_store(42); }')
        self.assertIn("extern void native_store(int32_t);", generated)
        self.assertIn("static inline XeUnit xe_function_5f_native_5f_store(int32_t)", generated)
        self.assertIn("native_store(", generated)

    def test_user_aggregate_pointer_has_no_implicit_c_layout(self):
        errors = self.check('struct S { value: i32, } extern "C" { fn foreign(value: S@); } fn main() {}')
        self.assertEqual(errors[0].code, "XE-FFI-0001")


@unittest.skipUnless(CC, "外部 C 接口验收需要系统 C 编译器")
class FfiExecutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="xe-ffi-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.file("main.xe", "fn main() {}")
        self.output = self.root / "program"

    def file(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_built(self, expected):
        result = subprocess.run([str(self.output)], capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ""))

    def cli(self, function, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = function([str(argument) for argument in arguments])
        return status, out.getvalue(), err.getvalue()

    def test_scalar_calls_void_function_values_and_callback_roundtrip(self):
        self.source = self.file("main.xe", '''extern "C" {
            fn native_add(a: i32, b: i32) -> i32;
            fn native_store(value: i32);
            fn native_get() -> i32;
            fn native_float(value: f64, flag: bool) -> f64;
            fn native_apply(callback: fn(i32) -> i32, value: i32) -> i32;
            fn native_choose() -> fn(i32) -> i32;
        }
        fn next(value: i32) -> i32 { value + 1 }
        fn main() {
            let add: fn(i32, i32) -> i32 = native_add;
            let store: fn(i32) -> Unit = native_store;
            store(add(20, 22));
            let chosen = native_choose();
            println("{} {} {} {}", native_get(), native_apply(next, 41), chosen(21), native_float(1.5, true));
        }''')
        native = self.file("native.c", '''#include <stdint.h>
#include <stdbool.h>
static int32_t stored;
int32_t native_add(int32_t a, int32_t b) { return a + b; }
void native_store(int32_t value) { stored = value; }
int32_t native_get(void) { return stored; }
double native_float(double value, bool flag) { return flag ? value * 2 : value; }
int32_t native_apply(int32_t (*callback)(int32_t), int32_t value) { return callback(value); }
static int32_t twice(int32_t value) { return value * 2; }
int32_t (*native_choose(void))(int32_t) { return twice; }
''')
        build_executable(self.source, self.output, cc=CC,
                         extra_flags=("-pedantic-errors",), link_inputs=(native,))
        self.run_built("42 42 42 3\n")

    def test_mutable_pointer_and_pointer_return(self):
        self.source = self.file("main.xe", '''extern "C" {
            fn native_increment(value: i32@[mut]);
            fn native_identity(value: i32@[mut]) -> i32@[mut];
        }
        fn main() { let[mut] value = 40; native_increment(value@[mut]);
            let pointer = native_identity(value@[mut]); pointer# = pointer# + 1;
            println("{}", value);
        }''')
        native = self.file("native.c", '''#include <stdint.h>
void native_increment(int32_t *value) { *value += 1; }
int32_t *native_identity(int32_t *value) { return value; }
''')
        build_executable(self.source, self.output, cc=CC, link_inputs=(native,))
        self.run_built("42\n")

    def test_external_object_links_and_input_is_never_overwritten(self):
        self.source = self.file("main.xe", 'extern "C" { fn native_number() -> i32; } fn main() { println("{}", native_number()); }')
        native = self.file("native.c", "#include <stdint.h>\nint32_t native_number(void) { return 42; }\n")
        object_path = self.root / "native.o"
        compiled = subprocess.run([CC, "-std=c11", "-c", str(native), "-o", str(object_path)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        build_executable(self.source, self.output, cc=CC, link_inputs=(object_path,))
        self.run_built("42\n")
        original = object_path.read_bytes()
        with self.assertRaises(BuildError):
            build_executable(self.source, object_path, cc=CC, link_inputs=(object_path,))
        self.assertEqual(object_path.read_bytes(), original)

    @unittest.skipUnless(AR, "静态库链接验收需要 ar")
    def test_static_archive_is_linked_after_the_generated_program(self):
        self.source = self.file("main.xe", 'extern "C" { fn native_number() -> i32; } fn main() { println("{}", native_number()); }')
        native = self.file("native.c", "#include <stdint.h>\nint32_t native_number(void) { return 42; }\n")
        object_path = self.root / "native.o"
        archive = self.root / "libnative.a"
        compiled = subprocess.run([CC, "-c", str(native), "-o", str(object_path)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        archived = subprocess.run([AR, "rcs", str(archive), str(object_path)], capture_output=True, text=True)
        self.assertEqual(archived.returncode, 0, archived.stderr)
        build_executable(self.source, self.output, cc=CC, link_inputs=(archive,))
        self.run_built("42\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan/no-pie 运行验收当前针对 Linux")
    def test_byte_pointer_roundtrip_passes_address_and_undefined_sanitizers(self):
        self.source = self.file("main.xe", '''extern "C" {
            fn native_set(data: u8@[mut], length: usize);
        }
        fn main() { let[mut] bytes: Array[u8, 3] = [1, 2, 3];
            native_set(bytes[0]@[mut], bytes.len());
            println("{} {} {}", bytes[0], bytes[1], bytes[2]);
        }''')
        native = self.file("native source.c", '''#include <stdint.h>
#include <stddef.h>
void native_set(uint8_t *data, size_t length) {
    for (size_t i = 0; i < length; ++i) data[i] = (uint8_t)(40 + i);
}
''')
        build_executable(self.source, self.output, cc=CC, link_inputs=(native,),
                         extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
        self.run_built("40 41 42\n")

    def test_cross_module_symbol_is_not_renamed_and_conflicts_are_diagnosed(self):
        self.file("xe.toml", '[package]\nname="ffi"\n')
        self.source = self.file("src/main.xe", '''use crate::native::answer;
            extern "C" { fn native_number() -> i32; }
            fn main() { println("{} {}", answer(), native_number()); }''')
        self.file("src/native.xe", '''extern "C" { fn native_number() -> i32; }
            pub fn answer() -> i32 { native_number() }''')
        native = self.file("native.c", "#include <stdint.h>\nint32_t native_number(void) { return 42; }\n")
        build_executable(self.source, self.output, cc=CC, link_inputs=(native,))
        self.run_built("42 42\n")
        self.file("src/native.xe", '''extern "C" { fn native_number(value: i32) -> i32; }
            pub fn answer() -> i32 { native_number(1) }''')
        source, tree = load_program(self.source)
        errors = Checker(source, tree).check()
        self.assertEqual(errors[0].code, "XE-FFI-0002", errors[0].render())

    def test_link_failure_preserves_old_program(self):
        build_executable(self.source, self.output, cc=CC)
        original = self.output.read_bytes()
        self.source = self.file("main.xe", 'extern "C" { fn missing_symbol() -> i32; } fn main() { missing_symbol(); }')
        with self.assertRaises(BuildError):
            build_executable(self.source, self.output, cc=CC)
        self.assertEqual(self.output.read_bytes(), original)

    def test_unused_external_declaration_does_not_need_a_definition(self):
        self.source = self.file("main.xe", 'extern "C" { fn missing_symbol() -> i32; } fn main() { println("ok"); }')
        build_executable(self.source, self.output, cc=CC)
        self.run_built("ok\n")

    def test_link_inputs_are_recorded_and_conservatively_disable_cache(self):
        self.source = self.file("main.xe", 'extern "C" { fn native_number() -> i32; } fn main() { println("{}", native_number()); }')
        native = self.file("native.c", "#include <stdint.h>\nint32_t native_number(void) { return 1; }\n")
        project = project_at(self.source)
        first = build(project, self.source, self.output, cc=CC, link_inputs=(native,))
        self.assertFalse(first.reused)
        self.assertIn(str(native), json.loads(first.receipt.read_text())["build"]["link_inputs"])
        self.assertFalse(build(project, self.source, self.output, cc=CC, link_inputs=(native,)).reused)
        self.file("native.c", "#include <stdint.h>\nint32_t native_number(void) { return 2; }\n")
        build(project, self.source, self.output, cc=CC, link_inputs=(native,))
        self.run_built("2\n")

    def test_unified_and_legacy_cli_link_inputs(self):
        self.source = self.file("main.xe", 'extern "C" { fn native_number() -> i32; } fn main() { println("{}", native_number()); }')
        native = self.file("native.c", "#include <stdint.h>\nint32_t native_number(void) { return 42; }\n")
        status, _, error = self.cli(main, "build", self.source, "--cc", CC, "--link-input", native, "-o", self.output)
        self.assertEqual((status, error), (0, ""))
        self.run_built("42\n")
        status, _, error = self.cli(legacy_main, self.source, "--build", "--cc", CC, "--link-input", native, "-o", self.output)
        self.assertEqual((status, error), (0, ""))
        self.run_built("42\n")


if __name__ == "__main__":
    unittest.main()
