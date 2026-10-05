# ticket_centered

## 一张工单是什么

能带走的一包东西：**程序**（一张工单一个程序）+ **这一步要读的数据**（store 里那几条路径下的
值）。示例工单：

```viba
# workflow/tickets/write_a_draft.viba
__decl__ =
    ($module_path str * $func_name str)
  <- $env Env

args = __get_args__ << __decl__

import actor_api as actor

greeted = actor.acknowledge          # 回一句"知道了"
  << $env ($sub_env << args.env << "greeted")
  << $about ($module_path "" * $func_name "stated_task")

drafted = actor.write_code           # 写一版
  << $env ($sub_env << args.env << "drafted")
  << $task ($module_path "" * $func_name "stated_task")


__impl__ = actor.report_done         # 回一句"做好了"
  << $env ($sub_env << args.env << "report_done")
  << $code drafted

```

* 每一步是一次调用：调哪个角色 api 的哪个算子。这张工单三步都是 actor 的（别的工单可以几个角色
  混着调）。
* `$env` 给的是环境：这一步在哪条数据路径上跑，答案就落在哪儿。
* 其余实参是 `$module_path` 和 `$func_name`（要读谁答的：哪条路径下、哪个定义）——值不在实参
  里，在 store 里；要读哪几条路径，由这两样指。工单里的路径都**相对案子**：`""` 是案子自己，
  `"draft"` 是这一版；案子名在工单里不出现，由发出去的那一侧（跑在哪个案子下）给。
* 那两句回话也是工单规定的步骤：这张工单里没有 assistant 发言的步骤——需要它发言的工单，就写一步
  在它名下。

这一包发给会上的每个成员；各人拿一份自己的副本，在包里跑一遍——自己会的算子算、store 里有答案的
回放、其余递延；答下的值抄回会议。

## 一套怎么转

* **只有一份共享的数据**：一个 meeting store，装着这场会的会议记录和值（值按数据路径放）。
  Redis 只是门铃——消息说"有事发生了"，被叫醒的人自己去 store 读；消息里不存状态，也不做决定。
* **每一步做什么，写在 viba 里**：三个角色的 api 写各自会什么；工单程序写每一步调谁、在哪条数据
  路径上、接在哪一步之后；规则写什么算不合格。
* **进程只做两件事**：自己会的算子就按实现了算；store 里已经有答案的就回放；都不会就递延——
  那一步交给别人。
* **没有调度者**：assistant 看 store 里已经有什么，决定该发哪一张工单；一张工单发给会上的**每个**
  成员，各人跑同一个程序、做自己那几步（不是自己的就递延）。

## 本设计由这五类文件组成

这五类放在**一份流程目录**里：程序按 `--workflow` → `$REMOTEBUDDY_WORKFLOW` → 项目自己的
`.agents/workflow/` → 配置目录 `~/.remotebuddy/workflow/` → 程序里带的默认 的次序找一份，找到谁用谁。
改流程的人只拿到那份目录（像 skills 之于 Claude Code），程序本身不给他。

1. `workflow/actor_api.viba` —— actor 会做的事
2. `workflow/assistant_api.viba` —— assistant 会做的事
3. `workflow/critic_api.viba` —— critic 会做的事
4. `workflow/tickets/*.viba` —— 工单程序：一场会很多张（这一趟五张：`write_a_draft`、
   `judge_the_draft`、`revise_the_draft`、`judge_the_revision`、`close_the_meeting`），都是开会前
   设计好的；assistant 看情况发起哪一张
5. `workflow/rules/*.viba` —— 规则：critic 跑的程序

三个角色各自的事只在各自的 api 里定义一次；**一张工单至少 import 它要调的那些角色的 api**
（`import actor_api as actor` 这样），每一步只写调用，一个问题都不重复定义。一张工单可以同时调几个
角色的算子——不同步可以归不同人做；规则也是单独的程序：critic 拿那一份，自己找出可能违反的那几条。

## 三个角色各会什么

| 角色 | api 文件 | 会做的事 |
|---|---|---|
| actor | `actor_api.viba` | 回一句"知道了"、写一版代码、回一句"做好了"、照判语改一版 |
| assistant | `assistant_api.viba` | 把人类说的话放进 store（落在案子的路径下）、收尾说一句 |
| critic | `critic_api.viba` | 判：判的是哪一份，由工单给；**哪些规则可能被违反，由它自己找**（规则是它自己带着的） |

每一步的结果和实参都是 `$module_path` 和 `$func_name`（这一步答在哪条路径下、哪个定义）；值
不在实参里，在 store 里。

## 跑起来是什么样

`.venv/bin/python e2e_ticket_meeting.py`：开一场**真会**（真 agent、真 tmux、真 HTTP），只走页面那几条
HTTP 面——assistant 把人类那句话放进 store、读工单板、发该发的那一张；服务那一侧把包发给每个成员、
给每个成员起一个进程。真实的一次（当时的会议目录 `runs/ticket-9ea7a401`）：

```
—— assistant 看情况发起第 1 张 ——
    write_a_draft：该发（要读的都在，自己还没做过）
  工单：write_a_draft（页面那条 POST 就是它）
  ← case/quicksort/greeted + acknowledge ✓（actor）
  ← case/quicksort/drafted + write_code ✓（actor）
  ← case/quicksort/report_done + report_done ✓（actor）
—— assistant 看情况发起第 2 张 ——
    write_a_draft：不发（case/quicksort/greeted + acknowledge 已经答过了）
    judge_the_draft：该发（要读的都在，自己还没做过）
  工单：judge_the_draft（页面那条 POST 就是它）
  ← case/quicksort/judged + judge ✓（critic）
—— ……五张：`write_a_draft`、`judge_the_draft`、`revise_the_draft`、`judge_the_revision`、
     `close_the_meeting`，每张一个包、发给每个成员 ——
```

（"该发/不发"是读 store 算出来的，不是写死的顺序。一场跑完，工单那一侧留在会议目录的
`ticket_centered/` 下（会议目录 = `<工作区>/.meeting/<会议号>/`）：值在 `store/`，每个成员那份
原件在 `tickets/<工单>/<成员>/`，会议记录在 `meeting_record.md`；页面上会话头部那个「工单」按钮
点开的就是同一份东西。）

## 为什么这么设计

* **underlying_ops 会收敛，而且收敛得动**：它是宿主那张能力表，也是唯一的能力面——`interpret`
  自己不带实现，一段 viba 能调到的算子一个都跑不出这张表。表由人工审核，不在自演进的范围内。
* **viba 代码可以随便变**：工单和规则都是 viba 程序，改它们不动能力面；这一层交给自演进——内容
  寻址、带版本，新版本够不到正在跑的会。
* **所以没有"任意代码"那类安全问题**：一段 viba 再怎么写，也只能发出表里那些算子的效果。
* **所以系统可以很简单**：harness 只剩三件事——一个 store、一个"该发哪一张"的判据、几张能力表；
  顺序、回话、判什么、改什么，全在 viba 里。
* **store 不需要可信**：每个角色只能写自己名下那条数据路径；同一份记录，每个成员的包里都留了一份
  自己的副本，对账和交叉验证靠它们。数据路径的写权是秩序问题（别互相覆盖），不是信任问题。
