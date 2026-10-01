"""真实 Xe 命令行工具的参数、交互、EOF 与资源清理测试。"""
import errno
import os
import re
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

if os.name == "posix":
    import pty
    import termios

from compiler.xe_ast.build import build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Source

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "examples/feature_check/main.xe"
CC = shutil.which("cc") or ""
FEATURES = (
    "numeric", "comparison-chain", "short-circuit", "evaluation-order",
    "struct-copy-methods", "resource-drop", "generics", "tuple-alias",
    "enum-owning", "enum-pointer", "maybe-conversion", "array-slice-range",
    "function-pipeline", "string-utf8", "pointer-permissions", "resource-array",
)


class TerminalSession:
    """真实 PTY 会话：先等到提示，再输入，避免用固定 sleep 猜测刷新时机。"""

    def __init__(self, case, program, arguments=(), *, stdin_terminal=True, stdout_terminal=True,
                 stderr_terminal=True, environment=None):
        self.case = case
        self.output = b""
        self.cursor = 0
        self.master, slave = pty.openpty()
        options = termios.tcgetattr(slave)
        # 禁止终端自行回显输入：断言观察到的文字必须来自 Xe 程序。
        options[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, options)
        env = os.environ.copy()
        env.pop("NO_COLOR", None)
        env["TERM"] = "xterm-256color"
        env.update(environment or {})
        try:
            self.process = subprocess.Popen(
                [str(program), *arguments], stdin=slave if stdin_terminal else subprocess.PIPE,
                stdout=slave if stdout_terminal else subprocess.PIPE,
                stderr=slave if stderr_terminal else subprocess.PIPE,
                cwd=ROOT, env=env,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)

    def read_ready(self, timeout):
        if not select.select([self.master], [], [], timeout)[0]:
            return False
        try:
            chunk = os.read(self.master, 65536)
        except OSError as error:
            if error.errno == errno.EIO:  # Linux PTY 在子进程关闭后返回 EIO。
                return False
            raise
        self.output += chunk
        return bool(chunk)

    def expect(self, marker, timeout=10):
        deadline = time.monotonic() + timeout
        while marker not in self.output[self.cursor:]:
            remaining = deadline - time.monotonic()
            self.case.assertGreater(remaining, 0, repr(self.output))
            if not self.read_ready(remaining):
                self.case.fail("终端未输出预期内容：" + repr(marker) + "\n" + repr(self.output))
        self.cursor = self.output.index(marker, self.cursor) + len(marker)

    def send(self, data):
        if self.process.stdin is None:
            os.write(self.master, data)
        else:
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def finish(self, status=0):
        self.case.assertEqual(self.process.wait(timeout=10), status)
        stdout, stderr = self.process.communicate(timeout=10)
        while self.read_ready(0):
            pass
        all_output = self.output + (stdout or b"") + (stderr or b"")
        self.case.assertNotIn(b"AddressSanitizer", all_output)
        self.case.assertNotIn(b"runtime error:", all_output)
        return self.output.replace(b"\r\n", b"\n"), stdout or b"", stderr or b""

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.communicate(timeout=10)
        os.close(self.master)


class FeatureCheckSemanticTests(unittest.TestCase):
    def test_project_is_valid_and_uses_live_pointers(self):
        text = SOURCE.read_text(encoding="utf-8")
        checker = Checker(Source(text, str(SOURCE)), parse_source(text, str(SOURCE)))
        errors = checker.check()
        self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
        self.assertEqual(checker.warnings, [], "\n".join(warning.render() for warning in checker.warnings))


