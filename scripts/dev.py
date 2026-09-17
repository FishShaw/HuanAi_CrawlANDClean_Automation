#!/usr/bin/env python3
"""本地开发入口（与 main 同一套机制）；检查暂存快照，合并失败时恢复到合并前。

本分支 feature/nanjing-eia-depts 是这条流水线的主干，待遇与 main 相同：
- 不能直接往主干提交，改动在 eia/<名称> 分支上做，再用 merge 合回主干；
- 普通提交跑 lint，合并跑 lint + 全部离线回归测试，失败自动撤销合并。

    .venv/bin/python scripts/dev.py setup            # 装钩子（新克隆 / 新 worktree 执行一次）
    .venv/bin/python scripts/dev.py start fix-xxx    # 从主干开 eia/fix-xxx
    .venv/bin/python scripts/dev.py check            # lint + 测试
    .venv/bin/python scripts/dev.py merge eia/fix-xxx   # 在主干上执行，合并前自动检查
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TRUNK = "feature/nanjing-eia-depts"
PROTECTED = {"main", TRUNK}


def run(*args, cwd=ROOT, capture=False, env=None):
    return subprocess.run(args, cwd=cwd, check=True, text=True, env=env,
                          stdout=subprocess.PIPE if capture else None).stdout


def git(*args, capture=False):
    return run("git", *args, capture=capture)


def require_clean():
    if git("status", "--porcelain", capture=True).strip():
        raise RuntimeError("工作区有未提交修改，请先提交或暂存后再操作。")


def lint(directory):
    run(sys.executable, "-m", "ruff", "check", ".", cwd=directory)


def test(directory):
    # 离线回归：不请求 i-ESG、不写飞书。原始记录 raw/ 不入库，从当前检出读取。
    env = {**os.environ, "EIA_RAW_DIR": str(ROOT / "raw")}
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v", cwd=directory, env=env)


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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
            raise RuntimeError("请输入改动名称，例如 start add-zhejiang。")
        git("switch", TRUNK)
        git("switch", "-c", "eia/" + args.name)
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
        if args.action == "pre-commit" and branch in PROTECTED and has_head and not merging():
            raise RuntimeError(f"{branch} 只接收通过测试的合并；请用 scripts/dev.py start <名称> 开分支提交。")
        staged_check(with_tests=args.action == "pre-merge" or merging())
    else:
        require_clean()
        if not args.name:
            raise RuntimeError("请输入待合并的分支。")
        if git("branch", "--show-current", capture=True).strip() != TRUNK:
            raise RuntimeError(f"请先 git switch {TRUNK}，再执行 merge。")
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
        print("合并成功，暂存快照已通过 lint 和离线回归测试。")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        print(f"停止：{exc}", file=sys.stderr)
        sys.exit(1)
