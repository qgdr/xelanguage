"""进程参数必须通过真实 argv 验收，不能用 stdin 或字符串拆分代替。"""
import contextlib
import copy
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compiler.xe_ast import parse_source
from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.cli import main
from compiler.xe_ast.semantic import Checker, check_source
from compiler.xe_ast.source import Source
from compiler.xe_ast.stdlib_env import ENV_ARGS_RESULT, env_function
from compiler.xe_ast.typesys import IO_ERROR, STR, Type

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which("cc") or ""


class EnvSemanticTests(unittest.TestCase):
    def check(self, text):
        tree = parse_source(text, "args.xe")
        original = copy.deepcopy(tree)
        checker = Checker(Source(text, "args.xe"), tree)
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        self.assertEqual(checker.warnings, [], "\n".join(w.render() for w in checker.warnings))
        self.assertEqual(tree, original)
        return checker

    def test_registry_returns_existing_readonly_slice_and_error_types(self):
        signature = env_function("std::env::args")
        assert signature is not None
        self.assertEqual(signature.parameters, ())
        self.assertEqual(signature.result, ENV_ARGS_RESULT)
        self.assertEqual(ENV_ARGS_RESULT.args, (Type("Slice", (STR,)), IO_ERROR))
        self.assertFalse(signature.formatted)
        for name in ("args", "env::args", "std::io::args", "std::env::arguments"):
            self.assertIsNone(env_function(name))

    def test_successful_argument_view_can_be_copied(self):
        self.check('''fn main() {
            let result << std::env::args();
            let args = result?[panic]; let copied = args;
            println("{} {}", args.len(), copied.len());
        }''')

    def test_fixed_function_value_and_generic_argument(self):
        self.check('''type Args = Slice[str]?[io::Error];
            type Reader = fn() -> Args;
            fn[T] call(reader: fn() -> T) -> T {reader()}
            fn main() {let reader: Reader = std::env::args;
                let args = call[Args](reader)?[panic];}''')

    def test_process_strings_can_be_returned_without_local_pointer_warning(self):
        self.check('''fn program_name() -> str {
            let args = std::env::args()?[panic]; args[0]
        } fn main(){println("{}", program_name());}''')

    def test_parameter_fixture_checks_without_warnings(self):
        self.check((ROOT / "tests/backend/args.xe").read_text(encoding="utf-8"))

    def test_minimal_document_example_matches_the_actual_xe_file(self):
        source = (ROOT / "examples/args/main.xe").read_text(encoding="utf-8")
        documentation = (ROOT / "doc/25.md").read_text(encoding="utf-8")
        example = re.search(r"```xe\n(.*?)\n```", documentation, re.DOTALL)
        self.assertIsNotNone(example)
        assert example is not None
        self.assertEqual(example.group(1).strip(), source.strip())
        self.check(source)

    def test_arity_unknown_path_and_readonly_write_errors(self):
        cases = (
            ('fn main(){std::env::args(1);}', "XE-CALL-0001"),
            ('fn main(){std::env::arguments();}', "XE-NAME-0001"),
            ('fn main(){let a=std::env::args()?[panic];a[0]="bad";}', "XE-MUT-0001"),
            ('fn main(){let[mut] a=std::env::args()?[panic];a[0]="bad";}', "XE-MUT-0001"),
            ('fn main(){let[mut] a=std::env::args()?[panic];let p=a@[mut];p[0]="bad";}', "XE-MUT-0001"),
            ('fn main(){let[mut] a=std::env::args()?[panic];let p=a[0]@[mut];}', "XE-MUT-0001"),
        )
        for text, code in cases:
            with self.subTest(text=text):
                errors = check_source(text, "args.xe")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, code, errors[0].render())

    def test_args_name_is_not_a_new_reserved_prelude_name(self):
        self.check('fn args()->i32{42} fn main(){let args=args();println("{}",args);}')


class RunArgumentParsingTests(unittest.TestCase):
    def test_separator_keeps_argument_boundaries_and_does_not_call_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "main.xe", Path(directory) / "program"
            with patch("compiler.xe_ast.build.build_executable") as build, \
                    patch("compiler.xe_ast.cli.subprocess.call", return_value=7) as run:
                status = main([str(source), "--run", "-o", str(output), "--",
                               "", "two words", "--help", "--", "$(not-a-shell)"])
            self.assertEqual(status, 7)
            build.assert_called_once()
            run.assert_called_once_with([str(output.resolve()), "", "two words",
                                         "--help", "--", "$(not-a-shell)"])

    def test_program_arguments_are_rejected_for_nonrun_actions(self):
        for action in ("--check", "--build", "--emit-c"):
            with self.subTest(action=action), contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as caught:
                    main(["missing.xe", action, "--", "--help"])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("只能与 --run", err.getvalue())


