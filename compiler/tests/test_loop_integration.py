"""Loop targets survive modules, instantiation, and independent function bodies."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.tests.legacy_support import LegacyTestCase
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.modules import load_program
from compiler.xe_ast.semantic import Checker


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


@unittest.skipUnless(shutil.which("gcc"), "requires GCC")
class LoopIntegrationTests(LegacyTestCase):
    def assert_runs(self, files, expected):
        with tempfile.TemporaryDirectory(prefix="xe-loop-integration-") as directory:
            root = Path(directory)
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            entry = root / "src/main.xe"
            source, tree = load_program(entry)
            checker = Checker(source, tree)
            errors = checker.check()
            self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
            binary = root / "program"
            build_executable(entry, binary, cc="gcc", extra_flags=(
                "-Werror=implicit-function-declaration", "-Werror=incompatible-pointer-types",
                "-Werror=return-type"))
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")
        return checker

    def test_module_rewriting_and_generic_instantiation_preserve_label_strings(self):
        checker = self.assert_runs({
            "xe.toml": '[package]\nname = "loop_integration"\n',
            "src/main.xe": '''
                use crate::runner::first as T;
                fn main() { println("{} {}", T(1), T(String::from("owned"))); }
            ''',
            "src/runner.xe": '''
                pub fn[T] first(value: T) -> i32 {
                    let[mut] count = 0;
                    while[label = T] count < 4 {
                        count = count + 1;
                        0 |> _ -> { if count < 3 { continue[T]; }; unit };
                        break[T];
                    }
                    count
                }
            ''',
        }, "3 3\n")
        self.assertEqual(len(checker.generic_instances), 2)
        for node in nodes(checker.tree):
            if node.get("kind") == "While":
                self.assertEqual(node["label"], "T")
            if node.get("kind") in {"Break", "Continue"}:
                self.assertEqual(node["target"], "T")
        for name in checker.generic_instances.values():
            for node in nodes(checker.functions[name].node):
                if node.get("kind") == "While":
                    self.assertEqual(node["label"], "T")
                if node.get("kind") in {"Break", "Continue"}:
                    self.assertEqual(node["target"], "T")

    def test_generic_trait_default_method_keeps_self_named_control_targets(self):
        self.assert_runs({"src/main.xe": '''
            trait Run {
                fn run(self: Self@) -> i32 {
                    let[mut] count = 0;
                    while[label = Self] count < 4 {
                        count = count + 1;
                        if count < 2 { continue[Self]; };
                        break[Self];
                    }
                    count
                }
            }
            struct[T] Holder { value: T, }
            impl[T] Run for Holder[T] {}
            fn main() {
                let first << Holder[i32] { .value = 1; };
                let second << Holder[String] { .value << String::from("owned"); };
                println("{} {}", first.run(), second.run());
            }
        '''}, "2 2\n")

    def test_closure_owns_its_same_named_loop_and_restores_the_callers_target(self):
        self.assert_runs({"src/main.xe": '''
            fn main() {
                let[mut] count = 0;
                while[label = shared] count < 2 {
                    count = count + 1;
                    let seed = 40;
                    let callback << fn[seed]() -> i32 {
                        let[mut] inner = 0;
                        while[label = shared] inner < 3 {
                            inner = inner + 1;
                            if inner < 2 { continue[shared]; };
                            break[shared];
                        }
                        seed + inner
                    };
                    println("{}", callback());
                    if count < 2 { continue[shared]; };
                    break[shared];
                }
                println("outer {}", count);
            }
        '''}, "42\n42\nouter 2\n")

    def test_inline_pipeline_and_branch_handlers_inherit_the_enclosing_target(self):
        self.assert_runs({"src/main.xe": '''
            fn pipeline() -> i32 {
                let[mut] count = 0;
                for[label = outer] n in 0..3 {
                    0 |> _ -> {
                        count = count + 1;
                        if n < 2 { continue[outer]; };
                        unit
                    };
                    break[outer];
                }
                count
            }
            fn branch() -> i32 {
                let[mut] count = 0;
                for[label = outer] n in 0..3 {
                    count = count + 1;
                    let maybe: i32? = Maybe::Yes[n];
                    maybe ? 1> value -> {
                        if value < 2 { continue[outer]; };
                        unit
                    } 2> _ -> { break[outer]; };
                    break[outer];
                }
                count
            }
            fn main() { println("{} {}", pipeline(), branch()); }
        '''}, "3 3\n")


if __name__ == "__main__":
    unittest.main()
