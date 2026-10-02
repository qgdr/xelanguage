"""Repository promises must have real links, executable examples and fixtures.

This is a small standard-library regression gate, not a complete Markdown parser
or a proof that the language/its documentation can never contain an error.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from compiler.release import source_snapshot
from compiler.xe_ast.build import BuildError, build_executable
from compiler.xe_ast.parser import parse_source
from compiler.xe_ast.semantic import Checker
from compiler.xe_ast.source import Diagnostic, Source

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which("cc") or ""
GIT = shutil.which("git") or ""
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_LINK = re.compile(r"(?<!\\)!?\[[^\[\]\n]*\]\((<[^>\n]*>|[^)\n]*)\)")
REFERENCE = re.compile(r"(?m)^ {0,3}\[[^\]\n]+\]:\s*(<[^>\n]*>|\S+)")


def fenced_blocks(text: str) -> list[tuple[str, int, str]]:
    """Return fence info, first source line and code; honor longer/~ fences too."""
    blocks = []
    marker = ""
    info = ""
    start = 0
    lines: list[str] = []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        fence = FENCE.match(line.rstrip("\r\n"))
        if not marker:
            if fence:
                marker, info = fence.groups()
                start, lines = number + 1, []
        elif fence and fence[1][0] == marker[0] and len(fence[1]) >= len(marker) and not fence[2].strip():
            blocks.append((info.strip(), start, "".join(lines)))
            marker = ""
        else:
            lines.append(line)
    if marker:
        raise ValueError(f"未闭合 Markdown 代码围栏，起始行 {start - 1}")
    return blocks


def markdown_prose(text: str) -> str:
    """Mask code with spaces while preserving offsets/lines for useful failures."""
    lines = text.splitlines(keepends=True)
    marker = ""
    masked = []
    for line in lines:
        fence = FENCE.match(line.rstrip("\r\n"))
        if marker:
            masked.append(re.sub(r"[^\r\n]", " ", line))
            if fence and fence[1][0] == marker[0] and len(fence[1]) >= len(marker) and not fence[2].strip():
                marker = ""
        elif fence:
            marker = fence[1]
            masked.append(re.sub(r"[^\r\n]", " ", line))
        else:
            masked.append(line)
    # Inline examples such as fn[T](value) are language code, not Markdown links.
    return re.sub(r"(`+).*?\1", lambda match: re.sub(r"[^\r\n]", " ", match[0]),
                  "".join(masked), flags=re.DOTALL)


def local_links(text: str) -> list[tuple[int, str]]:
    """Inline/image links and reference definitions; URL titles are not paths."""
    prose = markdown_prose(text)
    links = []
    for pattern in (INLINE_LINK, REFERENCE):
        for match in pattern.finditer(prose):
            if pattern is INLINE_LINK and re.search(r"\bfn$", prose[:match.start()]):
                continue  # A bare fn[T](...) example is not a documentation link.
            raw = match[1].strip()
            if not raw:
                continue
            destination = raw[1:raw.index(">")].strip() if raw.startswith("<") else raw.split()[0]
            parsed = urlsplit(destination)
            if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
                continue  # http/mailto, absolute URLs/paths and anchor-only links
            links.append((prose.count("\n", 0, match.start()) + 1, unquote(parsed.path)))
    return links


class MarkdownLinkParserTests(unittest.TestCase):
    def test_fenced_and_inline_language_code_are_not_links(self):
        text = '''fn[T](i32) and `fn[captured](value)` are examples.
`[not a link](missing.md)`
```xe
fn[T] example(value: T) {}
[not a link](missing.md)
```
[real](doc/00.md#overview)
~~~text
[not a link](missing.md)
~~~~
'''
        self.assertEqual(local_links(text), [(7, "doc/00.md")])

    def test_local_image_reference_angle_destination_and_external_links(self):
        text = '''[`file`](doc/00.md "a title")
![image](assets/picture.png)
[space](<folder with spaces/file.md#heading>)
[encoded](folder%20with%20spaces/file.md)
[reference]: ./README.md "a title"
[web](https://example.invalid/page) [mail](mailto:x@example.invalid)
[heading](#local-heading) [absolute](/absolute/path)
\\[escaped](missing.md)
无空格的中文[链接](doc/01.md)
'''
        self.assertEqual(local_links(text), [(1, "doc/00.md"), (2, "assets/picture.png"),
                                            (3, "folder with spaces/file.md"),
                                            (4, "folder with spaces/file.md"), (9, "doc/01.md"),
                                            (5, "./README.md")])

    def test_fence_extraction_retains_source_lines_and_generic_snippets(self):
        self.assertEqual(fenced_blocks("heading\n```xe\nfn[T] id(x:T)->T{x}\n```\n"),
                         [("xe", 3, "fn[T] id(x:T)->T{x}\n")])
        with self.assertRaisesRegex(ValueError, "起始行 1"):
            fenced_blocks("```xe\nfn main(){}\n")


class RepositoryDocumentationTests(unittest.TestCase):
    def test_all_packaged_markdown_relative_links_exist(self):
        files = source_snapshot(ROOT)
        pages = [name for name in files if name.endswith(".md")]
        self.assertTrue(pages, "源码白名单里必须有 Markdown 文档，不能用空 glob 宣称链接检查通过")
        broken = []
        checked = 0
        for name in pages:
            for line, destination in local_links(files[name].decode("utf-8")):
                checked += 1
                target = (ROOT / name).parent / destination
                if not target.exists():
                    broken.append(f"{name}:{line}: 本地链接不存在：{destination}")
        self.assertGreater(checked, 0, "必须实际检查本地文档链接，不能产生零链接假通过")
        self.assertEqual(broken, [], "\n" + "\n".join(broken))

    def readme_examples(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        blocks = [(line, code) for info, line, code in fenced_blocks(text) if info == "xe"]
        self.assertEqual(len(blocks), 8, "README 必须实际检查八个完整 Xe 程序；增删示例须更新执行合同")
        for line, code in blocks:
            self.assertTrue(code.strip(), f"README.md:{line}: Xe 示例不能为空")
        return blocks

    def test_readme_xe_examples_parse_and_semantically_check_as_complete_programs(self):
        for line, code in self.readme_examples():
            with self.subTest(line=line):
                source = Source("\n" * (line - 1) + code, str(ROOT / "README.md"))
                try:
                    tree = parse_source(source.text, source.filename)
                except Diagnostic as error:
                    self.fail(error.render())
                self.assertEqual(tree["kind"], "Module", f"README.md:{line}: 示例须解析为模块")
                self.assertTrue(tree["items"], f"README.md:{line}: Xe 示例不能是空程序")
                self.assertTrue(any(item.get("kind") == "Function" and item.get("name") == "main"
                                    for item in tree["items"]),
                                f"README.md:{line}: 每个 Xe 示例必须包含 main")
                errors = Checker(source, tree).check()
                self.assertEqual(errors, [], "\n".join(error.render() for error in errors))

    @unittest.skipUnless(CC, "README 完整示例执行验收需要系统 C 编译器")
    def test_readme_complete_programs_really_build_and_run(self):
        blocks = self.readme_examples()
        expected = ("42 1\n", "42\n", "42 Xe\n", "42 Xe\n", "20\n40\n42\n10 30\n",
                    "42 0\n", "42 0\n", "42\n2\n")
        self.assertEqual(len(blocks), len(expected), "README 示例改变时须明确更新真实执行合同")
        with tempfile.TemporaryDirectory(prefix="xe-readme-examples-") as directory:
            for index, ((line, code), output) in enumerate(zip(blocks, expected, strict=True)):
                with self.subTest(line=line):
                    source = Path(directory) / f"readme-{index}.xe"
                    program = Path(directory) / f"readme-{index}"
                    source.write_text("\n" * (line - 1) + code, encoding="utf-8")
                    try:
                        build_executable(source, program, cc=CC)
                        completed = subprocess.run([str(program)], capture_output=True, text=True,
                                                   stdin=subprocess.DEVNULL, timeout=10)
                    except Diagnostic as error:
                        self.fail(f"README.md:{line}: 示例构建失败\n{error.render()}")
                    except (BuildError, OSError, subprocess.TimeoutExpired) as error:
                        self.fail(f"README.md:{line}: 示例构建或执行失败\n{error}")
                    self.assertEqual((completed.returncode, completed.stdout, completed.stderr),
                                     (0, output, ""), f"README.md:{line}: 真实程序输出不符合介绍合同")

    def agents_examples(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        blocks = [(line, code) for info, line, code in fenced_blocks(text) if info == "xe"]
        self.assertGreaterEqual(len(blocks), 5, "独立重建契约应保留完整实例与泛型片段")
        return blocks

    def test_agents_xe_examples_parse_and_semantically_check_without_requiring_main(self):
        generic_without_main = False
        for line, code in self.agents_examples():
            with self.subTest(line=line):
                source = Source("\n" * (line - 1) + code, str(ROOT / "AGENTS.md"))
                try:
                    tree = parse_source(source.text, source.filename)
                except Diagnostic as error:
                    self.fail(error.render())
                self.assertEqual(tree["kind"], "Module")
                self.assertTrue(tree["items"], f"AGENTS.md:{line}: Xe 示例不能是空程序")
                errors = Checker(source, tree).check()
                self.assertEqual(errors, [], "\n".join(error.render() for error in errors))
                has_main = any(item.get("kind") == "Function" and item.get("name") == "main"
                               for item in tree["items"])
                if not has_main and any(item.get("generics") for item in tree["items"]):
                    generic_without_main = True
        # This follows Xe's declared deferred-instantiation policy; checking an
        # unused generic declaration does not prove every possible instantiation.
        self.assertTrue(generic_without_main, "须实际检查不含 main 的泛型片段")

    @unittest.skipUnless(CC, "AGENTS 完整示例执行验收需要系统 C 编译器")
    def test_agents_complete_programs_really_build_and_run(self):
        complete = []
        for line, code in self.agents_examples():
            tree = parse_source(code, str(ROOT / "AGENTS.md"))
            if any(item.get("kind") == "Function" and item.get("name") == "main" for item in tree["items"]):
                complete.append((line, code))
        expected = ("42\n", "3 Xe\n", "0\n", "42\n")
        self.assertEqual(len(complete), len(expected), "完整 AGENTS 示例改变时须明确更新真实执行合同")
        with tempfile.TemporaryDirectory(prefix="xe-agents-examples-") as directory:
            for index, ((line, code), output) in enumerate(zip(complete, expected, strict=True)):
                with self.subTest(line=line):
                    source = Path(directory) / f"agents-{index}.xe"
                    program = Path(directory) / f"agents-{index}"
                    source.write_text("\n" * (line - 1) + code, encoding="utf-8")
                    build_executable(source, program, cc=CC)
                    completed = subprocess.run([str(program)], capture_output=True, text=True,
                                               stdin=subprocess.DEVNULL, timeout=10)
                    self.assertEqual((completed.returncode, completed.stdout, completed.stderr),
                                     (0, output, ""), f"AGENTS.md:{line}: 真实程序输出不符合重建合同")

    def test_current_language_fixture_globs_are_not_empty(self):
        for name in ("language", "backend", "fails", "syntax_fails", "warnings"):
            with self.subTest(directory=name):
                self.assertTrue(list((ROOT / "tests" / name).glob("*.xe")),
                                f"tests/{name} 必须存在 Xe 样例，不能因目录更名而零样例假通过")


@unittest.skipUnless(GIT and (ROOT / ".git").exists(),
                     "Git 忽略策略验收限 Git 工作树；归档源码包及编译器运行不需要 Git")
class RepositoryIgnorePolicyTests(unittest.TestCase):
    def test_generated_files_and_credentials_ignored_but_real_sources_visible(self):
        ignored = (
            "target/debug/program", ".venv/bin/python", "compiler/__pycache__/module.pyc",
            ".ruff_cache/data", "tests/example.ast.json", "tests/example.ll", "tests/example.bc",
            "tests/example.o", "tests/example.obj", "tests/example.out", "tests/example.exe",
            "examples/libnative.a", "examples/libnative.so", "examples/libnative.dylib",
            ".env", ".env.local", "credentials/private.pem", "credentials/private.key",
            ".aws/credentials", ".ssh/id_ed25519",
        )
        visible = (
            "tests/language/example.xe", "examples/ffi/functions.c", "compiler/runtime/runtime.h",
            "doc/example.md", "examples/toolchain/xe.toml", "compiler/main.py", ".env.example",
        )
        environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                            "GIT_OPTIONAL_LOCKS": "0"})
        with tempfile.TemporaryDirectory(prefix="xe-ignore-policy-") as directory:
            root = Path(directory)
            (root / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
            subprocess.run([GIT, "init", "--initial-branch=main", str(root)], env=environment,
                           capture_output=True, check=True, timeout=10)
            result = subprocess.run([GIT, "-C", str(root), "-c", f"core.excludesFile={os.devnull}",
                                     "check-ignore", "--no-index", "--stdin", "-z"],
                                    input="\0".join((*ignored, *visible)).encode() + b"\0",
                                    env=environment, capture_output=True, timeout=10)
        self.assertIn(result.returncode, (0, 1), result.stderr.decode(errors="replace"))
        actual = {name.decode() for name in result.stdout.split(b"\0") if name}
        self.assertEqual(actual, set(ignored), "忽略规则须保护生成物/凭据，同时保留合法源码可提交")


if __name__ == "__main__":
    unittest.main()
