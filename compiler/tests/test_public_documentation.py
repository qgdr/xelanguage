"""The shipped manual uses Core and has a counted executable contract.

Historical private documents are tested separately when present. These checks
cannot pass by skipping the public handbook or finding zero code examples.
"""
import copy
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.release import source_snapshot
from compiler.tests.test_repository import fenced_blocks, local_links, markdown_prose
from compiler.xe_ast.backend_c import AnnotatedChecker, CBackend
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.language_policy import current_profile
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.source import Source

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which(os.environ.get("CC", "gcc"))
CHAPTERS = (
    ("01-first-program.md", "42\n"),
    ("02-basic-types.md", "ready\n42 true hello\n"),
    ("03-functions-and-control-flow.md", "3 3 42\n"),
    ("04-boxes-and-values.md", "20 10\n"),
    ("05-pointers.md", "42 42\n"),
    ("06-copy-and-move.md", "7 7\n"),
    ("07-strings-and-resources.md", "hello\nhello\n"),
    ("08-structs-and-methods.md", "42 44\n"),
    ("09-integer-operations.md", "-20 -2 -1 true\n"),
    ("10-arrays-and-views.md", "20 99 99 99\n"),
    ("11-temporary-arrays.md", "10 20\n"),
    ("12-resource-arrays.md", "received\ndone\n"),
    ("13-type-declarations.md", "42\n"),
    ("14-set-and-union.md", "42\n"),
    ("15-enums.md", "hello\n1 42\n"),
    ("16-branch-pipelines.md", "42 0 7\n"),
    ("17-generic-functions.md", "42 owned 7\n"),
    ("18-initialization-and-cleanup.md", "new\n42 rebuilt\n"),
    ("19-support-boundaries.md", "20\n"),
)


class PublicDocumentationTests(unittest.TestCase):
    def examples(self):
        pages = [("README.md", ("42\n", "hello hello\n", "99 99\n", "42\n")),
                 ("docs/getting-started.md", ("hello, Xe: 42\n",))]
        pages.extend((f"docs/language/{name}", (output,)) for name, output in CHAPTERS)
        samples = []
        for name, outputs in pages:
            path = ROOT / name
            blocks = [(line, code) for tag, line, code in fenced_blocks(path.read_text(encoding="utf-8"))
                      if tag == "xe-core"]
            self.assertEqual(len(blocks), len(outputs), f"{name}: executable sample count changed")
            for (line, code), output in zip(blocks, outputs, strict=True):
                self.assertTrue(code.strip())
                samples.append((name, line, code, output))
        self.assertEqual(len(samples), 24)
        return samples

    def test_all_public_samples_parse_check_and_generate_c_without_warnings(self):
        self.assertEqual(current_profile(), "core")
        for name, line, code, _ in self.examples():
            with self.subTest(page=name, line=line):
                source = Source("\n" * (line - 1) + code, str(ROOT / name))
                tree = parse_source(source.text, source.filename)
                before = copy.deepcopy(tree)
                checker = AnnotatedChecker(source, tree)
                self.assertEqual(checker.check(), [])
                self.assertEqual(checker.warnings, [])
                self.assertIn("int main(", CBackend(checker).generate())
                self.assertEqual(tree, before)

    @unittest.skipUnless(CC, "public manual execution requires a C compiler")
    def test_all_public_samples_execute_with_the_documented_outputs(self):
        assert CC is not None
        with tempfile.TemporaryDirectory(prefix="xe-public-manual-") as directory:
            for index, (name, line, code, expected) in enumerate(self.examples()):
                with self.subTest(page=name, line=line):
                    source, output = Path(directory) / f"sample-{index}.xe", Path(directory) / f"sample-{index}"
                    source.write_text(code, encoding="utf-8")
                    build_executable(source, output, cc=CC)
                    result = subprocess.run([str(output)], input="", text=True, capture_output=True, timeout=10)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ""))

    @unittest.skipUnless(CC, "view sanitizer requires a C compiler")
    def test_public_writable_view_example_keeps_default_sanitizers(self):
        assert CC is not None
        sample = next(item for item in self.examples() if item[0].endswith("10-arrays-and-views.md"))
        with tempfile.TemporaryDirectory(prefix="xe-public-view-sanitizer-") as directory:
            source, output = Path(directory) / "main.xe", Path(directory) / "main"
            source.write_text(sample[2], encoding="utf-8")
            build_executable(source, output, cc=CC,
                             extra_flags=("-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-no-pie"))
            result = subprocess.run([str(output)], text=True, capture_output=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, sample[3], ""))

    def test_public_document_inventory_and_filenames_are_explicit(self):
        files = source_snapshot(ROOT)
        chapters = {f"docs/language/{name}" for name, _ in CHAPTERS}
        expected = chapters | {
            "README.md", "RELEASE.md", "CHANGELOG.md", "examples/README.md",
            "examples/core/README.md", "examples/dependency-planner/README.md",
            "docs/README.md", "docs/language.md", "docs/getting-started.md", "docs/cli.md",
            "docs/support.md", "docs/releases.md", "docs/documentation.md",
        }
        self.assertEqual({name for name in files if name.endswith(".md")}, expected)
        self.assertEqual({p.name for p in (ROOT / "docs/language").glob("*.md")},
                         {name for name, _ in CHAPTERS})
        self.assertEqual([name[:2] for name, _ in CHAPTERS], [f"{index:02d}" for index in range(1, 20)])
        for name in expected:
            with self.subTest(file=name):
                if Path(name).name not in {"README.md", "RELEASE.md", "CHANGELOG.md"}:
                    self.assertRegex(Path(name).name, r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")

    def test_public_links_titles_math_and_example_labels_are_consistent(self):
        files = source_snapshot(ROOT)
        for name, data in files.items():
            if not name.endswith(".md"):
                continue
            text = data.decode("utf-8")
            prose = markdown_prose(text)
            with self.subTest(page=name):
                self.assertNotRegex(prose, r"\\[\[\]()]")
                for _, destination in local_links(text):
                    target = (ROOT / name).parent / destination
                    relative = target.resolve().relative_to(ROOT).as_posix()
                    self.assertFalse(relative.startswith(("internal/", "doc/", "formal/")))
                for label, destination in re.findall(r"\[([^\]\n]+)\]\(([^)\n]+\.md)\)", prose):
                    target = ((ROOT / name).parent / destination).resolve()
                    if target.parent == ROOT / "docs/language":
                        title = target.read_text(encoding="utf-8").splitlines()[0].removeprefix("# ")
                        self.assertEqual(label, title)
                for tag, _, _ in fenced_blocks(text):
                    if tag.startswith("xe"):
                        self.assertIn(tag, {"xe-core", "xe-fragment", "xe-invalid"})


if __name__ == "__main__":
    unittest.main()
