"""标准 IO 的真实运行验收：字节、错误、独立构建和资源生命周期。

输入按 bytes 传递，避免 Python 的文本换行/解码替我们修正错误输入。
正常输出以及 IO 错误都通过真正编译的 Xe 程序验证，而非模拟标准库。
"""
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which("cc") or ""


@unittest.skipUnless(CC, "标准 IO 验收需要系统 C 编译器")
class StdlibIoExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="xe-stdlib-io-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.reader = cls.root / "reader"
        build_executable(ROOT / "tests/backend/readline.xe", cls.reader, cc=CC)

    def compile(self, source, name="test", flags=()):
        path = self.root / (name + ".xe")
        path.write_text(source, encoding="utf-8")
        program = self.root / name
        build_executable(path, program, cc=CC, extra_flags=flags)
        return program

    def run_reader(self, data):
        result = subprocess.run([str(self.reader)], input=data,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        return result.stdout

    def test_empty_input_is_eof_but_empty_line_is_owned_string(self):
        self.assertEqual(self.run_reader(b""), b"eof\n")
        self.assertEqual(self.run_reader(b"\n\n"), b"line: \nline: \neof\n")

    def test_crlf_and_unterminated_final_line(self):
        self.assertEqual(self.run_reader(b"first\r\nsecond\nlast"),
                         b"line: first\nline: second\nline: last\neof\n")
        # Only CR immediately before LF belongs to a CRLF line ending.
        self.assertEqual(self.run_reader(b"last\r"), b"line: last\r\neof\n")

    def test_unicode_and_embedded_nul_are_not_truncated(self):
        data = "你好，Xe! 🌍".encode() + b"\x00after\n"
        self.assertEqual(self.run_reader(data), b"line: " + data + b"eof\n")

    def test_long_line_uses_growing_buffer(self):
        payload = "你🌍好".encode() * 16384
        self.assertEqual(self.run_reader(payload + b"\n"),
                         b"line: " + payload + b"\neof\n")

    def test_invalid_utf8_is_error_not_empty_line_or_eof(self):
        for data in (b"\xff\n", b"\xc0\xaf\n", b"\xed\xa0\x80\n", b"\xf4\x90\x80\x80\n", b"\xe4\xb8"):
            with self.subTest(data=data):
                result = subprocess.run([str(self.reader)], input=data,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(result.stderr.startswith(b"read error: "), result.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "目录读取错误验收针对 Linux")
    def test_directory_stdin_reports_io_error(self):
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            result = subprocess.run([str(self.reader)], stdin=descriptor,
                                    capture_output=True, timeout=10)
        finally:
            os.close(descriptor)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr.startswith(b"read error: "), result.stderr)

    def test_qualified_prints_keep_formatting_and_owned_value_rules(self):
        program = self.compile('''fn main() {
            let text << String::from("kept");
            std::io::print("{} {} {{}} ", true, 42);
            std::io::println("{}", text@);
            println("{}", text);
            std::io::eprintln("error {}", '猫');
        }''', "qualified")
        result = subprocess.run([str(program)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"true 42 {} kept\nkept\n")
        self.assertEqual(result.stderr, "error 猫\n".encode())

    def test_readline_as_fixed_signature_function_value_and_generic_argument(self):
        program = self.compile('''type Reader = fn() -> String??[std::io::Error];
        fn[T] identity(value: T) -> T { value }
        fn main() -> i32 {
            let reader: Reader = identity(std::io::readline);
            let reading << reader();
            reading ?
                1> line -> line ?
                    1> text -> { println("{}", text); 0 }
                    2> _ -> 2
                2> error -> { eprintln("{}", error); 3 }
        }''', "function_value")
        for data, code, output in ((b"hello\n", 0, b"hello\n"), (b"", 2, b"")):
            with self.subTest(data=data):
                result = subprocess.run([str(program)], input=data,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout, output)
                self.assertEqual(result.stderr, b"")

    def test_readline_flushes_prompt_before_waiting_for_input(self):
        program = self.compile('''fn main() {
            print("prompt> ");
            let reading << readline();
        }''', "prompt")
        process = subprocess.Popen([str(program)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert process.stdout is not None  # stdout 明确配置为 PIPE。
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), "输入前提示文字没有刷新")
            self.assertEqual(os.read(process.stdout.fileno(), 8), b"prompt> ")
            stdout, stderr = process.communicate(b"answer\n", timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual((stdout, stderr), (b"", b""))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def test_constant_can_shadow_unqualified_readline(self):
        program = self.compile('''const readline: i32 = 7;
            fn main() { println("{}", readline); }''', "constant_shadow")
        result = subprocess.run([str(program)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"7\n")
        self.assertEqual(result.stderr, b"")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie 验收针对 Linux")
    def test_generic_readline_call_result_is_automatically_dropped(self):
        program = self.compile('''type Input = String??[io::Error];
        fn[T] call(reader: fn() -> T) -> T { reader() }
        fn main() { let result << call[Input](std::io::readline); }
        ''', "generic_call", (
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
        for data in (b"\n", b"owned\n", b"", b"\xff\n"):
            with self.subTest(data=data):
                result = subprocess.run([str(program)], input=data,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((result.stdout, result.stderr), (b"", b""))

    @unittest.skipUnless(Path("/dev/full").exists(), "刷新失败验收需要 /dev/full")
    def test_prompt_flush_failure_is_returned_without_reading(self):
        program = self.compile('''fn main() -> i32 {
            print("prompt");
            readline() ?
                1> line -> { 2 }
                2> error -> { eprintln("flush error: {}", error); 7 }
        }''', "flush_failure")
        with Path("/dev/full").open("wb") as output:
            result = subprocess.run([str(program)], input=b"", stdout=output,
                                    stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertTrue(result.stderr.startswith(b"flush error: "), result.stderr)

    def test_generated_c_is_standalone_outside_repository(self):
        source = (ROOT / "tests/backend/readline.xe").read_text(encoding="utf-8")
        generated = lower_to_c(source, "standalone.xe")
        self.assertNotIn('#include "../../stdlib/io/xe_io.h"', generated)
        with tempfile.TemporaryDirectory(prefix="xe-standalone-io-") as directory:
            root = Path(directory)
            path, program = root / "standalone.c", root / "program"
            path.write_text(generated, encoding="utf-8")
            compile_result = subprocess.run([CC, "-std=c11", str(path), "-o", str(program)],
                                            cwd=root, capture_output=True, timeout=30)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run([str(program)], input=b"outside\n",
                                    cwd=root, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"line: outside\neof\n")

    @unittest.skipUnless(Path("/dev/full").exists(), "输出失败验收需要 /dev/full")
    def test_buffered_stdout_failure_prevents_successful_exit(self):
        for name in ("print", "println"):
            with self.subTest(name=name):
                program = self.compile('fn main() { ' + name + '("buffered {}", 42); }',
                                       "full_" + name)
                with Path("/dev/full").open("wb") as output:
                    result = subprocess.run([str(program)], stdout=output,
                                            stderr=subprocess.PIPE, timeout=10)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(b"output failed", result.stderr)

    @unittest.skipUnless(Path("/dev/full").exists(), "输出失败验收需要 /dev/full")
    def test_stderr_failure_prevents_successful_exit(self):
        program = self.compile('fn main() { eprintln("stderr {}", 42); }', "full_stderr")
        with Path("/dev/full").open("wb") as output:
            result = subprocess.run([str(program)], stdout=subprocess.PIPE,
                                    stderr=output, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")

    @unittest.skipUnless(sys.platform.startswith("linux"), "ASan no-pie 验收针对 Linux")
    def test_repeated_inputs_drop_resources_under_sanitizers(self):
        source = (ROOT / "tests/backend/readline.xe").read_text(encoding="utf-8")
        program = self.compile(source, "sanitized", (
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
        payload = (b"\n" + "你好🌍".encode() + b"\x00tail\r\n") * 200
        env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1:abort_on_error=1")
        result = subprocess.run([str(program)], input=payload,
                                capture_output=True, timeout=10, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(result.stdout.count(b"line: "), 400)
        # Exercise allocated-buffer cleanup on invalid UTF-8, not only successful lines.
        result = subprocess.run([str(program)], input=b"x" * 8192 + b"\xff\n",
                                capture_output=True, timeout=10, env=env)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(b"Sanitizer", result.stderr)
        self.assertNotIn(b"runtime error:", result.stderr)


if __name__ == "__main__":
    unittest.main()
