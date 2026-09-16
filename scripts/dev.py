#!/usr/bin/env python3
"""本地开发入口；检查暂存快照，合并失败时恢复到合并前。"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=ROOT, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def git(*args, capture=False):
    return run("git", *args, capture=capture)


def require_clean():
    if git("status", "--porcelain", capture=True).strip():
        raise RuntimeError("工作区有未提交修改，请先提交或暂存后再操作。")


def lint(directory):
    run(sys.executable, "-m", "ruff", "check", ".", cwd=directory)


def test(directory):
    # 仅离线回归；不采集网站，不生成或覆盖正式工作簿。
    run(sys.executable, "nanjing_departments_workflow.py", "--self-test", cwd=directory)
    if (directory / "tests").is_dir():
        run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v", cwd=directory)


def staged_check(with_tests=False):
    if git("ls-files", "-u", capture=True).strip():
        raise RuntimeError("仍有未解决的合并冲突。")
    # 导出索引，不用工作区内容；防止未暂存修复掩盖待提交错误。
    with tempfile.TemporaryDirectory(prefix="eia-staged-") as temp:
        snapshot = Path(temp)
        git("checkout-index", "--all", f"--prefix={snapshot}{os.sep}")
        lint(snapshot)
        if with_tests:
            test(snapshot)


def merging():
    return subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          cwd=ROOT, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "start", "lint", "test", "check", "merge", "pre-commit", "pre-merge"])
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()
    if args.action == "setup":
        git("config", "--local", "core.hooksPath", ".githooks")
        git("config", "--local", "merge.ff", "false")
        for hook in (ROOT / ".githooks").iterdir():
            hook.chmod(0o755)
        print("已启用提交及合并检查；常规合并强制生成合并提交。")
    elif args.action == "start":
        require_clean()
        if not args.name:
            raise RuntimeError("请输入新功能名称，例如 start add-source。")
        git("switch", "main")
        git("switch", "-c", "feature/" + args.name)
    elif args.action == "lint":
        lint(ROOT)
    elif args.action == "test":
        test(ROOT)
    elif args.action == "check":
        lint(ROOT)
        test(ROOT)
    elif args.action in ("pre-commit", "pre-merge"):
        branch = git("branch", "--show-current", capture=True).strip()
        has_head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if args.action == "pre-commit" and branch == "main" and has_head and not merging():
            raise RuntimeError("main 只接收通过测试的合并；请在功能分支提交。")
        staged_check(with_tests=args.action == "pre-merge" or merging())
    else:
        require_clean()
        if not args.name:
            raise RuntimeError("请输入待合并的功能分支。")
        if git("branch", "--show-current", capture=True).strip() != "main":
            raise RuntimeError("请先 git switch main，再执行 merge。")
        # 确保 hooks 已安装，避免漏掉最后提交时的检查。
        git("config", "--local", "core.hooksPath", ".githooks")
        try:
            git("merge", "--no-ff", "--no-commit", args.name)
            if not merging():
                print("该分支已合并，无需重复操作。")
                return
            # pre-commit 对合并后的暂存快照执行 lint 和完整测试。
            git("commit", "-m", f"Merge {args.name} (local checks passed)")
        except BaseException:
            if merging():
                git("merge", "--abort")
            raise
        print("合并成功，暂存快照已通过 lint 和离线测试。")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        print(f"停止：{exc}", file=sys.stderr)
        sys.exit(1)
