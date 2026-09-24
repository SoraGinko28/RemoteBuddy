# 自进化：改哪一层、别踩什么

给驱动自进化的 agent 看的。这里只写**现在这份代码里真成立**的事：能改什么、改完怎么验、
以及真踩过的坑。

## 1. 能改的只有一层

| 层 | 谁动 |
|---|---|
| `workflow/`：三份角色 api、`tickets/*.viba`、`rules/*.viba` | **自进化**（纯 viba，改它不动任何 Python） |
| 能力表：`ticket_centered/{actor,critic,assistant}.py` 里那几张表、规则用的叶子算子 | **人工审核**（设计文档："表由人工审核，不在自演进的范围内"） |
| `ticket_centered/` 其余 Python、`web_v2.py`、`meeting_adt.py`、`MEETING_ADT.md` | 不归自进化 |

**推论（最容易撞的一条）**：一张新工单只能调这 7 个算子——actor 的 `acknowledge` / `write_code` /
`report_done` / `revise_code`，assistant 的 `stated_task` / `closing_note`，critic 的 `judge`；
一条新规则只能调 critic 表里那三个叶子——`code_under_judgment` / `widest_parameters` /
`within_limit`。想量一个新东西（比如"函数有没有超过 15 行"），那是**加能力表**，得先让人同意；
自进化只能在"会什么"不变的前提下改"怎么转、判什么"。

## 2. 改完怎么验（三层，从便宜到贵）

```bash
.venv/bin/python -m pytest tests/test_ticket_centered.py -q   # ① 设计的规矩 + 五张工单真跑一遍（写代码那步脚本化）
.venv/bin/python -m pytest tests -q                           # ② 别把产品弄坏（2 个 tmux 用例缺 conda 环境，本来就红）
.venv/bin/python e2e_ticket_meeting.py --keep                 # ③ 真会：真 agent、真 tmux、真 HTTP（花钱、几分钟）
```

①是硬门：它按设计的规矩断言，并且用真的解释器、真的 store、真的规则、真的成员进程把五张工单走完。
③跑完会**留在页面上**（`--finish leave` 是默认），会话头部那个「工单 n/5」按钮点开就是同一份东西。

## 3. 设计的规矩（测试里都是硬断言，不是建议）

* 五类文件：`actor_api.viba` / `assistant_api.viba` / `critic_api.viba` / `tickets/*.viba` /
  `rules/*.viba`；一张工单至少 import 它要调的那些角色的 api。
* 一张工单里每一步只写**调用**（不重复定义类型）；有 `__ret__`。
* 调到的算子必须在它 import 的那份 api 里。
* **工单里的数据路径都相对案子**：设计文件里不许出现带 `/` 的路径，也不许出现案子名。
* 规则是程序：`environ` 进、bool 出，有自己的 `__ret__`。

## 4. 真踩过的坑

1. **viba 只跑 `__ret__` 拉得着的东西。** 工单里几步要接成链（`$after …` / `$code …`），否则那一步
   **永远不发生**——`write_a_draft` 里"回一句知道了"就这么丢过一次（它没被任何一步读）。
2. **两步不能落在同一条 (数据路径, 算子) 上**：第二条会**回放**第一条的答案，不会重跑。要区分就把
   `$env (environ.sub_env << "…")` 的名字换掉（现在每步一个名字：`greeted`、`drafted`、`judged`…）。
3. **并集在实参/字段位置要加括号**：`<- $x (mc.A | nil)`；不加括号它漏到顶层（`MEETING_ADT.md`
   自己犯过这个，viba 的结合性是 `**` > `*` > `|`）。
4. **"该发哪一张"的判据** = 它要读的都有答案 ∧ 它自己的答案一个都还没有。所以：
   * 设计必须是 DAG（成环 → 谁也不该发）；
   * 一张工单不能把自己已经答过的路径再答一遍（那样它永远不该发）；
   * 几步之间的先后由数据依赖给，别指望"文件里写的先后"。
5. **同刻多张都该发时，次序是文件名字典序**（`workflow.tickets()`，不是流程序）。现在这五张同一时刻
   只有一张该发，所以顺序无所谓；新设计要么保持"同刻只该发一张"，要么把编号编进文件名。
6. **判语**：`objection` 是 critic 的 `judge` 那一步写下的（合格时是空串）。工单指向一条不存在的
   (路径, 算子)，成员进程会以"那一步还没答"失败——现场在 journal 的 `ticket_ran` 输出里，以及工单板上
   那一格没有 ✓。
7. **actor 的 `write_code` / `revise_code` 真的会叫 agent**（真花钱、真耗时）。判语说合格时
   `revise_code` 直接照原样返回、不再叫 agent。别写一张反复叫它"再交一版"的工单。
8. **要交哪个文件**：页面那条路是从**人类第一句话点名的文件名**取的（`quicksort.py`）当 agent 的产物；
   设计里换产物，得跟人类那句话对得上，否则发单会被拒（409 "没点名要交哪个文件"）。
9. **内容寻址**：`Workflow.load(dir).digest()` 是这份设计的名字，写在会议记录第一行；每张发出去的
   包里带着 `program/`（那份程序），所以**一张工单钉在发单那一刻的版本上**——改了 `workflow/` 不影响
   已经发出去的包，但下一张发的是新版本。同一场会里混版本时，记录里的摘要对不上就是证据。
10. **`is_complete` 还没接**：现在挡住明显写错的是第 2 节那一层测试；viba 自己的完整性检查还没进来。

## 5. 现场在哪

```
runs/<会议号>/ticket_centered/meeting_record.md     发过哪几张工单
runs/<会议号>/ticket_centered/store/                材料：每条数据路径下一条 Prepare（$call / $measured）
runs/<会议号>/ticket_centered/tickets/<工单>/<成员>/ 每个成员那份原件（program/ + store/）——对账靠它们
runs/<会议号>/streams/events.jsonl                  journal：state/ticket_issued、state/ticket_ran
GET /api/meetings/<会议号>/tickets                  页面那块板子（该发哪一张、谁答的、判语、记录）
```

## 6. 想试一份候选设计

* 放另一个目录，成员进程用 `--workflow <dir>` 起（`python -m ticket_centered.critic --meeting … --workflow …`）；
* **页面那条路固定读仓库根上的 `workflow/`**（`web_v2` 用的是 `WORKFLOW_DIR`），要上页面就得把候选放进
  `workflow/`；
* 候选的名字：`Workflow.load(dir).digest()`；先过第 2 节的 ①，再上真会。

## 7. 还没做、别当成有了

* **Redis 门铃**：设计里成员是被门铃叫醒、自己去 store 读；现在是发单那一侧直接给每个成员起进程。
* **能力表人工审核的那张清单**：表现在是 Python 里的 dict，没有一张可审的"算子 + 谁实现"清单。
* **写权校验**：每个角色只写自己名下那条数据路径——构造上成立，没人校验（`Meeting.reconcile()` 只比
  各成员包里那份副本跟会议 store 一不一致）。
* **页面上改流程**：板子只读 + 一个"发这一张"按钮。
