"""替身：装作干完了、装作判过了的两张能力表，专门拿来试"流程那一关看不看得见"。

这不是给真会议用的。真的 actor 是工作区里那个 agent（它把东西写进工作区），真的 tech_critic 会真跑
每一条验收条件。这两张表**答得像做完了、什么都没做**：

* `dummy_actor` —— 每一步都答一句像样的成品文字，但**一个字都不写进工作区**；
* `dummy_tech_critic` —— 一律答"合格"，**一条验收条件都不跑**。

一份规范的表里把 `agent` 那一行换成 `dummy_actor`、把 `tech_critic` 那一行换成
`dummy_tech_critic`，就是"有毒的规范"：该出的流程问题正好拿它试 `mgmt_critic` 看不看得见。
"""

from __future__ import annotations

from typing import Callable, Dict

from . import _viba  # noqa: F401

from viba.interpret import put_snapshot

from .critic import JUDGED, REASON

# 替身交出来的那一段"成品"：像一段代码，其实什么都没接上。
DUMMY_CODE = "def f(*args, **kwargs):\n    return 0\n"


def dummy_actor_caps(code: str = DUMMY_CODE) -> Dict[str, Callable]:
    """装作干活的 actor：四个算子都在，答的都是话，工作区里一个文件都不落。"""

    def acknowledge(env, about):
        return "知道了，这就照题目做一版"

    def do_step(env, task, after):
        return code

    def report_done(env, result, after):
        return "做好了"

    def redo_step(env, result, objection):
        return code

    return {"acknowledge": acknowledge, "do_step": do_step,
            "report_done": report_done, "redo_step": redo_step}


def dummy_tech_critic_caps() -> Dict[str, Callable]:
    """装作判过的 tech_critic：一律合格，验收条件一条都不跑。"""

    def judge(env, under_judgment):
        del under_judgment                       # 看都不看
        put_snapshot(env, "", REASON)            # 没有问题
        put_snapshot(env, True, JUDGED)          # 合格
        return True

    return {"judge": judge}


__all__ = ["dummy_actor_caps", "dummy_tech_critic_caps", "DUMMY_CODE"]
