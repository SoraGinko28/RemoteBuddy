# tickets

一场会跑在一张一张发出去的**工单**上。消息先到 assistant：它把人类说的那句放进案子，再读案子里的值
算出该发哪一张，把那张单写下来。然后每个成员接单——自己会的算子算、案子里已有值的回放、其余答
`$not_implemented`（"这一步不是我的事"），于是一张单在几个成员之间轮着往前走，直到它的进度里有一条
`$ok`。

| 模块 | 是什么 |
|---|---|
| `layout.py` | 一场会的数据放在会议目录下面的哪几处（工单、进度、值、记录） |
| `ticket.py` | 一张工单：一次要跑的调用，和它跑在哪个案子上；怎么写、怎么读 |
| `progress.py` | 一张工单的进度：一次尝试一个文件；"有 `$ok` 就算做完" |
| `values.py` | 一个案子的值：一条数据路径下面挂一份 viba 快照 |
| `binding.py` | 一个角色和一次跑之间那层绑定：哪一步由谁答（回放 / 算 / 交出去），以及读一次跑的答复 |
| `handler.py` | 一个处理者：轮到它接哪几张单、怎么接、接完写哪一篇进度 |
| `table.py` | 这场会有哪几个成员：读规范旁边那张表（`<规范>/services`） |
| `plan.py` | 该发哪一张：读案子里的值算出来，不是写死的次序 |
| `issue.py` | 发一张单：号怎么起、单上那几样从哪儿来 |
| `session.py` | 一场会的一轮一轮：发单、接单、什么时候算完 |
| `meeting.py` | 会议目录那一侧：案子叫什么、一条消息进来怎么跑一趟 |
| `actor.py` / `critic.py` / `assistant.py` | 三个角色的能力表（RB 这一侧实现的那几件事） |
| `channel.py` | actor 那一条线：把题面发进会议、等 agent 干完、从它的工作区读回文件 |
| `member.py` | 一个成员进程的入口（`python -m tickets.actor …`） |
| `workflow.py` | 规范：读 `<规范>` 那一层（工单程序、请求、规则、成员表、自己带的角色） |
| `declare.py` | 接口那几份声明在另一个仓库里（`RbApiDeclare`）：按哪一版读、读的时候核什么，以及那张单、那次尝试、那份进度写成什么 |

## 盘上那几处

```
<会议目录>/tickets/<工单号>/ticket.viba              一张工单：那次调用本身，环境留空
<会议目录>/ticket_progress/<工单号>/<时刻>-<谁>.viba  处理它的每一次，一次一个文件
<会议目录>/store/<案子>/<数据路径>/<算子>.viba        每一步答下的值
<会议目录>/meeting_record.md                          这场会的记录
```

一张单上只有：它的号、它跑的是哪一段程序（`$ticket_name`）、那次调用（`$call`）、它跑在哪个案子上
（`$storage_path`）、按哪几个目录找模块（`$VIBA_PATH`，接口那个仓库的目录也在里面）、按哪一版接口写的
（`$handler_name2api_declare_repo_commit_id`）、写下的时刻。程序在规范里，单上不发副本。

**那几份数据写成什么，也不是这一处写的，但它就在这个仓库里**：一张单（`Ticket`）、一次尝试
（`TicketStage`）、一张单的全部进度（`TicketProgress = AppendOnlyList[TicketStage]`）三份定义都在
`tickets/ticket.viba` 里（`declare.py` 那份 `Definition` 就是读它）。程序照它拼、照它读；多一个成员、
少一个成员、类型不对，写的时候当场报出来。盘上一个文件就是那一列里的一项，按文件名的次序（名字以
写入时刻开头）。

actor、critic、assistant 会哪些算子不在这两个仓库的任何一处代码里，在 `RbApiDeclare` 那个仓库的
`rb_api/` 下：程序和规范**两边都以 submodule 依赖它**（`RemoteBuddy-core/RbApiDeclare/`、
`RbNorm/RbApiDeclare/`），指着同一个 commit。submodule 指着哪一版，就是哪一版；程序读自己那一份，
并核一下规范那份是同一个 commit。

**进度只增不删**：一次尝试写一个文件，名字带写它的时刻和是谁写的，所以两个人同时写也是两个文件，
不需要锁。一张单做完了没有，读的是它这一整份进度里**有没有**一条 `$ok`——这条只从"没有"变成"有"，
后来写下的停下记录顶不掉它。

## 谁是成员

`<规范>/services` 一行一个成员：**名字**、**它实现哪份 api**、**怎么找到它**（`agent` / `critic` /
`assistant` / `scripted <模块>:<函数>` / `process <命令…>`）。加一个成员是往这张表里加一行，Python
一行不用改；RB 这一侧只负责把表里那几种写法变成能接单的处理者。表里没写 `assistant` 也会有它一个：
人类那句话总得有人收、单总得有人发。

## 跑一场

程序里（`web.py` 那一条路）：

```python
from tickets.assistant import assistant_handler
from tickets.critic import critic_handler
from tickets.actor import live_handler, meeting_actor_caps
from tickets.meeting import run_for_meeting

handlers = [assistant_handler(meeting_dir, workflow), critic_handler(meeting_dir, workflow), actor]
run_for_meeting(meeting_dir, case="case/quicksort", words="写一个快速排序，放到 quicksort.py",
                workflow=workflow, handlers=handlers)
```

一个成员单独跑：

```bash
python -m tickets.actor     --meeting <会议目录> --case case/quicksort --writer 模块:函数
python -m tickets.critic    --meeting <会议目录> --case case/quicksort
python -m tickets.assistant --meeting <会议目录> --case case/quicksort --words "人类说的话"
```

看这一场现在是什么样：

```bash
python -c "from tickets.progress import the_state; import json; \
print(json.dumps(the_state('<会议目录>'), ensure_ascii=False, indent=1))"
```
