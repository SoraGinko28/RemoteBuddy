# ticket_centered 的接口

一场会跑在一叠**事先设计好**的工单上。harness 只有三件事，别处不许再做决定：

1. 一个 store（数据路径下面挂值，会议记录也在里头）；
2. 一个"该发哪一张"的判据（读 store 算，不是写死的顺序）；
3. 几张能力表（每个成员一张：它会哪些算子）。

顺序、回话、判什么、改什么、谁接在谁后面，全在规范里（`RbNorm` 里那几类 viba 文件，程序一行
都不带）。设计见
`../ticket_centered_workflow/ticket_centered.md`。

跑在一场**真实的会**上（真 agent、真 tmux、真 HTTP、真规则）：
`.venv/bin/python e2e_ticket_meeting.py`（`--workspace DIR` 就把这场会开在那个工作区里：数据进
`<DIR>/.meeting/<会议号>/`，agent 在 `<DIR>` 根下干活）

自进化（改规范那一侧）：看 [`self_evolution.md`](self_evolution.md) —— 位置、怎么验、你会踩的坑。

## 规范在它自己的仓库里（不是程序里）

像 Claude Code 的 skills 之于它的二进制：规范（工单、规则、三个角色的 api、服务表）在
[`RbNorm`](https://github.com/lixinqi/RbNorm)，程序只负责**找到那个目录**，读它：

```
1. --workflow <dir>（或调用时给的 root）
2. $REMOTEBUDDY_WORKFLOW
3. $REMOTEBUDDY_NORM
4. 默认：程序仓库旁边那一层的 RbNorm
```

```bash
python -m ticket_centered.workflow --where    # 看这一趟读的是哪份规范、里面有什么、摘要是什么
```

自进化改的就是那个仓库；**程序不用给他，也不用动**。工单板和会议记录里都写着这一场读的是哪份
规范、摘要是什么。发单时那份摘要也写进 store（`orders/<这一趟>/norm`）——哪一版规范发的，事后查得到。

## 五类文件（`RbNorm/`）

| 文件 | 是什么 |
|---|---|
| `actor_api.viba` | actor 会做的事：`acknowledge` / `write_code` / `report_done` / `revise_code` |
| `assistant_api.viba` | assistant 会做的事：`stated_task` / `closing_note` |
| `critic_api.viba` | critic 会做的事：`judge` |
| `tickets/*.viba` | 工单程序：一场会很多张，都是开会前设计好的；一张至少 import 它要调的那些 api |
| `rules/*.viba` | 规则：critic 跑的程序，environ 进、bool 出 |

**工单就是 viba 代码**，没有另一种东西叫"工单"。一张工单里的数据路径都**相对案子**：`""` 是案子自己，
`"drafted"` 是这一步；案子名在工单里不出现，由发出去的那一侧给。一步的实参是地址
（`$module_path … * $func_name …`）：值不在实参里，在 store 里。

## 谁接哪一头

| 谁 | 调什么 |
|---|---|
| 会议的数据（store 服务端） | `Store`：`read` / `write` / `answered` / `stored` / `record`；值按数据路径放，会议记录在旁边 |
| 每个成员的进程 | `python -m ticket_centered.{actor,critic,assistant} --meeting … --case … --bundle …`：`member.run_bundle` 在发给它的那一份里跑一遍，`Member.at(...)` → `Runner` 做"自己算 / 回放 / 递延" |
| assistant 的派单那一侧（唯一的派单者） | `Meeting.due(ticket, case)` / `next_ticket(case)` 决定该发哪一张；`Meeting.publish(ticket, case)` 把一张发给会上的每个成员 |
| 自进化的负责人 | `Workflow.load(dir)` / `workflow.digest()` / `workflow.tickets()` / `workflow.rules()` / `workflow.ops_of(api)` / `workflow.role_of_op(op)`；一场会记着它跑的是哪份流程的摘要 |
| 工单系统（界面、对账、审计） | `Meeting.store.stored()` / `Meeting.backups` / `Meeting.reconcile`；成员包里那份 `store/` 是备份 |
| 页面（`web.py` + `frontend/workspace.js`） | `GET /api/meetings/<id>/tickets` 是工单板（该发哪一张、每张要读什么/谁答的、判语与收尾、会议记录）；`POST /api/meetings/<id>/tickets/<工单>` 发这一张：服务侧 `Meeting.publish` + 给每个成员起一个进程。会话头部那个「工单 n/5」按钮就是它 |
| 真实的会（`web` 服务 + tmux 里的 agent） | `ticket_centered.actor.AgentChannel`：`say` 说一句、等那个 agent 干完、从它的工作区读回文件 |

## 三个角色各自的进程

三个角色各有自己的文件，也各有自己的进程：`actor.py` / `critic.py` / `assistant.py`——每个文件里是
**那个角色的能力表**加上**那个进程的入口**；三个进程共用的那点东西（解析 `--meeting/--case/--bundle`、
在包里跑、把答案抄回会议）在 `member.py`。

```
python -m ticket_centered.actor     --meeting <会议目录> --case case/quicksort --bundle <发给我的那一包> \
    --meeting-url http://127.0.0.1:PORT --meeting-id <会议号> --workspace <agent 干活的目录> --target quicksort.py
python -m ticket_centered.critic    --meeting <会议目录> --case case/quicksort --bundle <发给我的那一包>
python -m ticket_centered.assistant --meeting <会议目录> --case case/quicksort --words "人类说的话"
python -m ticket_centered.assistant --meeting <会议目录> --case case/quicksort --bundle <发给我的那一包>
```

一个成员的进程拿到"发给我的那一包"（`tickets/<工单>/<成员>/`：`program/` 是程序，`store/` 是这一步要
读的数据），在自己的副本里跑一遍：自己会的算子算、store 里已有答案的回放、其余递延；**答下的值抄回
会议**。包的路径里就有工单名和成员名，不用另外带一封信。

`assistant` 的 `stated_task` 不在任何工单里（那是"把人类说的话放进 store"这一步），所以给
`--words` 的时候不带 `--bundle`。

## 发工单那一侧：一张发给每个成员

`Meeting.publish(ticket, case)` 把一张工单做成包（`program/` 是程序，`store/` 是这一步要读的数据，
连它自己读的一起抄、已经答过的也抄——回放比重做便宜），发给会上的**每个**成员，返回"发给谁 → 它那一包
在哪"。谁跑、怎么跑是成员那一侧的事（`--bundle` 指到哪一份，就是发给谁的）。跑完 `Meeting.due` /
`next_ticket` 再读一次 store 决定下一张。

## 这张流程里五张工单怎么转

`write_a_draft`（actor：回一句知道了 → 写一版 → 回一句做好了）→ `judge_the_draft`（critic）→
`revise_the_draft`（actor：照判语改一版）→ `judge_the_revision`（critic）→ `close_the_meeting`
（assistant 收尾）。每一次由 store 里的值算出来：要读的都在、它自己那几步还没答，就该发。

## 还没做的

* **Redis 只是门铃**：设计里成员是被门铃叫醒、自己去 store 读；现在没有门铃——发单的那一侧直接把
  包交给成员（每个成员一个进程）。
* **能力表人工审核**：表还是 Python 里的 dict，没有一张可直接签字的清单；现在能查的是它的**摘要**
  （`host.capability_digest()`，写在会议记录第一行）——变了看得出来，合不合规要人看。
* **写权**：`Meeting.audit()` 会查——每个成员自己答了哪几步在会议记录里留了账，按"算子归哪个角色"
  核对，不是自己名下的就报出来。它查不出的是**伪造**（那要靠各成员包里那份副本对账 + 人签字）。
* **`is_complete`**：viba 自己那个"设计完不完整"的检查还没接进来（设计里那几条规则现在在测试里）。
* 页面那块工单板是只读加一个"发这一张"按钮：能发、能看，但还不能在页面上改流程、看每个成员包里的原件。
