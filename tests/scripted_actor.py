"""""测试用的写手：这一版里 actor 的"写"是脚本化的。""

真会议里 `--writer` 不指这里，`tickets.actor` 走会议那条线（说话进会议、等那个 agent 干完、从它的
工作区读回文件）。测试要的是不依赖 agent 也能把工单这条链跑通：`write(prompt) -> code` 原样交一版，
别的都是真的（真解释器、真案子、真进程）。
"""

from __future__ import annotations

CODE = "def quick_sort(items, key=None, reverse=False, lo=0, hi=None):\n    return items\n"


def write(_prompt: str) -> str:
    """照这份题面写一版：测试里就是这一份。"""
    return CODE


def boom(_prompt: str) -> str:
    """写不动的那一份：这一趟会断在半路，拿来试"下一趟接着跑"。"""
    raise RuntimeError("写不动了")
