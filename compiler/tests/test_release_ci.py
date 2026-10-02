"""CI 安全/平台契约的静态回归，不冒充 GitHub 远程执行或完整 YAML 校验。

使用标准库固定少量关键配置，避免为编译器运行引入 YAML 解析依赖。
工作流的真正执行须在用户决定 push 后由 GitHub-hosted runner 验收。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/release-check.yml"


class ReleaseCIPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        # 不把注释中的说明当作执行配置。
        cls.configuration = "\n".join(line.split(" #", 1)[0] for line in cls.text.splitlines()
                                       if not line.lstrip().startswith("#"))

    def test_job_is_bounded_and_uses_supported_platform(self):
        for entry in ('runs-on: ubuntu-24.04', 'timeout-minutes: 15', 'CC: gcc',
                      'python-version: "3.13"', 'architecture: x64',
                      'UV_PYTHON_DOWNLOADS: never'):
            with self.subTest(entry=entry):
                self.assertIn(entry, self.configuration)
        self.assertIn('test "$(uname -m)" = "x86_64"', self.configuration)

    def test_only_read_permission_and_no_publishing_steps(self):
        self.assertIn("permissions:\n  contents: read", self.configuration)
        self.assertNotRegex(self.configuration, r"(?m)^\s*[\w-]+:\s*write\s*$")
        self.assertIn("persist-credentials: false", self.configuration)
        for forbidden in ("pull_request_target:", "workflow_run:", "release:",
                          "upload-artifact@", "gh release", "git push", "git tag",
                          "pypa/gh-action-pypi-publish", "id-token:"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.configuration)

    def test_actions_are_official_and_pinned_to_full_commits(self):
        actions = re.findall(r"(?m)^\s*- uses:\s*(\S+)\s*$", self.configuration)
        self.assertEqual(len(actions), 3)
        expected = {"actions/checkout", "actions/setup-python", "astral-sh/setup-uv"}
        actual = set()
        for action in actions:
            name, separator, revision = action.partition("@")
            self.assertEqual(separator, "@")
            self.assertRegex(revision, r"^[0-9a-f]{40}$")
            actual.add(name)
        self.assertEqual(actual, expected)
        self.assertRegex(self.configuration, r'(?m)^\s+version:\s*"\d+\.\d+\.\d+"\s*$')
        self.assertIn("enable-cache: false", self.configuration)

    def test_locked_install_and_one_shared_release_gate(self):
        self.assertIn("run: uv sync --locked --dev --python python", self.configuration)
        self.assertEqual(self.configuration.count("run: make release-check CC=gcc RELEASE_FLAGS=--require-clean"), 1)
        self.assertLess(self.configuration.index("run: uv sync --locked"),
                        self.configuration.index("run: make release-check"))

    def test_sanitizers_are_not_suppressed_or_failures_ignored(self):
        self.assertNotRegex(self.configuration, r"detect_leaks\s*[=:]\s*0")
        self.assertNotRegex(self.configuration, r"(?m)^\s*(ASAN_OPTIONS|LSAN_OPTIONS|UBSAN_OPTIONS):")
        self.assertNotRegex(self.configuration, r"(?m)^\s*continue-on-error:\s*true\s*$")

    def test_duplicate_runs_can_cancel_only_same_workflow_ref(self):
        self.assertIn("group: release-check-${{ github.workflow }}-${{ github.ref }}", self.configuration)
        self.assertIn("cancel-in-progress: true", self.configuration)
        self.assertIn("pull_request:", self.configuration)
        self.assertIn("workflow_dispatch:", self.configuration)
        self.assertIn('tags: ["v*"]', self.configuration)


if __name__ == "__main__":
    unittest.main()
