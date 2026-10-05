"""一场会跑在 assistant 发出去的 viba 工单上。

三个角色不再是三个各拿一份副本的进程，而是**服务**：一张能力表加一份共用的 store。消息总是先到
assistant 手上：它把人类说的话放进 store，再发一张工单（一份 viba 程序放进 `orders/`），然后自己跑
一遍——它做不了的步骤当场在 store 里留下 viba 工单（`prepare/<算子>.viba`，`$call` 已固定、
`$measured` 还是 nil），谁的能力表里有那个算子谁就接。

一轮只往前走一步：工单里留得下的是已经算好的实参，所以下一步的工单要等上一步答完、assistant 再跑一遍
才写得出来。每一份工单在发出时带着它的 `$call`，答完带着它的 `$measured`。

actor 和 critic 都是这样的服务：`workflow/tickets/` 那五张单一路走下来——写一版（actor）、判一版
（critic）、照判语改一版（actor）、判改完那版（critic）、收尾（assistant 自己说），每张单都是 assistant
读 store 决定发的。

模块：

| 模块 | 是什么 |
|---|---|
| `orders.py` | 工单在 store 里的位置：发单（把程序放进 `orders/`）、欠着的步骤（`prepare/` 里 `$measured` 还是 nil 的） |
| `service.py` | 一个成员作为一个服务：一张能力表，在共用的 store 上跑一张工单，报它答了哪几步 |
| `assistant.py` | assistant 这一侧：把人类说的话放进 store，跑一遍程序，把做不了的步骤写成工单 |
| `plan.py` | 该发哪一张：读 store 算 |
| `session.py` | 一场会的一轮一轮：发单、服务接单、什么时候算完 |
| `run.py` | 一句话进来跑到头：一条命令那个入口 |
| `meeting.py` | 会议目录那一侧：`<会议目录>/ordered/` 的 store 与记录 |
"""
