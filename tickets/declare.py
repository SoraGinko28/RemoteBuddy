"""接口那几个声明：actor、critic、assistant 会哪些算子，写在另一个仓库里。

那一份接口两边都要：规范里工单和规则 `import` 它，程序把它实现出来。所以它既不放在规范那个仓库里，
也不放在程序这个仓库里，单独一个仓库（默认 `RbApiDeclare/rb_api/`），两边各自说自己按的是哪一版：

* 规范那一侧：`<规范目录>/api_declare`，一个文件一行，写着那个仓库的 commit id；
* 程序这一侧：发一张单的时候把这一版记在单上（`$handler_name2api_declare_repo_commit_id`），并把
  那个目录放进单上的 `$VIBA_PATH`，跑的时候按它找得到 `actor_api` 这些名字。

读的时候核一遍：那个仓库当前的 HEAD 就是 `api_declare` 写着的那个 commit，那份接口下没有没提交的
改动。对不上就当场说清楚——不然读进去的声明和记在单上的那一版不是同一份。

规范里没有 `api_declare` 这份文件，就是这份规范自己带 api（或者一个角色都不带）：这一处一份都不读，
单上那一项也就是空的。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

DECLARE_ENV = "REMOTEBUDDY_API_DECLARE"
DECLARE_FILE = "api_declare"
API_SUFFIX = "_api"
VIBA_SUFFIX = ".viba"
# 接口仓库里，声明放在这个包目录下；程序仓库旁边那一层
DECLARE_PACKAGE = Path("RbApiDeclare") / "rb_api"
DEFAULT_DECLARE = Path(__file__).resolve().parent.parent.parent / DECLARE_PACKAGE
# 写下来的 commit id 至少这么长：太短的没法说它是哪一版
SHORTEST_COMMIT = 7


def declare_home() -> Path:
    """接口那个仓库的声明目录在哪儿：`$REMOTEBUDDY_API_DECLARE` 指到哪儿就是哪儿。"""
    given = os.environ.get(DECLARE_ENV)
    return Path(given).expanduser() if given else DEFAULT_DECLARE


def declared_commit(root) -> str:
    """这份规范说它按哪一版接口写的：`<规范目录>/api_declare` 那一行。

    没有那个文件就是空串（这份规范自己带 api）。文件在、里面却没有一行说出版本，那是写错了。
    """
    path = Path(root) / DECLARE_FILE
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text.split()[0]
    raise ValueError(f"{path} 里没有一行说按哪一版接口写的")


def declared_apis(root, home=None) -> Tuple[Dict[str, str], str, Optional[Path]]:
    """`root` 这一份规范用的接口：`({名字: 原文}, commit, 目录)`。

    规范里没有 `api_declare` 就是 `({}, "", None)`。有它就把那个仓库的声明读一遍，读完核一下版本。
    """
    asked = declared_commit(root)
    if not asked:
        return {}, "", None
    where = Path(home) if home is not None else declare_home()
    files = {path.stem: path.read_text(encoding="utf-8")
             for path in sorted(where.glob(f"*{API_SUFFIX}{VIBA_SUFFIX}"))}
    if not files:
        raise FileNotFoundError(
            f"{Path(root) / DECLARE_FILE} 说了按 {asked[:SHORTEST_COMMIT]} 那一版写，"
            f"但 {where} 里一份 *{API_SUFFIX}{VIBA_SUFFIX} 都没有")
    head = head_of(where)
    if len(asked) < SHORTEST_COMMIT or not head.startswith(asked):
        raise ValueError(
            f"接口对不上：规范按 {asked} 写的，{where} 现在是 {head}。"
            f"把那个仓库切到写的那一版，或者改 {Path(root) / DECLARE_FILE} 这一行")
    uncommitted = uncommitted_in(where)
    if uncommitted:
        raise ValueError(
            f"{where} 里有没提交的改动（{'、'.join(uncommitted)}）：读进去的就不是 {head} 那一版了。"
            f"先提交，或者改 {Path(root) / DECLARE_FILE} 这一行")
    return files, head, where


def head_of(where) -> str:
    """这个仓库当前在哪一版：`git rev-parse HEAD`。"""
    done = _git(where, "rev-parse", "HEAD")
    if done.returncode != 0:
        raise ValueError(
            f"{where} 不是一个 git 仓库（读不出 HEAD）：{(done.stderr or '').strip()[-200:]}")
    return done.stdout.strip()


def uncommitted_in(where) -> list:
    """这份接口下有没有没提交的改动，一项一个文件名。"""
    done = _git(where, "status", "--porcelain", "--", ".")
    if done.returncode != 0:
        raise ValueError(f"读不出 {where} 的改动：{(done.stderr or '').strip()[-200:]}")
    out = []
    for line in done.stdout.splitlines():
        text = line.strip()
        if text:
            out.append(text.split(maxsplit=1)[-1])
    return out


def _git(where, *arguments):
    """在 `where` 里跑一条 git 命令。"""
    return subprocess.run(["git", "-C", str(where), *arguments],
                          capture_output=True, text=True, check=False)


__all__ = ["declare_home", "declared_commit", "declared_apis", "head_of", "uncommitted_in",
           "DECLARE_ENV", "DECLARE_FILE", "DEFAULT_DECLARE"]
