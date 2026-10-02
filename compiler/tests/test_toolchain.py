"""统一工具链的工程合同：真实编译、缓存、产物保护、CLI、格式与文档。

输入/生成物在独立临时目录。不会 clean 用户 target 或整理项目源文件。
自举项目只保留原样，不是本测试实现的后端。
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compiler.artifacts import ArtifactStore
from compiler.documentation import collect, markdown
from compiler.driver import ROOT, build
from compiler.formatting import format_source, semantic_shape, source_files
from compiler.project import project_at, select_source
from compiler.toolchain import main
from compiler.xe_ast.build import BuildError
from compiler.xe_ast.parser import parse_source

CC = shutil.which("cc") or ""


class ToolchainFixtures(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def file(self, name, text):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        return file

    def package(self, name="hello"):
        self.file("xe.toml", f'[package]\nname="{name}"\nversion="0.1.0"\nedition="2027"\n')
        return self.file("src/main.xe", 'fn main(){println("hello");}')

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main([str(argument) for argument in arguments])
        return status, out.getvalue(), err.getvalue()


class ProjectTests(ToolchainFixtures):
    def test_file_project_and_explicit_manifest(self):
        source = self.package()
        project, entry = select_source(source)
        self.assertEqual((project.root, entry, project.name), (self.root, source, "hello"))
        self.assertEqual(select_source(manifest=self.root)[1], source)
        standalone = self.file("other/app.xe", "fn main(){}")
        # nearest manifest still owns this path; loader will reject outside src.
        self.assertEqual(project_at(standalone).root, self.root)

    def test_library_and_named_binary_discovery(self):
        self.package()
        library = self.file("src/lib.xe", "pub fn add(a:i32,b:i32)->i32{a+b}")
        named = self.file("src/bin/helper.xe", "fn main(){}")
        self.assertEqual(select_source(manifest=self.root, library=True)[1], library)
        self.assertEqual(select_source(manifest=self.root, binary="helper")[1], named)
        for name in ("../outside", "/absolute", "bad\\path"):
            with self.subTest(name=name), self.assertRaises(BuildError):
                select_source(manifest=self.root, binary=name)

    def test_configuration_errors_are_friendly(self):
        source = self.package()
        for text in ('[package', 'package=42', '[package]\nname=42', '[package]\nname=""'):
            with self.subTest(text=text):
                self.file("xe.toml", text)
                status, stdout, stderr = self.cli("check", source, "--message-format=json")
                self.assertEqual((status, stdout), (2, ""))
                self.assertIn("diagnostics", json.loads(stderr))
                self.assertNotIn("Traceback", stderr)

    def test_output_name_does_not_interpret_package_name_as_path(self):
        source = self.package("../../escape")
        project, _ = select_source(source)
        self.assertNotIn("/", project.artifact_name(source))
        self.assertNotEqual(project.artifact_name(source), "..")

    def test_missing_input_and_manifest_mismatch(self):
        self.assertEqual(self.cli("check", self.root / "absent.xe")[0], 2)
        self.package()
        different = self.root / "other"
        self.file("other/xe.toml", '[package]\nname="other"')
        entry = self.file("other/src/main.xe", "fn main(){}")
        self.assertEqual(self.cli("check", entry, "--manifest-path", self.root)[0], 2)
        self.assertEqual(self.cli("check", "--manifest-path", different)[0], 0)


class FrontendToolTests(ToolchainFixtures):
    def test_ast_stdout_is_json_and_check_loads_modules(self):
        source = self.package()
        self.file("src/part.xe", "pub fn number()->i32{42}")
        source.write_text('use crate::part::number; fn main(){println("{}",number());}')
        status, output, error = self.cli("ast", source, "-o", "-")
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(json.loads(output)["schema_version"], 1)
        self.assertEqual(self.cli("check", source)[0], 0)
        self.file("src/part.xe", "pub fn number()->i32{false}")
        status, _, error = self.cli("check", source, "--message-format=json")
        self.assertEqual(status, 1)
        self.assertIn("part.xe", json.loads(error)["diagnostics"][0]["file"])

    def test_json_file_status_does_not_mix_in_human_text(self):
        source = self.file("main.xe", "fn main(){}")
        for action in ("ast", "emit-c"):
            with self.subTest(action=action):
                status, output, error = self.cli(action, source, "--message-format=json")
                self.assertEqual((status, error), (0, ""))
                self.assertEqual(json.loads(output)["action"], action)

    def test_documentation_and_c_output_cannot_overwrite_dependency_manifest(self):
        source = self.package()
        self.file("xe.toml", '[package]\nname="hello"\n[dependencies]\nhelper={path="helper"}')
        manifest = self.file("helper/xe.toml", '[package]\nname="helper"')
        self.file("helper/src/lib.xe", 'pub fn value()->i32{7}')
        source.write_text('use helper::value;fn main(){println("{}",value());}')
        original = manifest.read_bytes()
        for action in ("emit-c", "doc", "build"):
            with self.subTest(action=action):
                self.assertEqual(self.cli(action, source, "-o", manifest)[0], 2)
                self.assertEqual(manifest.read_bytes(), original)

    def test_errors_preserve_previous_ast_and_source(self):
        source = self.file("broken.xe", "fn main(){let x=;}")
        output = self.file("result.json", "previous")
        self.assertEqual(self.cli("ast", source, "-o", output)[0], 1)
        self.assertEqual(output.read_text(), "previous")
        self.assertEqual(self.cli("ast", source, "-o", source)[0], 2)
        self.assertEqual(source.read_text(), "fn main(){let x=;}")

    def test_ast_and_emit_c_are_registered_for_safe_clean(self):
        source = self.file("main.xe", "fn main(){}")
        self.assertEqual(self.cli("ast", source)[0], 0)
        self.assertEqual(self.cli("emit-c", source)[0], 0)
        output = self.root / "target/ast/main.xe.ast.json"
        preview = self.cli("clean", self.root, "--dry-run", "--message-format=json")
        self.assertIn(str(output), json.loads(preview[1])["removed"])
        self.assertTrue(output.exists())
        self.assertEqual(self.cli("clean", self.root)[0], 0)
        self.assertFalse(output.exists())
        self.assertTrue(source.exists())

    def test_c_stdout_and_original_entrypoint_still_work(self):
        source = self.file("main.xe", "fn main(){}")
        status, output, error = self.cli("emit-c", source, "-o", "-")
        self.assertEqual((status, error), (0, ""))
        self.assertIn("Generated by Xe", output)
        from compiler.xe_ast.cli import main as old_main
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(old_main([str(source), "-o", "-"]), 0)

    def test_lint_var_only_warns_and_leaves_source_unchanged(self):
        text = "fn main(){var value=1;value=2;}"
        source = self.file("main.xe", text)
        status, _, error = self.cli("lint", source)
        self.assertEqual(status, 0)
        self.assertIn("XE-LINT-0001", error)
        self.assertEqual(source.read_text(), text)

    def test_usage_separator_and_invalid_timeout(self):
        for argv in (("check", "--", "foo"), ("run", "--timeout=nan"),
                     ("run", "--timeout=-1"), ("build", "--build-timeout=inf")):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(list(argv))
            self.assertEqual(caught.exception.code, 2)


@unittest.skipUnless(CC, "需要 C 编译器")
class BuildToolTests(ToolchainFixtures):
    def test_default_project_from_child_directory_and_real_named_binary(self):
        self.package()
        self.file("src/lib.xe", "pub use crate::math::square;")
        self.file("src/math.xe", "pub fn square(n:i32)->i32{n*n}")
        self.file("src/bin/smoke.xe", 'use crate::math::square;fn main(){println("{}",square(7));}')
        nested = self.root / "src/nested"; nested.mkdir()
        completed = subprocess.run([str(ROOT / "xe"), "run", "--bin", "smoke"],
                                   cwd=nested, capture_output=True, text=True, timeout=10)
        self.assertEqual((completed.returncode, completed.stdout), (0, "49\n"), completed.stderr)
        completed = subprocess.run([str(ROOT / "xe"), "check"], cwd=nested,
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_build_and_cache_and_release(self):
        source = self.package()
        args = ("build", "--manifest-path", self.root, "--message-format=json")
        first = self.cli(*args)
        self.assertEqual(first[0], 0, first[2])
        first_data = json.loads(first[1])
        self.assertFalse(first_data["cached"])
        self.assertEqual(Path(first_data["program"]).parent, self.root / "target/debug")
        self.assertTrue(json.loads(self.cli(*args)[1])["cached"])
        release = self.cli(*args, "--release")
        self.assertEqual(release[0], 0, release[2])
        self.assertIn("/release/", json.loads(release[1])["program"])
        self.assertFalse(json.loads(self.cli(*args, "--rebuild")[1])["cached"])
        source.write_text('fn main(){println("changed");}')
        self.assertFalse(json.loads(self.cli(*args)[1])["cached"])

    def test_dependency_and_manifest_changes_invalidate_cache(self):
        source = self.package()
        source.write_text('use crate::other::number;fn main(){println("{}",number());}')
        other = self.file("src/other.xe", "pub fn number()->i32{1}")
        args = ("build", source, "--message-format=json")
        self.assertEqual(self.cli(*args)[0], 0)
        self.assertTrue(json.loads(self.cli(*args)[1])["cached"])
        other.write_text("pub fn number()->i32{2}")
        self.assertFalse(json.loads(self.cli(*args)[1])["cached"])
        self.file("xe.toml", '[package]\nname="hello"\nversion="0.2"')
        self.assertFalse(json.loads(self.cli(*args)[1])["cached"])

    def test_executable_and_receipt_tampering_force_rebuild(self):
        source = self.package()
        args = ("build", source, "--message-format=json")
        data = json.loads(self.cli(*args)[1])
        Path(data["program"]).write_text("changed binary")
        self.assertFalse(json.loads(self.cli(*args)[1])["cached"])
        Path(data["receipt"]).write_text("broken json")
        self.assertFalse(json.loads(self.cli(*args)[1])["cached"])
        data = json.loads(self.cli(*args)[1])
        receipt = json.loads(Path(data["receipt"]).read_text()); receipt["files"] = []
        Path(data["receipt"]).write_text(json.dumps(receipt))
        self.assertFalse(json.loads(self.cli(*args)[1])["cached"])

    def test_intervening_publisher_does_not_poison_cache_key(self):
        source = self.package()
        from compiler.xe_ast.build import compile_generated
        def interfered(generated, c_path, output, **kwargs):
            compile_generated(generated, c_path, output, **kwargs)
            output.write_bytes(b"another publisher replaced this binary")
            output.chmod(0o755)
        with patch("compiler.driver.compile_generated", side_effect=interfered):
            self.assertEqual(self.cli("build", source)[0], 0)
        data = json.loads(self.cli("build", source, "--message-format=json")[1])
        self.assertFalse(data["cached"])
        result = subprocess.run([data["program"]], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout), (0, "hello\n"))

    def test_publication_replaces_output_link_and_registers_new_file_hash(self):
        from compiler.artifacts import digest
        from compiler.xe_ast.build import compile_generated
        old = self.file("old-program", "keep original target")
        output = self.root / "program"
        output.symlink_to(old)
        hashes = {}
        compile_generated("int main(void){return 0;}\n", self.root / "program.c", output,
                          cc=CC, published_hashes=hashes)
        self.assertFalse(output.is_symlink())
        self.assertEqual(old.read_text(), "keep original target")
        self.assertEqual(hashes, {str(output): digest(output)})

    def test_changed_generated_c_is_restored_not_executed(self):
        source = self.package()
        args = ("build", source, "--message-format=json")
        data = json.loads(self.cli(*args)[1]); output = Path(data["c"])
        original = output.read_bytes(); output.write_text("not a C program")
        reused = self.cli(*args)
        self.assertEqual(reused[0], 0, reused[2])
        self.assertTrue(json.loads(reused[1])["cached"])
        self.assertEqual(output.read_bytes(), original)

    def test_failure_preserves_old_executable(self):
        source = self.package()
        data = json.loads(self.cli("build", source, "--message-format=json")[1])
        program = Path(data["program"]); old = program.read_bytes()
        source.write_text("fn main(){missing();}")
        self.assertEqual(self.cli("build", source)[0], 1)
        self.assertEqual(program.read_bytes(), old)
        source.write_text("fn main(){}")
        self.assertEqual(self.cli("build", source, "--cc", self.root / "missingcc")[0], 2)
        self.assertEqual(program.read_bytes(), old)
        with patch("compiler.driver.compile_generated", side_effect=BuildError("C failed")):
            self.assertEqual(self.cli("build", source, "--rebuild")[0], 2)
        self.assertEqual(program.read_bytes(), old)

    def test_cache_never_skips_semantic_check_or_warning(self):
        source = self.file("warning.xe", "fn local()->i32@{let n=1;n@}fn main(){}")
        args = ("build", source, "--message-format=json")
        first = self.cli(*args)
        self.assertEqual(first[0], 0, first[2])
        self.assertIn("warning", first[2])
        second = self.cli(*args)
        self.assertTrue(json.loads(second[1])["cached"])
        self.assertEqual(json.loads(first[2]), json.loads(second[2]))

    def test_options_and_thread_linking_work(self):
        source = self.file("main.xe", "fn main(){}")
        args = ("build", source, "--cflag=-Wall", "--message-format=json")
        self.assertEqual(self.cli(*args)[0], 0)
        self.assertFalse(json.loads(self.cli(*args)[1])["cached"])
        thread = ROOT / "examples/threads/main.xe"
        project = project_at(self.root)
        output = self.root / "threads"
        result = build(project, thread, output)
        completed = subprocess.run([str(result.output)], capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_failure_c_is_registered_but_old_binary_is_not_replaced(self):
        source = self.package()
        data = json.loads(self.cli("build", source, "--message-format=json")[1])
        old = Path(data["program"]).read_bytes()
        with patch("compiler.driver.compile_generated", side_effect=BuildError("C failed")):
            self.assertEqual(self.cli("build", source, "--rebuild")[0], 2)
        self.assertEqual(Path(data["program"]).read_bytes(), old)
        self.assertTrue(ArtifactStore(self.root).read("failed-c", Path(data["c"])))

    def test_cc_flags_and_executable_permissions_invalidate_cache(self):
        source = self.package()
        selected_cc = shutil.which(os.environ.get("CC", "cc")) or CC
        args = ("build", source, "--message-format=json")
        data = json.loads(self.cli(*args)[1]); program = Path(data["program"])
        program.chmod(0o600)
        self.assertFalse(json.loads(self.cli(*args)[1])["cached"])
        self.assertTrue(os.access(program, os.X_OK))
        # 环境指定的默认工具和它在 PATH 中的绝对路径是同一个工具。
        # 不假定默认一定叫 cc：发布验收显式选择 CC=gcc。
        self.assertTrue(json.loads(self.cli(*args, "--cc", selected_cc)[1])["cached"])
        if os.name == "posix":
            import shlex
            wrapper = self.file("different-cc", '#!/bin/sh\nexec ' + shlex.quote(selected_cc) + ' "$@"\n')
            wrapper.chmod(0o755)
            self.assertFalse(json.loads(self.cli(*args, "--cc", wrapper)[1])["cached"])

    @unittest.skipUnless(__import__("sys").platform.startswith("linux"), "ASan/no-pie 验收针对 Linux")
    def test_sanitize_build_really_runs_and_cleans_resources(self):
        source = self.file("sanitized.xe", '''fn main(){
            let[mut] values<<Vec[String]::new();values.push(String::from("ok"));
            let text<<values.pop()?[panic];println("{}",text@);
        }''')
        status, output, error = self.cli("test", source, "--sanitize", "--message-format=json")
        self.assertEqual((status, error), (0, ""))
        case = json.loads(output)["tests"][0]
        self.assertEqual((case["stdout"], case["stderr"]), ("ok\n", ""))

    @unittest.skipUnless(os.name == "posix", "需要硬链接")
    def test_hardlink_to_source_is_protected(self):
        source = self.package()
        alias = self.root / "alias"; alias.hardlink_to(source)
        for action in ("build", "ast", "emit-c"):
            with self.subTest(action=action):
                self.assertEqual(self.cli(action, source, "-o", alias)[0], 2)
                self.assertEqual(alias.read_bytes(), source.read_bytes())

    def test_output_cannot_cover_source_dependency_manifest_or_cache(self):
        source = self.package()
        other = self.file("src/other.xe", "pub fn number()->i32{1}")
        source.write_text("use crate::other::number;fn main(){}")
        for output in (source, other, self.root / "xe.toml", self.root / "target/.xe-tools/custom"):
            with self.subTest(output=output):
                old = output.read_bytes() if output.exists() else None
                self.assertEqual(self.cli("build", source, "-o", output)[0], 2)
                if old is not None:
                    self.assertEqual(output.read_bytes(), old)

    def test_doctor_and_invalid_cc(self):
        status, output, error = self.cli("doctor", "--message-format=json")
        self.assertEqual((status, error), (0, ""))
        self.assertIn("cc", json.loads(output))
        self.assertEqual(self.cli("doctor", "--cc", self.root / "missing")[0], 2)

    def test_test_files_success_nonzero_and_timeout(self):
        source = self.file("pass.xe", "fn main(){}")
        failure = self.file("fail.xe", "fn main()->i32{7}")
        forever = self.file("timeout.xe", "fn main(){while true{}}")
        status, output, error = self.cli("test", source, failure, forever, "--timeout=0.2", "--message-format=json")
        self.assertEqual((status, error), (1, ""))
        tests = json.loads(output)["tests"]
        self.assertEqual([item["passed"] for item in tests], [True, False, False])
        self.assertEqual(tests[1]["exit_code"], 7)
        self.assertEqual(tests[2]["timeout"], 0.2)
        self.assertEqual(self.cli("test")[0], 2)

    def test_test_compilation_error_does_not_hide_other_cases(self):
        bad = self.file("bad.xe", "fn main(){missing();}")
        good = self.file("good.xe", 'fn main(){eprintln("Sanitizer is just text here");}')
        status, output, error = self.cli("test", bad, good, "--message-format=json")
        self.assertEqual((status, error), (1, ""))
        data = json.loads(output)["tests"]
        self.assertEqual([item["passed"] for item in data], [False, True])
        self.assertEqual(data[0]["phase"], "compile")

    def test_run_forwards_exact_arguments_without_shell_and_keeps_cwd(self):
        source = self.file("args.xe", (ROOT / "examples/args/main.xe").read_text())
        marker = self.root / "must-not-be-created"
        arguments = ["two words", "你好", "", "--help", "--", f"$(touch {marker})"]
        completed = subprocess.run([str(ROOT / "xe"), "run", str(source), "--", *arguments],
                                   cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for argument in arguments:
            self.assertIn(argument, completed.stdout)
        self.assertFalse(marker.exists())
        self.assertNotIn("构建完成", completed.stdout)

    def test_root_environment_launcher_and_module_entry(self):
        completed = subprocess.run([str(ROOT / "xe"), "doctor", "--message-format=json"],
                                   cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        data = json.loads(completed.stdout)
        if (ROOT / ".venv").exists():
            self.assertTrue(Path(data["python"]).is_relative_to(ROOT / ".venv"))
        import sys
        completed = subprocess.run([sys.executable, "-m", "compiler", "--version"], cwd=ROOT,
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("stage0", completed.stdout)


class FormattingTests(ToolchainFixtures):
    def test_all_language_examples_roundtrip_and_are_idempotent(self):
        paths = sorted((ROOT / "tests/language").glob("*.xe"))
        self.assertTrue(paths, "现行语言样例目录不能为空；检查迁移后的路径")
        for path in paths:
            with self.subTest(file=path.name):
                original = path.read_bytes().decode("utf-8")
                formatted = format_source(original, str(path))
                self.assertEqual(format_source(formatted, str(path)), formatted)

    def test_format_roundtrip_and_idempotence(self):
        samples = [
            "fn main() {\n\tlet[mut] x = 1;  \n     if x == 1 {\nx = 2;\n }\n}\n",
            'fn main() {\nprintln("中 😀 { }\\0");\n// do not trim this comment   \n/* keep\n    this\n block */\n}\n',
            'fn main() {\r\nlet x = tuple[\r\n1,\r\n2,\r\n];\r\n}\r\n',
            (ROOT / "tests/language/enum.xe").read_text(),
            (ROOT / "tests/language/branch_pipeline.xe").read_text(),
            (ROOT / "tests/language/iterator_step.xe").read_text(),
        ]
        for number, text in enumerate(samples):
            with self.subTest(number=number):
                result = format_source(text)
                self.assertEqual(semantic_shape(parse_source(text)), semantic_shape(parse_source(result)))
                self.assertEqual(format_source(result), result)
        self.assertIn("    let[mut]", format_source(samples[0]))
        self.assertIn("    this\n block", format_source(samples[1]))
        self.assertEqual(format_source(samples[2]).count("\r\n"), samples[2].count("\r\n"))

    def test_check_does_not_write_and_write_preserves_permissions(self):
        source = self.file("main.xe", "fn main(){\nlet x=1;\n}\n")
        source.chmod(0o640)
        original = source.read_bytes()
        self.assertEqual(self.cli("fmt", source, "--check")[0], 1)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.cli("fmt", source)[0], 0)
        self.assertEqual(source.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.cli("fmt", source, "--check")[0], 0)

    def test_invalid_second_file_prevents_all_writes(self):
        good = self.file("a.xe", "fn main(){\nlet x=1;\n}")
        bad = self.file("b.xe", "fn main(){")
        original = good.read_bytes()
        self.assertEqual(self.cli("fmt", good, bad)[0], 1)
        self.assertEqual(good.read_bytes(), original)

    def test_default_project_scan_excludes_target_and_dependencies(self):
        self.package()
        extra = self.file("src/part.xe", "pub fn value()->i32{1}")
        self.file("target/generated.xe", "broken syntax")
        self.assertEqual(source_files([self.root]), sorted([extra, self.root / "src/main.xe"]))
        self.assertEqual(self.cli("fmt", "--manifest-path", self.root, "--check")[0], 0)

    def test_does_not_follow_source_links(self):
        real = self.file("real.xe", "fn main(){}")
        link = self.root / "link.xe"; link.symlink_to(real)
        self.assertEqual(self.cli("fmt", link)[0], 2)
        self.assertEqual(real.read_text(), "fn main(){}")


class DocumentationTests(ToolchainFixtures):
    def test_public_docs_fields_methods_variants_comments_and_private_filter(self):
        self.package()
        self.file("src/lib.xe", '''//! Example module.
/// Coordinates.
pub struct Point { /// X coordinate.
 pub x: i32, hidden: i32, }
impl Copy for Point;
impl Point { /// Public measure.
 pub fn measure(self:Self@)->i32 {self.x} }
struct Secret {x:i32,}
impl Secret {pub fn hidden(self:Self@)->i32{self.x}}
/// Unit alternatives.
pub enum Mode{Yes,No,}
impl Copy for Mode;
/// Transparent alias.
pub type Count=i32;
fn internal(){}''')
        data, _, _ = collect(self.root / "src/lib.xe")
        names = [item["name"] for module in data["modules"] for item in module["symbols"]]
        self.assertIn("Point", names); self.assertIn("Point.x", names)
        self.assertNotIn("Point.hidden", names); self.assertNotIn("Secret", names)
        self.assertNotIn("internal", names)
        self.assertTrue(any("measure" in name for name in names))
        self.assertFalse(any("hidden" in name for name in names))
        self.assertIn("Mode::Yes", names)
        rendered = markdown(data)
        self.assertIn("Coordinates.", rendered); self.assertIn("Example module.", rendered)
        self.assertIn("X coordinate.", rendered); self.assertIn("Public measure.", rendered)
        status, output, error = self.cli("doc", "--manifest-path", self.root, "--format=json", "-o", "-")
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(json.loads(output)["schema_version"], 1)
        all_data, _, _ = collect(self.root / "src/lib.xe", private=True)
        self.assertIn("internal", [item["name"] for module in all_data["modules"] for item in module["symbols"]])

    def test_markdown_fence_and_output_protection(self):
        source = self.file("main.xe", 'pub const TEXT:str="```";fn main(){}')
        data, _, _ = collect(source)
        self.assertIn("````xe", markdown(data))
        original = source.read_bytes()
        self.assertEqual(self.cli("doc", source, "-o", source)[0], 2)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.cli("doc", source)[0], 0)
        self.assertTrue((self.root / "target/doc/index.md").exists())


class ArtifactTests(ToolchainFixtures):
    def test_missing_clean_path_never_cleans_parent_target(self):
        store = ArtifactStore(self.root)
        output = self.file("target/protected", "generated")
        store.record("test", output, [output])
        self.assertEqual(self.cli("clean", self.root / "missing")[0], 2)
        self.assertTrue(output.exists())

    def test_expected_producer_hash_preserves_intervening_edits(self):
        import hashlib
        output = self.file("target/output", "user edit")
        store = ArtifactStore(self.root)
        store.record("test", output, [output], expected_hashes={str(output): hashlib.sha256(b"generated").hexdigest()})
        self.assertIn(str(output), store.clean()["preserved"])
        self.assertTrue(output.exists())
    def test_clean_leaves_unknown_modified_external_and_bootstrap_files(self):
        store = ArtifactStore(self.root)
        owned = self.file("target/debug/owned", "generated")
        changed = self.file("target/debug/changed", "generated")
        unknown = self.file("target/user.txt", "keep")
        bootstrap = self.file("target/bootstrap/stage3.c", "keep bootstrap")
        external = self.file("outside.txt", "external")
        store.record("test", owned, [owned, changed, external, bootstrap])
        changed.write_text("user changed it")
        preview = store.clean(dry_run=True)
        self.assertIn(str(owned), preview["removed"]); self.assertTrue(owned.exists())
        result = store.clean()
        self.assertFalse(owned.exists())
        for file in (changed, unknown, bootstrap, external):
            self.assertTrue(file.exists(), result)
        self.assertIn(str(changed), result["preserved"])
        self.assertIn(str(bootstrap), result["preserved"])

    def test_clean_rejects_target_symlink_and_preserves_corrupt_receipt(self):
        real = self.root / "real"; real.mkdir()
        (self.root / "target").symlink_to(real, target_is_directory=True)
        with self.assertRaises(BuildError):
            ArtifactStore(self.root)
        (self.root / "target").unlink()
        invalid = self.file("target/.xe-tools/bad.json", "not json")
        result = ArtifactStore(self.root).clean()
        self.assertIn(str(invalid), result["preserved"]); self.assertTrue(invalid.exists())

    def test_compiler_tests_are_explicit_and_do_not_recursively_run_here(self):
        with patch("compiler.toolchain.subprocess.call", return_value=0) as run:
            self.assertEqual(self.cli("test", "--compiler", "--pattern=test_cli.py")[0], 0)
        self.assertIn("test_cli.py", run.call_args.args[0])
        self.assertEqual(self.cli("test", "--compiler", self.root / "some.xe")[0], 2)
        self.assertEqual(self.cli("test", "--compiler", "--sanitize")[0], 2)

    def test_compiler_regression_json_is_a_single_object(self):
        completed = subprocess.CompletedProcess([], 0)
        with patch("compiler.toolchain.subprocess.run", return_value=completed):
            status, output, error = self.cli("test", "--compiler", "--message-format=json")
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(json.loads(output)["compiler_tests"]["exit_code"], 0)
