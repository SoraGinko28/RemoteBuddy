# ordered_meeting 的接口

消息先到 assistant，它发工单；每个成员是一个**服务**（一张能力表），在**一份共用的 store** 上接单。
这一版只有 actor 参与干活。

## store 里有什么

```
<store>/orders/<工单>/program/          这份工单的程序：几个 api 加它自己那张单子
<store>/<案子>/prepare/stated_task.viba 人类刚说的那句话（assistant 放进去的）
<store>/<案子>/<数据路径>/prepare/<算子>.viba   工单：$call 已固定，$measured 还是 nil
<store>/<案子>/<数据路径>/value.viba    这一步的结果（replayed 读写的文件）
```

**工单就是那份 `prepare/<算子>.viba`**，两部分各管一半：数据路径说有哪一步、要哪个算子；`$call` 说这一步
拿到了什么。它是自足的——谁的能力表里有那个算子，谁就能照它算完，把结果落到同一条数据路径下，并把同一
份结果填回 `$measured`。没有发给谁的包，也没有第二份 store。

## 一轮

1. **assistant 发单**：跑一遍工单程序。它能力表里没有的那一步，当场写成上面那份工单留在 store 里。
2. **每个服务跑一遍**：自己会的算子算、store 里已有结果的回放、其余递延。

顺序不能反：上一轮欠着的没补上就跑，只会在同一条数据路径上停第二次。跑到头（程序给出结果）是 `ok`；
一轮下来谁都没答出东西也没跑完是 `stuck`。

## 三个模块怎么用

```python
from ordered_meeting.session import Session
from ticket_centered import actor_caps

session = Session(store, "case/quicksort", "draft-1", workflow="workflow", ticket="write_a_draft")
session.state_the_task("写一个快速排序，放到 quicksort.py")   # 消息先到 assistant
session.publish()                                            # 发单
outcome = session.run([("the-actor", "actor", actor_caps(write=my_writer))])
```

| 模块 | 是什么 |
|---|---|
| `orders.py` | 发单（把程序放进 `orders/`）、欠着的工单（`prepare/` 里 `$measured` 还是 nil 的那些） |
| `service.py` | 一个服务跑一遍：自己算 / 回放 / 递延，报它答了哪几步 |
| `assistant.py` | 消息进 store、发单、把做不了的步骤写成工单 |
| `session.py` | 一轮一轮：发单、服务接单、什么时候算完 |

## 还没做的

* **actor 还是个函数**：这一版的 actor 是脚本化的 `write(prompt) -> code`（见
  `tests/test_ordered_meeting.py`）。真会议里它要是一个进程，走 `ticket_centered.actor.AgentChannel`
  那条路（说话进会议、等 agent 干完、从工作区读回文件）。
* **assistant 还只发一张单**：`write_a_draft` 是写死的。设计里"该发哪一张"要由 assistant 读 store
  算出来。
* **critic 还没有**：critic 的算子（`judge`）接进来，就是同一套——它的能力表里有 `judge`，工单里写上
  这一步，它就在自己的进程里接。
