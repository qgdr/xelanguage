"""标准 IO 的终端策略：真实管道/PTY、环境变量和普通函数值。

PTY 只用来构造真实终端描述符，不替换 isatty 的返回值。分别改变三个流，
避免把 stdin 是终端误认为 stdout/stderr 也一定是终端。
"""
import errno
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.semantic import check_source
from compiler.xe_ast.stdlib_io import IO_NATIVE_FUNCTIONS, io_function
from compiler.xe_ast.typesys import BOOL


ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which("cc")
HAS_PTY = os.name == "posix" and hasattr(os, "openpty")
PROBE = '''fn main() {
    println("{} {} {} {} {}", std::io::stdin_is_terminal(),
        std::io::stdout_is_terminal(), std::io::stderr_is_terminal(),
        std::io::stdout_supports_color(), std::io::stderr_supports_color());
}'''


class StandardIoTerminalSemanticTests(unittest.TestCase):
    def test_terminal_functions_have_normal_zero_argument_bool_signatures(self):
        self.assertEqual(len(IO_NATIVE_FUNCTIONS), 5)
        for name in IO_NATIVE_FUNCTIONS:
            with self.subTest(name=name):
                signature = io_function(name)
                self.assertEqual(signature.parameters, ())
                self.assertEqual(signature.result, BOOL)
                self.assertFalse(signature.formatted)
                self.assertIs(signature, io_function("std::io::" + name))
                source = f'''fn main() {{let query: fn() -> bool = std::io::{name};
                    let answer: bool = query(); println("{{}}", answer);}}'''
                self.assertEqual(check_source(source), [])

    def test_terminal_functions_reject_arguments_and_unknown_paths(self):
        for name in IO_NATIVE_FUNCTIONS:
            with self.subTest(name=name):
                errors = check_source(f"fn main() {{std::io::{name}(1);}}")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-CALL-0001")
        for name in ("std::io::is_terminal", "other::stdout_is_terminal"):
            with self.subTest(name=name):
                errors = check_source(f"fn main() {{{name}();}}")
                self.assertTrue(errors)
                self.assertEqual(errors[0].code, "XE-NAME-0001")


@unittest.skipUnless(CC, "终端策略运行验收需要系统 C 编译器")
class StandardIoTerminalExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="xe-io-terminal-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        source = cls.root / "probe.xe"
        source.write_text(PROBE, encoding="utf-8")
        cls.program = cls.root / "probe"
        build_executable(source, cls.program, cc=CC,
                         extra_flags=("-Werror=implicit-function-declaration",))

    def probe(self, terminals=(False, False, False), overrides=None, program=None):
        environment = dict(os.environ)
        environment.pop("NO_COLOR", None)
        environment["TERM"] = "xterm-256color"
        for name, value in (overrides or {}).items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        master = slave = None
        try:
            if any(terminals):
                master, slave = os.openpty()
            result = subprocess.run([str(program or self.program)],
                stdin=slave if terminals[0] else subprocess.DEVNULL,
                stdout=slave if terminals[1] else subprocess.PIPE,
                stderr=slave if terminals[2] else subprocess.PIPE,
                env=environment, timeout=10)
            if slave is not None:
                os.close(slave)
                slave = None
            tty_output = b""
            if master is not None:
                while True:
                    try:
                        block = os.read(master, 4096)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break  # Linux PTY 的所有 slave 关闭后读端以 EIO 结束。
                        raise
                    if not block:
                        break
                    tty_output += block
            stdout = tty_output if terminals[1] else result.stdout
            stderr = b"" if terminals[2] else result.stderr
            if not terminals[1]:
                self.assertEqual(tty_output, b"", "终端 stderr 不应出现诊断")
            self.assertEqual(result.returncode, 0, stderr)
            self.assertEqual(stderr, b"")
            return stdout.decode().split()
        finally:
            if slave is not None:
                os.close(slave)
            if master is not None:
                os.close(master)

    def test_pipe_or_file_descriptors_are_not_terminals_or_color_outputs(self):
        self.assertEqual(self.probe(), ["false"] * 5)
        self.assertEqual(self.probe(overrides={"NO_COLOR": "", "TERM": None}), ["false"] * 5)

    @unittest.skipUnless(HAS_PTY, "独立终端流验收需要 POSIX PTY")
    def test_three_terminal_streams_are_detected_independently(self):
        cases = (
            ((True, False, False), ["true", "false", "false", "false", "false"]),
            ((False, True, False), ["false", "true", "false", "true", "false"]),
            ((False, False, True), ["false", "false", "true", "false", "true"]),
            ((True, True, True), ["true"] * 5),
        )
        for descriptors, expected in cases:
            with self.subTest(descriptors=descriptors):
                self.assertEqual(self.probe(descriptors), expected)

    @unittest.skipUnless(HAS_PTY, "颜色环境策略验收需要 POSIX PTY")
    def test_no_color_and_term_dumb_disable_color_but_not_terminal_detection(self):
        cases = (
            ({"NO_COLOR": "1"}, False),
            ({"NO_COLOR": "0"}, False),  # 非空即禁用，不把 "0" 当作 false。
            ({"NO_COLOR": " "}, False),
            ({"NO_COLOR": ""}, True),
            ({"NO_COLOR": None, "TERM": None}, True),
            ({"NO_COLOR": None, "TERM": "dumb"}, False),
            ({"NO_COLOR": "", "TERM": "dumb"}, False),
        )
        for environment, color in cases:
            with self.subTest(environment=environment):
                expected = ["true"] * 3 + ["true" if color else "false"] * 2
                self.assertEqual(self.probe((True, True, True), environment), expected)

    def test_function_values_generic_calls_and_prelude_names_work(self):
        source = '''fn[T] call(query: fn() -> T) -> T {query()}
            fn main(){let input = std::io::stdin_is_terminal;
                let output = stdout_is_terminal;
                let errors = std::io::stderr_is_terminal;
                let color = stdout_supports_color;
                let error_color = std::io::stderr_supports_color;
                println("{} {} {} {} {}", call[bool](input), output(),
                    errors(), call[bool](color), error_color());}'''
        path, program = self.root / "function_values.xe", self.root / "function_values"
        path.write_text(source, encoding="utf-8")
        build_executable(path, program, cc=CC)
        self.assertEqual(self.probe(program=program), ["false"] * 5)

    def test_prelude_names_can_still_be_shadowed_by_user_functions(self):
        path, program = self.root / "shadow.xe", self.root / "shadow"
        path.write_text('''fn stdout_is_terminal()->i32{7}
            fn main(){println("{} {}",stdout_is_terminal(),std::io::stdout_is_terminal());}''',
            encoding="utf-8")
        build_executable(path, program, cc=CC)
        self.assertEqual(self.probe(program=program), ["7", "false"])

    def test_generated_c_has_no_repository_include_dependency(self):
        generated = lower_to_c(PROBE, "terminal.xe")
        self.assertNotIn('#include "../../stdlib/io/xe_io.h"', generated)
        with tempfile.TemporaryDirectory(prefix="xe-terminal-standalone-") as directory:
            root = Path(directory)
            source, program = root / "probe.c", root / "probe"
            source.write_text(generated, encoding="utf-8")
            compiled = subprocess.run([CC, "-std=c11", "-Werror=implicit-function-declaration",
                str(source), "-o", str(program)], cwd=root, capture_output=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            self.assertEqual(self.probe(program=program), ["false"] * 5)


if __name__ == "__main__":
    unittest.main()
