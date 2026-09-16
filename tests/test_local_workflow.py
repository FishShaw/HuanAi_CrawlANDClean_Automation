"""在临时仓库验证真实 Git 钩子，不修改项目分支或业务数据。"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LocalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eia-git-test-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
        self.call("git", "init", "-b", "main")
        (self.repo / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/dev.py", self.repo / "scripts/dev.py")
        shutil.copytree(ROOT / ".githooks", self.repo / ".githooks")
        shutil.copy(ROOT / "pyproject.toml", self.repo / "pyproject.toml")
        # 复用运行测试的解释器及依赖，钩子始终使用确定的 Python。
        (self.repo / ".venv").symlink_to(sys.prefix, target_is_directory=True)
        (self.repo / ".gitignore").write_text(".venv/\n.ruff_cache/\n")
        (self.repo / "nanjing_departments_workflow.py").write_text("raise SystemExit(0)\n")
        self.call("git", "add", ".")
        self.call("git", "commit", "-m", "baseline")
        self.dev("setup")
        self.dev("start", "example")

    def call(self, *args, ok=True):
        result = subprocess.run(args, cwd=self.repo, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stdout.strip()

    def dev(self, *args, ok=True):
        return self.call(sys.executable, "scripts/dev.py", *args, ok=ok)

    def commit_code(self, content):
        (self.repo / "nanjing_departments_workflow.py").write_text(content)
        self.call("git", "add", ".")
        self.call("git", "commit", "-m", "feature")

    def test_staged_lint_blocks_commit_even_if_worktree_fixed(self):
        head = self.call("git", "rev-parse", "HEAD")
        source = self.repo / "bad.py"
        source.write_text("def broken(:\n")
        self.call("git", "add", "bad.py")
        source.write_text("value = 1\n")
        output = self.call("git", "commit", "-m", "bad", ok=False)
        self.assertIn("invalid-syntax", output)
        self.assertEqual(head, self.call("git", "rev-parse", "HEAD"))

    def test_failed_tests_abort_merge_and_preserve_branch(self):
        self.commit_code("raise SystemExit(1)\n")
        feature = self.call("git", "rev-parse", "HEAD")
        self.call("git", "switch", "main")
        head = self.call("git", "rev-parse", "HEAD")
        self.dev("merge", "feature/example", ok=False)
        self.assertEqual(head, self.call("git", "rev-parse", "HEAD"))
        self.assertEqual(feature, self.call("git", "rev-parse", "feature/example"))
        self.assertEqual("", self.call("git", "status", "--porcelain"))
        self.assertFalse((self.repo / ".git/MERGE_HEAD").exists())

    def test_passed_tests_allow_merge_commit(self):
        self.commit_code("# New feature\nraise SystemExit(0)\n")
        self.call("git", "switch", "main")
        self.dev("merge", "feature/example")
        self.assertEqual(3, len(self.call("git", "rev-list", "--parents", "-n", "1", "HEAD").split()))

    def test_native_merge_is_also_blocked(self):
        self.commit_code("raise SystemExit(1)\n")
        self.call("git", "switch", "main")
        head = self.call("git", "rev-parse", "HEAD")
        self.call("git", "merge", "feature/example", ok=False)
        self.assertEqual(head, self.call("git", "rev-parse", "HEAD"))
        self.call("git", "commit", "--no-edit", ok=False)
        self.call("git", "merge", "--abort")

    def test_conflict_aborts_cleanly(self):
        self.commit_code("# feature\nraise SystemExit(0)\n")
        self.call("git", "switch", "-c", "feature/other", "main")
        self.commit_code("# other\nraise SystemExit(0)\n")
        self.call("git", "switch", "main")
        self.dev("merge", "feature/other")
        head = self.call("git", "rev-parse", "HEAD")
        self.dev("merge", "feature/example", ok=False)
        self.assertEqual(head, self.call("git", "rev-parse", "HEAD"))
        self.assertEqual("", self.call("git", "status", "--porcelain"))

    def test_direct_main_commit_blocked(self):
        self.call("git", "switch", "main")
        (self.repo / "new.py").write_text("value = 1\n")
        self.call("git", "add", "new.py")
        self.call("git", "commit", "-m", "direct", ok=False)


if __name__ == "__main__":
    unittest.main()
