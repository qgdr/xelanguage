"""Step[T] C 后端：标签、具体泛型、融合停止与资源清理的实际运行测试。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from compiler.xe_ast.build import build_executable


CC = shutil.which("cc")


@unittest.skipUnless(CC, "需要系统 C 编译器")
class BackendStepTests(unittest.TestCase):
    def run_source(self, text):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "step.xe"
            output = Path(directory) / "step"
            source.write_text(text, encoding="utf-8")
            build_executable(
                source, output, True, CC,
                ("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"),
            )
            result = subprocess.run(
                [str(output)], capture_output=True, text=True, timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_custom_iterator_yields_item_and_stops(self):
        text = """struct Counter { current: i32, end: i32, }
impl Counter {
    fn next(self: Self@[mut]) -> Step[i32] {
        if self.current >= self.end { Step::Stop } else {
            let value = self.current;
            self.current = self.current + 1;
            Step::Item[value]
        }
    }
}
fn main() {
    let[mut] counter << Counter { .current = 0; .end = 3; };
    for value in counter@[mut] { println("{}", value); }
    println("end {}", counter.current);
}"""
        self.assertEqual(self.run_source(text), "0\n1\n2\nend 3\n")

    def test_from_fn_stop_is_fused(self):
        text = """fn main() {
    let[mut] calls = 0;
    let[mut] iterator << std::iter::from_fn(fn[calls@[mut]]() -> Step[i32] {
        calls = calls + 1;
        if calls == 1 { Step::Item[9] } else { Step::Stop }
    });
    for value in iterator@[mut] { println("{}", value); }
    iterator.next();
    iterator.next();
    println("calls {}", calls);
}"""
        self.assertEqual(self.run_source(text), "9\ncalls 2\n")

    def test_optional_item_is_distinct_from_stop(self):
        text = """fn main() {
    let index = 0;
    let iterator << std::iter::from_fn(fn[index]() -> Step[i32?] {
        if index == 0 {
            index = 1;
            Step::Item[Maybe::None]
        } else if index == 1 {
            index = 2;
            Step::Item[Maybe::Yes[7]]
        } else { Step::Stop }
    });
    for item in iterator {
        let absent = item? 1> _ -> false 2> _ -> true;
        println("{}", absent);
    }
}"""
        self.assertEqual(self.run_source(text), "true\nfalse\n")

    def test_item_and_environment_resources_drop_once(self):
        text = """struct Trace { n: i32, }
impl Drop for Trace {
    fn drop(self: Self@[mut]) { println("drop {}", self.n); }
}
fn main() {
    let guard << Trace { .n = 9; };
    let n = 0;
    let iterator << std::iter::from_fn(fn[guard, n]() -> Step[Trace] {
        if n >= 3 { Step::Stop } else {
            n = n + 1;
            Step::Item[Trace { .n = n; }]
        }
    });
    for value in iterator {
        if value.n == 1 { continue; }
        break;
    }
    println("end");
}"""
        self.assertEqual(self.run_source(text), "drop 1\ndrop 2\ndrop 9\nend\n")

    def test_generic_next_has_concrete_step_layout(self):
        text = """struct[T] Repeat { value: T, remaining: i32, }
impl[T] Repeat[T] where T implements Copy {
    fn next(self: Self@[mut]) -> Step[T] {
        if self.remaining == 0 { Step::Stop } else {
            self.remaining = self.remaining - 1;
            Step::Item[self.value]
        }
    }
}
fn main() {
    let iterator << Repeat[i32] { .value = 7; .remaining = 2; };
    for value in iterator { println("{}", value); }
}"""
        self.assertEqual(self.run_source(text), "7\n7\n")


if __name__ == "__main__":
    unittest.main()
