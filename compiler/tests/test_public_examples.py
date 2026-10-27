"""Every shipped example is a complete, warning-free Core program.

Private historical examples have their own explicit legacy fixtures. This
counted list must also run in source packages that do not contain internal/.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compiler.xe_ast.backend_c import lower_to_c
from compiler.xe_ast.build import build_executable
from compiler.xe_ast.language_policy import current_profile

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which(os.environ.get("CC", "gcc"))
OUTPUTS = {
    "core/main.xe": "42\n",
    "core/type-declarations.xe": "42\n",
    "core/generics.xe": "42 owned 7\n",
    "core/access.xe": "40 99 99\n",
    "core/structs.xe": "field 22\nfield 20\n42 44 21\nleft right\nfinish left right\ndone\n",
    "core/enums.xe": "text: hello\n1\n42 0\n",
    "core/else.xe": "42 7 0 1\n0 0\n99\n",
    "core/pipelines.xe": "42 42 42 0 43\n",
    "core/discard.xe": "handling pipe\nhandling text\n42\nbefore return\ndone\n",
    "core/resource-arrays.xe": "pair\npair\ndone\n",
    "dependency-planner/main.xe": (
        "case 0\nrow 0 0\ncomplete 20\n"
        "task 0 0 2\ntask 1 1 5\ntask 2 2 9\ntask 3 3 11\n"
        "task 4 4 16\ntask 5 5 13\ntask 6 6 19\ntask 7 7 20\n"
    ),
}


class PublicExampleTests(unittest.TestCase):
    def test_public_example_inventory_is_explicit_and_uses_canonical_names(self):
        directory = ROOT / "examples"
        files = {path.relative_to(directory).as_posix() for path in directory.rglob("*.xe")}
        self.assertEqual(files, set(OUTPUTS))
        self.assertEqual(len(files), 11)
        self.assertEqual({path.name for path in directory.iterdir() if path.is_dir()},
                         {"core", "dependency-planner"})
        for name in files:
            for component in Path(name).parts:
                self.assertRegex(component, r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.xe)?$")

    def test_every_public_example_checks_and_generates_c_without_warnings(self):
        self.assertEqual(current_profile(), "core")
        for name in OUTPUTS:
            source = ROOT / "examples" / name
            with self.subTest(example=name):
                warnings = []
                result = lower_to_c(source.read_text(encoding="utf-8"), str(source), warnings=warnings)
                self.assertEqual(warnings, [])
                self.assertIn("int main(", result)

    @unittest.skipUnless(CC, "public example execution requires a C compiler")
    def test_every_public_example_executes_with_documented_output(self):
        assert CC is not None
        with tempfile.TemporaryDirectory(prefix="xe-public-examples-") as directory:
            for index, (name, expected) in enumerate(OUTPUTS.items()):
                with self.subTest(example=name):
                    output = Path(directory) / f"example-{index}"
                    build_executable(ROOT / "examples" / name, output, cc=CC)
                    result = subprocess.run([str(output)], input="", text=True,
                                            capture_output=True, timeout=10)
                    self.assertEqual((result.returncode, result.stdout, result.stderr),
                                     (0, expected, ""))


if __name__ == "__main__":
    unittest.main()