@unittest.skipUnless(CC, "参数库运行测试需要系统 C 编译器")
class EnvExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="xe-env-args-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.program = cls.root / "args"
        build_executable(ROOT / "tests/backend/args.xe", cls.program, cc=CC)

    def compile(self, source, name, flags=()):
        path, program = self.root / (name + ".xe"), self.root / name
        path.write_text(source, encoding="utf-8")
        build_executable(path, program, cc=CC, extra_flags=flags)
        return program

    def test_no_user_arguments_still_includes_program_name(self):
        result = subprocess.run([str(self.program)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"count: 1\n0: {self.program} ({len(str(self.program).encode())} bytes)\n")
        self.assertEqual(result.stderr, "")

    def test_empty_unicode_spaces_flags_and_long_arguments_are_not_resplit(self):
        arguments = ["", "two words", "你好 🌍", "--help", "--", "single'quote", "double\"quote", "x" * 16384]
        result = subprocess.run([str(self.program), *arguments], capture_output=True, text=True, timeout=10)
        all_arguments = [str(self.program), *arguments]
        expected = f"count: {len(all_arguments)}\n" + "".join(
            f"{index}: {arg} ({len(arg.encode())} bytes)\n" for index, arg in enumerate(all_arguments))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")

    def test_function_value_and_generic_use_same_interface(self):
        program = self.compile('''type Args = Slice[str]?[io::Error];
            fn[T] call(reader: fn() -> T) -> T {reader()}
            fn main(){let reader=std::env::args;
                let args=call[Args](reader)?[panic]; println("{}",args[1]);}''', "function_value")
        result = subprocess.run([str(program), "hello Xe"], capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "hello Xe\n", ""))

    @unittest.skipUnless(os.name == "posix", "原始字节参数验收依赖 POSIX")
    def test_invalid_utf8_returns_error_not_panic_or_replacement_text(self):
        for value in (b"\xff", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"\xe4\xb8"):
            with self.subTest(value=value):
                result = subprocess.run([os.fsencode(self.program), value], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(result.stderr.startswith(b"argument error: "), result.stderr)

    @unittest.skipUnless(os.name == "posix", "原始字节参数验收依赖 POSIX")
    def test_unused_argument_library_does_not_reject_invalid_argument(self):
        program = self.compile('fn main(){println("unused");}', "unused")
        result = subprocess.run([os.fsencode(program), b"\xff"], capture_output=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"unused\n", b""))

    def test_generated_c_compiles_outside_repository(self):
        generated = lower_to_c('fn main(){let args=std::env::args()?[panic];println("{}",args[1]);}')
        self.assertNotIn('#include "../../stdlib/env/xe_env.h"', generated)
        with tempfile.TemporaryDirectory(prefix="xe-env-standalone-") as directory:
            root = Path(directory)
            path, program = root / "args.c", root / "args"
            path.write_text(generated, encoding="utf-8")
            compiled = subprocess.run([CC, "-std=c11", str(path), "-o", str(program)],
                                      cwd=root, capture_output=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(program), "standalone"], cwd=root, capture_output=True, timeout=10)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"standalone\n", b""))

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie 验收针对 Linux")
    def test_repeated_args_and_drop_use_process_storage_without_leaks(self):
        program = self.compile('''struct Trace;
            impl Drop for Trace {fn drop(self:Self@[mut]){
                let args=std::env::args()?[panic];println("drop {}",args[1]);}}
            fn main()->i32{let trace << Trace{};
                let first=std::env::args()?[panic];
                for i in 0..200 {let again=std::env::args()?[panic];
                    if again.len()!=first.len() or again[1]!=first[1] {return 1;}}
                println("{}",first[1]);return 7;}''', "sanitized", (
                    "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
        result = subprocess.run([str(program), "你好"], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, ASAN_OPTIONS="detect_leaks=1:abort_on_error=1"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, "你好\ndrop 你好\n", ""))

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie 验收针对 Linux")
    def test_invalid_args_error_cleanup_can_be_repeated(self):
        program = self.compile('''fn main()->i32{
            for i in 0..100 {let failed=std::env::args()? 1> _ -> false 2> _ -> true;
                if not failed {return 1;}}
            0}''', "sanitized_error", (
                "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
        result = subprocess.run([os.fsencode(program), b"valid", b"\xff"], capture_output=True, timeout=10,
                                env=dict(os.environ, ASAN_OPTIONS="detect_leaks=1:abort_on_error=1"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_compiler_run_forwards_arguments_after_separator(self):
        output = self.root / "cli_args"
        result = subprocess.run([sys.executable, str(ROOT / "compiler/main.py"),
            str(ROOT / "tests/backend/args.xe"), "--run", "-o", str(output), "--",
            "--help", "", "two words", "--"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("count: 5\n", result.stdout)
        self.assertTrue(result.stdout.endswith(
            "1: --help (6 bytes)\n2:  (0 bytes)\n3: two words (9 bytes)\n4: -- (2 bytes)\n"))
        self.assertEqual(result.stderr, "")

    def test_minimal_print_tool_is_only_one_argument_per_line(self):
        program = self.root / "minimal"
        build_executable(ROOT / "examples/args/main.xe", program, cc=CC)
        for arguments in ([], ["hello", "two words", "你好 Xe", "", "--help"]):
            with self.subTest(arguments=arguments):
                process = subprocess.Popen([str(program), *arguments], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    # 保持 stdin 打开也应退出：这个工具只能读参数，不能等交互输入。
                    process.wait(timeout=5)
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assertEqual(stdout, "\n".join([str(program), *arguments]) + "\n")
                    self.assertEqual(stderr, "")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()

    @unittest.skipUnless(os.name == "posix", "原始字节参数验收依赖 POSIX")
    def test_minimal_tool_reports_unreadable_arguments(self):
        program = self.root / "minimal_error"
        build_executable(ROOT / "examples/args/main.xe", program, cc=CC)
        result = subprocess.run([os.fsencode(program), b"\xff"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr.startswith("无法读取参数：".encode()), result.stderr)

    def test_array_and_both_slice_lengths_use_usize(self):
        program = self.compile('''fn main(){let[mut] items=[10,20,30];
            let read=items.slice(1..);let write=items.slice_mut(..);
            println("{} {} {}",items.len(),read.len(),write.len());
            let p=read@;println("{}",p.len());}''', "lengths")
        result = subprocess.run([str(program)], capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "3 2 3\n2\n", ""))


@unittest.skipUnless(CC and sys.platform.startswith("linux"),
                     "native 参数边界与 ASan no-pie 验收需要 Linux C 编译器")
class NativeEnvRuntimeTests(unittest.TestCase):
    def test_empty_args_allocation_failure_and_cached_storage_cleanup(self):
        # 操作系统入口通常不会产生 argc=0，真实 argv 也不能可靠制造 ENOMEM。
        # 因此单独调用 native 层，并仅替换 env 的 malloc；不模拟 Xe 分流代码。
        # ASan/UBSan 负责检查失败路径是否遗留描述符以及 cleanup 是否重复释放。
        runtime = (ROOT / "compiler/runtime/xe_runtime.h").read_text(encoding="utf-8")
        io_library = (ROOT / "stdlib/io/xe_io.h").read_text(encoding="utf-8")
        env_library = (ROOT / "stdlib/env/xe_env.h").read_text(encoding="utf-8")
        runtime = runtime.replace('#include "../../stdlib/io/xe_io.h"', io_library)
        runtime = runtime.replace('#include "../../stdlib/env/xe_env.h"',
            "#define malloc env_test_malloc\n" + env_library + "\n#undef malloc")
        harness = '''#include <stddef.h>
#include <assert.h>
static int allocation_attempts, refuse_allocation;
static void *env_test_malloc(size_t bytes);
''' + runtime + '''
static void *env_test_malloc(size_t bytes) {
    ++allocation_attempts;
    return refuse_allocation ? NULL : malloc(bytes);
}
int main(void) {
    /* 空参数表不用分配，也不解引用 NULL argv。 */
    xe_env_init(0, NULL);
    XeEnvArgs empty = xe_env_args();
    assert(!empty.error && !empty.len && !empty.data);
    assert(!allocation_attempts);
    xe_env_cleanup();

    char *values[] = {"tool", "", "spaces here"};
    refuse_allocation = 1;
    xe_env_init(3, values);
    XeEnvArgs failed = xe_env_args();
    assert(failed.error == ENOMEM && !failed.data && !failed.len);
    /* 即使稍后分配恢复，当前进程读取结果仍缓存；不会反复尝试。 */
    refuse_allocation = 0;
    assert(xe_env_args().error == ENOMEM && allocation_attempts == 1);
    xe_env_cleanup();

    char invalid[] = {(char)0xff, 0};
    char *bad[] = {"tool", invalid};
    xe_env_init(2, bad);
    failed = xe_env_args();
    assert(failed.error == EILSEQ && !failed.data && !failed.len);
    bad[1] = "now valid";
    assert(xe_env_args().error == EILSEQ && allocation_attempts == 2);
    xe_env_cleanup();

    xe_env_init(3, values);
    XeEnvArgs first = xe_env_args(), second = xe_env_args();
    assert(!first.error && first.len == 3 && allocation_attempts == 3);
    assert(first.data == second.data && first.len == second.len);
    assert(first.data[1].len == 0 && first.data[2].len == 11);
    /* 字符串本身仍是原 argv 字符串，不是复制分配或由库释放。 */
    assert(first.data[0].data == (const unsigned char *)values[0]);
    xe_env_cleanup();
    xe_env_cleanup();
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="xe-env-native-") as directory:
            program = Path(directory) / "native_args"
            compiled = subprocess.run([CC, "-std=c11", "-fsanitize=address,undefined",
                "-fno-sanitize-recover=all", "-no-pie", "-x", "c", "-", "-o", str(program)],
                input=harness, text=True, capture_output=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(program)], capture_output=True, timeout=10,
                env=dict(os.environ, ASAN_OPTIONS="detect_leaks=1:abort_on_error=1"))
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))


if __name__ == "__main__":
    unittest.main()