@unittest.skipUnless(CC, "运行项目需要系统 C 编译器")
class FeatureCheckExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="xe-feature-check-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.program = Path(cls.directory.name) / "feature_check"
        build_executable(SOURCE, cls.program, cc=CC,
                         extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))

    def run_commands(self, text, status=0):
        result = subprocess.run([str(self.program)], input=text, capture_output=True,
                                text=True, cwd=ROOT, timeout=10)
        self.assertEqual(result.returncode, status, result.stderr)
        if status == 0:
            self.assertEqual(result.stderr, "")
        self.assertNotIn("AddressSanitizer", result.stderr)
        self.assertNotIn("runtime error:", result.stderr)
        return result

    @contextmanager
    def terminal(self, *arguments, program=None, **options):
        session = TerminalSession(self, program or self.program, arguments, **options)
        try:
            yield session
        finally:
            session.close()

    def run_arguments(self, *arguments, status=0):
        # 故意提供不是 UTF-8 的 stdin。参数模式不应尝试读取它，
        # 也不应输出交互 banner；这是脚本调用的输出稳定性约定。
        result = subprocess.run([str(self.program), *arguments], input=b"\xff\n",
                                capture_output=True, cwd=ROOT, timeout=10)
        self.assertEqual(result.returncode, status, result.stderr)
        stderr = result.stderr.decode("utf-8")
        if status == 0:
            self.assertEqual(stderr, "")
        self.assertNotIn("AddressSanitizer", stderr)
        self.assertNotIn("runtime error:", stderr)
        self.assertNotIn(b"Xe feature check:", result.stdout)
        return result

    def test_argument_check_executes_all_features_without_reading_stdin(self):
        result = self.run_arguments("check")
        lines = result.stdout.decode("utf-8").splitlines()
        self.assertEqual(lines, ["PASS " + feature for feature in FEATURES]
                         + ["summary: 16 passed, 0 failed"])

    def test_argument_help_aliases_and_quit(self):
        for command in ("help", "--help", "-h"):
            with self.subTest(command=command):
                result = self.run_arguments(command)
                self.assertTrue(result.stdout.startswith(b"usage: xe-feature-check "))
                self.assertNotIn(b"PASS ", result.stdout)
        result = self.run_arguments("quit")
        self.assertEqual(result.stdout, b"")

    def test_argument_echo_preserves_unicode_spaces_and_empty_arguments(self):
        cases = (
            ((), "\n"),
            (("你好，Xe!", "🌍"), "你好，Xe! 🌍\n"),
            (("two words", "", "end"), "two words  end\n"),
            (("", "中间", ""), " 中间 \n"),
            (("--help",), "--help\n"),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                result = self.run_arguments("echo", *arguments)
                self.assertEqual(result.stdout.decode("utf-8"), expected)

    def test_argument_unknown_command_reports_usage_and_status_two(self):
        for command in ("未知命令", "", " check "):
            with self.subTest(command=command):
                result = self.run_arguments(command, status=2)
                self.assertEqual(result.stdout, b"")
                stderr = result.stderr.decode("utf-8")
                self.assertTrue(stderr.startswith("unknown command: " + command + "\n"))
                self.assertIn("usage: xe-feature-check ", stderr)

    def test_extra_arguments_are_not_silently_ignored(self):
        for command in ("help", "--help", "-h", "check", "quit"):
            with self.subTest(command=command):
                result = self.run_arguments(command, "extra", status=2)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"unexpected arguments for command: ", result.stderr)
                self.assertIn(b"usage: xe-feature-check ", result.stderr)

    def test_argument_mode_finishes_while_stdin_pipe_remains_open(self):
        # 不写入、也不关闭 stdin。真正调用 readline 的程序会一直等，
        # 因而这个测试比给一个立即 EOF 的输入更能证明不依赖标准输入。
        process = subprocess.Popen([str(self.program), "check"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
        try:
            self.assertEqual(process.wait(timeout=10), 0)
            output, errors = process.communicate(timeout=10)
            self.assertTrue(output.endswith(b"summary: 16 passed, 0 failed\n"))
            self.assertEqual(errors, b"")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    @unittest.skipUnless(os.name == "posix", "原始 argv 字节用例依赖 POSIX")
    def test_invalid_utf8_argument_reports_argument_error(self):
        for arguments in ((b"\xff",), (b"echo", b"valid", b"\xff")):
            with self.subTest(arguments=arguments):
                result = subprocess.run([os.fsencode(self.program), *arguments], input=b"",
                                        capture_output=True, cwd=ROOT, timeout=10)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(result.stderr.startswith(b"argument error: "))
                self.assertNotIn(b"AddressSanitizer", result.stderr)
                self.assertNotIn(b"runtime error:", result.stderr)

    def test_check_compares_all_feature_results(self):
        result = self.run_commands("check\nquit\n")
        lines = result.stdout.splitlines()
        self.assertEqual([line for line in lines if line.startswith("PASS ")],
                         ["PASS " + feature for feature in FEATURES])
        self.assertNotIn("FAIL ", result.stdout)
        self.assertEqual(lines[-1], "summary: 16 passed, 0 failed")

    def test_help_echo_unicode_and_quit(self):
        result = self.run_commands("help\necho\n你好，Xe! 🌍\nquit\ncheck\n")
        self.assertEqual(result.stdout.count("commands: help | check | echo | quit"), 2)
        self.assertEqual(result.stdout.splitlines()[-1], "你好，Xe! 🌍")
        self.assertNotIn("PASS ", result.stdout)

    def test_empty_stdin_and_blank_commands_exit_normally(self):
        for text in ("", "\n\n", "quit", "echo\n"):
            with self.subTest(text=text):
                result = self.run_commands(text)
                self.assertNotIn("PASS ", result.stdout)

    def test_empty_echo_line_is_a_real_line(self):
        result = self.run_commands("echo\n\nquit\n")
        self.assertTrue(result.stdout.endswith("normally.\n\n"), result.stdout)

    def test_unknown_interactive_command_warns_and_continues(self):
        result = subprocess.run([str(self.program)], input="未知命令\nhelp\ncheck\nquit\n",
                                capture_output=True, text=True, cwd=ROOT, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "unknown command: 未知命令\n")
        self.assertEqual(result.stdout.count("commands: help | check | echo | quit"), 2)
        self.assertIn("summary: 16 passed, 0 failed\n", result.stdout)
        self.assertNotIn("\x1b[", result.stdout + result.stderr)
        self.assertNotIn("xe> ", result.stdout)
        self.assertNotIn("AddressSanitizer", result.stderr)
        self.assertNotIn("runtime error:", result.stderr)

    def test_repeated_unknown_commands_release_strings_and_keep_success_status(self):
        result = subprocess.run([str(self.program)],
                                input="未知指令\n" * 100 + "check\nquit\n",
                                capture_output=True, text=True, cwd=ROOT, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "unknown command: 未知指令\n" * 100)
        self.assertTrue(result.stdout.endswith("summary: 16 passed, 0 failed\n"))
        self.assertNotIn("AddressSanitizer", result.stderr)
        self.assertNotIn("runtime error:", result.stderr)

    @unittest.skipUnless(os.name == "posix", "真实 PTY 测试依赖 POSIX")
    def test_terminal_prompts_colors_echo_and_unknown_command_recovery(self):
        with self.terminal() as terminal:
            # 这里还没有发送任何输入。能观察到无换行的 xe> 即证明
            # readline 之前确实刷新了 stdout，而不是退出时才补输出。
            terminal.expect(b"xe> ")
            terminal.send("未知命令\n".encode("utf-8"))
            terminal.expect("unknown command: 未知命令".encode("utf-8"))
            terminal.expect(b"xe> ")
            terminal.send(b"help\n")
            terminal.expect(b"commands: help | check | echo | quit")
            terminal.expect(b"xe> ")
            terminal.send(b"check\n")
            terminal.expect(b"summary: 16 passed, 0 failed")
            terminal.expect(b"xe> ")
            terminal.send(b"echo\n")
            terminal.expect(b"text> ")
            terminal.send("你好，Xe! 🌍\n".encode("utf-8"))
            terminal.expect("你好，Xe! 🌍".encode("utf-8"))
            terminal.expect(b"xe> ")
            terminal.send(b"quit\n")
            output, _, _ = terminal.finish()
        self.assertIn(b"\x1b[36m", output)
        self.assertIn(b"\x1b[32mPASS numeric", output)
        self.assertIn(b"\x1b[31munknown command: ", output)
        self.assertEqual(output.count(b"commands: help | check | echo | quit"), 2)
        self.assertIn("你好，Xe! 🌍\n".encode("utf-8"), output)
        codes = re.findall(rb"\x1b\[(\d+)m", output)
        self.assertEqual(codes[-1], b"0", "颜色必须复位，不能污染用户之后的终端输出")
        self.assertEqual(codes.count(b"0"), sum(code != b"0" for code in codes))

    @unittest.skipUnless(os.name == "posix", "真实 PTY 测试依赖 POSIX")
    def test_terminal_no_color_and_dumb_preserve_prompts_without_ansi(self):
        for environment in ({"NO_COLOR": "1"}, {"TERM": "dumb"}):
            with self.subTest(environment=environment), self.terminal(environment=environment) as terminal:
                terminal.expect(b"xe> ")
                terminal.send(b"unknown\n")
                terminal.expect(b"unknown command: unknown")
                terminal.expect(b"xe> ")
                terminal.send(b"echo\n")
                terminal.expect(b"text> ")
                terminal.send(b"plain text\nquit\n")
                output, _, _ = terminal.finish()
                self.assertIn(b"plain text\n", output)
                self.assertNotIn(b"\x1b[", output)

    @unittest.skipUnless(os.name == "posix", "真实 PTY 测试依赖 POSIX")
    def test_empty_no_color_does_not_disable_terminal_color(self):
        with self.terminal(environment={"NO_COLOR": ""}) as terminal:
            terminal.expect(b"xe> ")
            terminal.send(b"quit\n")
            output, _, _ = terminal.finish()
            self.assertIn(b"\x1b[36m", output)

    @unittest.skipUnless(os.name == "posix", "真实 PTY 测试依赖 POSIX")
    def test_terminal_eof_finishes_prompt_line_and_exits_normally(self):
        for command in (b"", b"echo\n"):
            with self.subTest(command=command), self.terminal() as terminal:
                terminal.expect(b"xe> ")
                if command:
                    terminal.send(command)
                    terminal.expect(b"text> ")
                # canonical 终端在行首收到 Ctrl-D 会提供正常 EOF。
                terminal.send(b"\x04")
                output, _, _ = terminal.finish()
                self.assertTrue(output.endswith(b"\n"), repr(output))

    @unittest.skipUnless(os.name == "posix", "真实 PTY 测试依赖 POSIX")
    def test_redirected_stdout_has_no_prompts_or_color_but_stderr_can_color(self):
        with self.terminal(stdout_terminal=False) as terminal:
            terminal.send(b"unknown\nhelp\ncheck\nquit\n")
            error_output, stdout, stderr = terminal.finish()
        self.assertEqual(stderr, b"")
        self.assertIn(b"\x1b[31munknown command: unknown", error_output)
        self.assertNotIn(b"xe> ", stdout)
        self.assertNotIn(b"\x1b[", stdout)
        self.assertIn(b"summary: 16 passed, 0 failed\n", stdout)

    @unittest.skipUnless(os.name == "posix", "真实 PTY 测试依赖 POSIX")
    def test_redirected_stderr_stays_plain_when_stdout_has_terminal_colors(self):
        with self.terminal(stderr_terminal=False) as terminal:
            terminal.expect(b"xe> ")
            terminal.send(b"unknown\n")
            terminal.expect(b"xe> ")
            terminal.send(b"quit\n")
            stdout, _, stderr = terminal.finish()
        self.assertIn(b"\x1b[36m", stdout)
        self.assertEqual(stderr, b"unknown command: unknown\n")
        self.assertNotIn(b"\x1b[", stderr)

    @unittest.skipUnless(os.name == "posix", "真实 PTY 测试依赖 POSIX")
    def test_pipe_input_suppresses_prompts_but_stdout_terminal_can_still_color(self):
        # 颜色由输出端决定，提示则要求输入和输出都为终端，不能混为同一个开关。
        with self.terminal(stdin_terminal=False) as terminal:
            terminal.send(b"check\nquit\n")
            output, _, _ = terminal.finish()
        self.assertNotIn(b"xe> ", output)
        self.assertNotIn(b"text> ", output)
        self.assertIn(b"\x1b[32mPASS numeric", output)

    @unittest.skipUnless(os.name == "posix", "真实 PTY 测试依赖 POSIX")
    def test_argument_check_can_color_but_echo_body_is_never_colored(self):
        with self.terminal("check") as terminal:
            output, _, _ = terminal.finish()
        self.assertIn(b"\x1b[32mPASS numeric", output)
        self.assertNotIn(b"xe> ", output)
        with self.terminal("echo", "你好 Xe", "") as terminal:
            output, _, _ = terminal.finish()
        self.assertEqual(output, "你好 Xe \n".encode("utf-8"))

    def test_checks_can_repeat_without_leaks_or_double_drops(self):
        result = self.run_commands("check\n" * 5)
        self.assertEqual(result.stdout.count("summary: 16 passed, 0 failed"), 5)
        self.assertEqual(sum(line.startswith("PASS ") for line in result.stdout.splitlines()), 80)

    def test_long_unicode_echo_and_unterminated_final_line(self):
        payload = "你🌍好" * 4096
        result = self.run_commands("echo\n" + payload)
        self.assertEqual(result.stdout.splitlines()[-1], payload)

    def test_crlf_commands_and_echo(self):
        result = self.run_commands("echo\r\nWindows 文本\r\ncheck\r\nquit\r\n")
        self.assertIn("\nWindows 文本\n", result.stdout)
        self.assertTrue(result.stdout.endswith("summary: 16 passed, 0 failed\n"))

    def assert_input_error(self, result):
        """错误路径也必须正常析构；不能只确认非零退出码。"""
        self.assertEqual(result.returncode, 3, result.stderr)
        stderr = result.stderr.decode("utf-8")
        self.assertTrue(stderr.startswith("input error: "), stderr)
        self.assertEqual(stderr.count("input error: "), 1)
        self.assertNotIn("AddressSanitizer", stderr)
        self.assertNotIn("runtime error:", stderr)

    def test_invalid_utf8_reports_input_error_in_command_or_echo(self):
        # 无效 UTF-8 不是未知命令，也不是正常 EOF。先执行有效命令，
        # 再走读取失败路径，验证已经取得的 String 仍能正常释放。
        for prefix in (b"check\n", b"echo\n"):
            with self.subTest(prefix=prefix):
                result = subprocess.run([str(self.program)], input=prefix + b"\xff\n",
                                        capture_output=True, cwd=ROOT, timeout=10)
                self.assert_input_error(result)
                if prefix == b"check\n":
                    self.assertIn(b"summary: 16 passed, 0 failed\n", result.stdout)

    @unittest.skipUnless(os.name == "posix", "目录 stdin 用例依赖 POSIX 文件描述符")
    def test_read_failure_is_not_confused_with_eof(self):
        # POSIX 上目录可以打开，却不能当作文本流读取。相比 /dev/...
        # 特殊文件，这个失败输入不依赖机器上某种额外设备是否存在。
        descriptor = os.open(ROOT, os.O_RDONLY)
        try:
            result = subprocess.run([str(self.program)], stdin=descriptor,
                                    capture_output=True, cwd=ROOT, timeout=10)
        finally:
            os.close(descriptor)
        self.assert_input_error(result)

    def test_a_real_failed_check_reports_failure_and_retains_status(self):
        # 负向对照：仅把数字检查的预期结果改错一个单位。这样证明
        # PASS/FAIL 来自运行时比较，而不是一份固定的成功文案。
        original = SOURCE.read_text(encoding="utf-8")
        expected = "wide + 42 == 4_000_000_042"
        self.assertEqual(original.count(expected), 1)
        changed = original.replace(expected, "wide + 42 == 4_000_000_043")
        source = Path(self.directory.name) / "wrong_expectation.xe"
        source.write_text(changed, encoding="utf-8")
        program = Path(self.directory.name) / "wrong_expectation"
        build_executable(source, program, cc=CC,
                         extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
        result = subprocess.run([str(program)], input="check\nhelp\nquit\n",
                                capture_output=True, text=True, cwd=ROOT, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("FAIL numeric\n", result.stdout)
        self.assertIn("summary: 15 passed, 1 failed\n", result.stdout)
        self.assertEqual(sum(line.startswith("PASS ") for line in result.stdout.splitlines()), 15)
        # 同一个真实失败检查在参数模式也必须返回 1，不能只打印 FAIL。
        argument_result = subprocess.run([str(program), "check"], input="",
                                         capture_output=True, text=True, cwd=ROOT, timeout=10)
        self.assertEqual(argument_result.returncode, 1, argument_result.stderr)
        self.assertEqual(argument_result.stderr, "")
        self.assertTrue(argument_result.stdout.startswith("FAIL numeric\n"))
        self.assertTrue(argument_result.stdout.endswith("summary: 15 passed, 1 failed\n"))
        recovered_result = subprocess.run([str(program)], input="check\nunknown\nquit\n",
                                          capture_output=True, text=True, cwd=ROOT, timeout=10)
        self.assertEqual(recovered_result.returncode, 1, recovered_result.stderr)
        self.assertEqual(recovered_result.stderr, "unknown command: unknown\n")
        self.assertIn("summary: 15 passed, 1 failed\n", recovered_result.stdout)
        if os.name == "posix":
            with self.terminal("check", program=program) as terminal:
                output, _, _ = terminal.finish(status=1)
            self.assertIn(b"\x1b[31mFAIL numeric", output)
            self.assertIn(b"\x1b[31msummary: 15 passed, 1 failed", output)
            self.assertTrue(output.rstrip().endswith(b"\x1b[0m"))


if __name__ == "__main__":
    unittest.main()
