# ordered_meeting 的接口

消息先到 assistant，它发工单；每个成员是一个**服务**（一张能力表），在**一份共用的 store** 上接单。
actor 和 critic 都是这样的服务：工单里写着哪一步、要哪个算子，谁的能力表里有那个算子谁就接。

## 会议目录下面

会议那一侧给的是一个目录（`<会议目录>/`），这一侧在它下面另起一份：

```
<会议目录>/ordered/store/                 共用的 store（上面那三样都在这下面）
<会议目录>/ordered/meeting_record.md      这场会的记录：谁说了什么、发了哪张单、谁答了哪一步
```

一条消息进来就跑一趟（`ordered_meeting.meeting.run_for_meeting`，命令行上是 `--meeting`）：案子名和
agent 要交的那个文件都从人类那句话里来（`case_for` / `target_for`），页面上不另设一遍。

```bash
python -m ordered_meeting.run --meeting <会议目录> --order meeting --workflow workflow \
    --words "写一个快速排序，放到 quicksort.py" --writer 模块:函数
```

## store 里有什么

```
<store>/orders/<工单>/program/          这份工单的程序：它自己那张单子、几个 api、规则那几份，
                                        还有一份 `ticket`（写着这一目录里是哪张单子）
<store>/<案子>/prepare/stated_task.viba 人类刚说的那句话（assistant 放进去的）
<store>/<案子>/<数据路径>/prepare/<算子>.viba   工单：$call 已固定，$measured 还是 nil
<store>/<案子>/<数据路径>/value.viba    这一步的结果（replayed 读写的文件）
```

**工单就是那份 `prepare/<算子>.viba`**，两部分各管一半：数据路径说有哪一步、要哪个算子；`$call` 说这一步
拿到了什么。它是自足的——谁的能力表里有那个算子，谁就能照它算完，把结果落到同一条数据路径下，并把同一
份结果填回 `$measured`。没有发给谁的包，也没有第二份 store。

## 一轮

1. **assistant 发单**：跑一遍工单程序。已经答过的回放，头一个欠着的那一步当场写成上面那份工单
   （`$call` 就是它这一步拿到的实参）。
2. **每个服务接单**：`owed` 里凡是它会的算子，照那份工单的 `$call` 算掉，结果填回 `$measured`。

一轮只往前走一步：一份工单里留得下的是**已经算好的实参**，所以下一步的工单要等上一步答完、assistant
再跑一遍才写得出来（顺序反了只会在同一条数据路径上停第二次）。assistant 那一趟没有递延，就是整张工单
跑到了头——`ok`；一轮下来谁都没答出东西也没跑到头，就是 `stuck`。

所以每一份工单在发出时都带着它的 `$call`，答完带着它的 `$measured`；两者都在同一个文件里。

## 三个模块怎么用

```python
from ordered_meeting.session import Session
from ticket_centered import actor_caps

session = Session(store, "case/quicksort", "draft-1", workflow="workflow", ticket="write_a_draft")
session.state_the_task("写一个快速排序，放到 quicksort.py")   # 消息先到 assistant
session.publish()                                            # 发 store 说该发的那一张
outcome = session.run([("the-actor", "actor", actor_caps(write=my_writer))])
```

会议那一侧要用的就是一条命令：一句话进来，assistant 发单、actor 接单，跑到头（结局只有 `ok` 退出码
是 0）。真会议里把 `--writer 模块:函数` 换成 `--meeting-url` / `--meeting-id` / `--workspace` / `--target`：

```bash
python -m ordered_meeting.run --store <store> --case case/quicksort --order draft-1 \
    --workflow workflow --words "写一个快速排序，放到 quicksort.py" --writer 模块:函数
```

两个进程也是这两个入口——一条命令一个服务，共用的只有那份 store：

```bash
python -m ordered_meeting.assistant --store <store> --case case/quicksort --order draft-1 \
    --workflow workflow --ticket write_a_draft --words "写一个快速排序，放到 quicksort.py"
python -m ordered_meeting.actor --store <store> --case case/quicksort --order draft-1 \
    --meeting-url http://127.0.0.1:8765 --meeting-id <会议号> --workspace <干活目录> --target quicksort.py
```

assistant 那一行报它写下了哪几张工单；actor 那一行报它答了哪几步（默认 `--phase answer`：补上欠着的那一步；`--phase run` 是自己跑一遍整
张单，用来回放核对）。真会议那一侧不给 `--writer`：能力表就是会议里那个 agent —— `acknowledge` /
`report_done` 读的是它自己在会议里说的那句，`write_code` / `revise_code` 把题面说进会议、等它干完、
从它的工作区把文件读回来（`ordered_meeting.actor.live_service`）。`--writer 模块:函数` 是脚本化的那条
路：写只是一个 `write(prompt) -> code`，测试用它替代那个 agent。

| 模块 | 是什么 |
|---|---|
| `orders.py` | 发单（把程序放进 `orders/`）、欠着的工单（`prepare/` 里 `$measured` 还是 nil 的那些） |
| `service.py` | 一个服务的两件正事：接单（照工单的 `$call` 算，`answer_service`）、自己跑一遍整张单（自己算 / 回放 / 递延，`run_service`） |
| `assistant.py` | 消息进 store、发单、把做不了的步骤写成工单 |
| `plan.py` | 该发哪一张：读 store 算（要读的都在、自己还没做过），不是写死的次序 |
| `actor.py` | actor 的服务进程：一张能力表，接单一趟（脚本化的 `--writer` 或真会议那只 agent） |
| `critic.py` | critic 的服务进程：`judge` 加上规则那几个算子，接单一趟 |
| `session.py` | 一轮一轮：发单、服务接单、什么时候算完 |
| `run.py` | 一句话进来跑到头（一条命令那个入口）：消息给 assistant、发单、服务接单、报结局 |
| `meeting.py` | 会议目录那一侧：`<会议目录>/ordered/` 的 store 与记录、案子名和要交的文件名从人类那句话来 |

## 还没做的

* **只有这几张单**：五张单是一条定死的流程（判完第一版照判语改一版，收尾判的是改完的那一版）；"该发
  哪一张"是读 store 算的，但流程本身还是事先设计好的那五张。
* **只有 actor 的那条路**：`--actor-only` 跑得动，停在判语那张（没人接，账上是 `stuck`）——这是它该有的
  样子，不假装跑完。
