# rb：两个子命令，一个开一场会，一个开一个网页看它

`rb` 是 `RemoteBuddy-core/rb.py`，像 `git` 一样带子命令：

    rb cli "写一个快速排序，放到 quicksort.py"      给一句话就开一场会，跑到它自己停下
    rb web --workspace ~/work                       开一个网页看这个工作区里所有的会

两个子命令用的是同一份会议数据（`<工作区>/.meeting/`）：`rb cli` 驱动一场会，`rb web` 看它——
一边跑一边看，跑完之后再看，看到的是同一场会。所以已经跑完的会也在那个网页里。

要在命令行上直接敲（像 `git` 那样），装一个入口就行 —— 这台机器上装的是 `~/.local/bin/rb`
（它已经在 `PATH` 里）：

    cat > ~/.local/bin/rb <<'SH'
    #!/bin/sh
    exec <RemoteBuddy-core>/.venv/bin/python <RemoteBuddy-core>/rb.py "$@"
    SH
    chmod +x ~/.local/bin/rb

`<RemoteBuddy-core>` 就是这棵树在哪（这台机器上是 `/Users/bytedance/workspace/RemoteBuddy-core`）。
不装入口也行：`<RemoteBuddy-core>/.venv/bin/python <RemoteBuddy-core>/rb.py cli "…"`。

`rb cli` 不是 TUI，也不等人输入：命令行上那句话说出去，它把这场会跑到停下，然后退出。它的
终端输出只有一行：看这场会该跑的那条 `rb web` 命令。它自己不开网页服务。

## 1. `rb cli`

    PROMPT                              交给这场会的那句话；多个词用空格接起来，`-` 从标准输入读
    --resume MEETING                    接着跑这一场会：`<工作区>/.meeting/<会议号>`
    --meeting-id ID                     这场会叫什么；不给自己起一个
    --workspace DIR                     这场会在哪个工作区里跑；不给就是现在这个目录
    --harness NAME                      新会话默认用哪套：claude、responses、codex、trae（别名也认）
    --norm-repo-path DIR                规范仓库在哪儿
    --norm-commit-id SHA                读规范仓库的哪一版
    --no-interference-rule-path FILE    干涉规则那份 viba 程序在哪儿
    --no-interference-rule-commit-id SHA 干涉规则读哪一版
    --json                              那一行命令写成一条 JSON
    -h / -V                             帮助 / 版本

参数跟着 DeepSeek Harness 那一套来（`dsh --profile headless`）：一份任务、`-` 读标准输入、
`--json` 一行一条 JSON、`-h` / `-V`；`dsh` 用 `--session-id` 接上一个已有的会话，这一处用
`--resume` 接上一场已有的会（给它的数据目录）。

跑起来的第一件事就是印那一行：

    rb web --workspace /Users/…/work

这一行随时可以拿去跑：正在跑的时候跑，看得见它一边跑；跑完之后跑，看得见它停下时的样子。
路径里有空格会加引号。`--json` 时是：

    {"type":"web","command":"rb web --workspace /Users/…/work","meeting_id":"20261012-131301-ae9b48","workspace":"/Users/…/work"}

正常跑完，stdout 上就这一行，别的什么都不印；出错了才在 stderr 上说一句（哪一步、为什么），
退出码说明结果。

## 2. `rb web`

    --workspace DIR   看哪个工作区；不给就是现在这个目录
    --host HOST       听哪个地址（只认本机），默认 127.0.0.1
    --port PORT       听哪个端口；默认 0，就是让系统挑一个没人用的
    --harness NAME    新会话默认用哪套：claude、responses、codex、trae（别名也认）

这就是原来那个网页服务（`python web.py`）：它印出链接，然后一直开着，Ctrl-C 收摊。

    RemoteBuddy workspace: http://127.0.0.1:57554/
    工作区：/Users/…/work（会议数据在 /Users/…/work/.meeting/）

页面就是这个工作区的全部会议：正在跑的、已经跑完的、停掉的，都在侧边栏里，点开能看对话、
终端、工单、记录。端口默认让它自己挑，是因为这个工作区旁边可能已经有一台服务占着 8765。

## 3. 规范读的是哪一版

`--norm-repo-path` 给一个仓库（或者它底下的规范目录），`--norm-commit-id` 给一个提交。

* 给了提交：`git archive <提交>` 把那一版的树取到一个临时目录，这一趟读的是它（读出来的字节就是
  那一版的字节；工作树后来改成什么样子都不影响这一趟）。这个目录在跑完之后删掉。
* 没给提交：读那个目录现在的样子。这一趟记住的版本是空串。
* 只给提交、不给仓库：当场拒，不开会。

取出来的东西按 `REMOTEBUDDY_NORM` 交给会议那一侧（`tickets/workflow.py` 的那条路），所以
规范是直接放在仓库根上、还是放在它底下的 `rb_norm/`，两种都认。这份东西不像一份规范
（缺 `ticket_programs/`、`intake/`、`acceptance/`）就当场报出来，不开会。

## 4. 干涉规则：一份 viba 程序

规则是 viba 程序（`interpret` 跑它，和 `acceptance/*.viba` 走同一个解释器）。每观测到一下就把
这一下交给它一次，它能调到这几个算子（都是 `<- $env Env`）：

    the_what        str    这一下是哪一路："event"（journal 里多了一条不是这一趟写的）/
                           "driver"（驱动权被拿走）
    the_actor       str    谁：journal 那条的 actor；驱动权那一路是被谁拿走的
    the_action      str    具体是哪一下：journal 那条的 kind；驱动权那一路是 "driver"
    the_phase       str    那会儿会议在哪一阶段
    the_content     str    那句话的正文（没有就是空串）
    the_seq         int    journal 里那一条的序号；驱动权那一路是 0
    from_the_drive  bool   这一下是不是驱动这场会的那一趟自己做的
    the_meeting_id  str    这场会的会议号

它答什么：

* 答一句话：空串是"这一下不算伸手"，别的都是"有人伸手了"，那句话就是停会的理由；
* 答 `bool`：`true` 是伸手（理由是"哪一路、谁、干了什么"那一句），`false` 是没伸手；
* 答 `nil`：这一下不管。

一份最短的规则长这样（"只有驱动自己做的才算，别的都算"）：

    __decl__ =
        bool
      <- $env Env

    args = __get_args__ << __decl__

    from_the_drive =
        bool
      <- $env Env

    __impl__ = builtin.not
      << $env args.env
      << $x (from_the_drive << $env args.env)

不给 `--no-interference-rule-path` 就用内置的那一条，内容和上面这段一样（`rb.DEFAULT_RULE`）。
规则读不起来（编译不过）就不开会；跑起来之后规则自己出错，算"这一下有人伸手"，停会，理由里带着
那个错。

## 5. 哪几下算伸手

驱动这一趟自己做的事不算。除此之外，这几路里出现一下就算：

1. **journal 里多了一条不是这一趟写的**。驱动这一趟写的每一条都在一本账上（`MeetingStore` 落
   journal 的那一处记了序号），不在这本账上的序号，就是别的进程写进来的。
2. **人说的话**。`kind` 是 `chat`、`actor` 是 `human` 的那一条，不管它是哪个进程写进来的，都算
   人伸了手。
3. **驱动权被别人拿走了**。会议目录里的 `driver.json` 写着别人的进程、那个人还活着、心跳还新，
   就是被拿走了。

人是从哪里说的？从 `rb web` 那一台。那一台会在同一份 journal 上写一条人说的话（第 2 条），也会
在写完之后才去要驱动权——那时候驱动权在这一趟手上，它要不到，所以它驱动不起来这一场会；但那条话
已经落在盘上，这一趟看得见，当场停会。伸手这回事也写进这场会的 journal（一条 `state` 事件，
`status` 是 `interfered`），所以跑完之后在 `rb web` 上也看得见为什么停的。

伸手之后做的事：先把停止请求写进这场会（`service.stop`：写 `stop_request`、打断那一轮、把状态记成
`interrupted`、把 tmux 会话收掉）；在做的那一趟收尾时可能把阶段写回去，所以等它利索收完（最多 15
秒）再停一次，盘上留下的就是"停了"。停不下来就硬停（`force=True`）。这一趟的退出码是 4。

## 6. 跑到什么时候

* 建会：`service.start({"message": 那句话, …})`。要说的话由规范的受理决定它是不是一件活：是就发单，
  不是就照旧只是跟 agent 说话。
* 接会（`--resume`）：先按那场会的数据目录认出工作区和会议号；给了新的一句话就再说一句，没给就接着
  跑没跑完的那几趟。
* 停下：会议的状态到了 `completed` / `failed` / `interrupted`，或者这一场自己的活都干完了、
  阶段不在"刚建起来"那几个（`created` / `starting`）上。建会之后 60 秒还没有任何活起来，就当这场会
  没开成（退出码 2）。
* 收摊：把这一趟取出来的规范临时目录、规则那份临时目录删掉。

退出码：`0` 跑完、`1` 没给子命令（印帮助）、`2` 参数不对或起不来、`3` 流程那一关没过（`mgmt_critic` 说这一场没走完，它点出的问题印在标准错误上）、`4` 有人伸手停会了。

## 7. 记下来的东西

会议的数据目录里多一份 `rb.json`：这场会叫什么、工作区在哪儿、驱动它的进程号、开始和结束的
时刻、这一趟读的规范目录和版本、干涉规则是哪儿的哪一版、以及谁伸了手（`what` / `actor` /
`action` / `content` / `seq` / 规则给的理由）。会议自己的数据一个字都不动：这一份是新加的旁注。

## 8. 还管不住什么

* **冒充会里的成员**。只看服务那一侧分不出"这一句是成员派活"还是别人冒充的：那边认的是"发话的
  人名在成员表里、而且不是人"。能拦住的是人自己说话（第 5 节第 2 条）、别的进程写 journal
  （第 1 条）、驱动权被拿走（第 3 条）。
* **直接改会议目录里的文件**（`store/case/…`、`tickets/…`）。这一趟看的是 journal 和驱动权，
  不改 journal 的改动看不见。
* **时间**。看是每 0.2 秒一轮；两步之间隔着不到一轮的伸手，可能已经写进去了才被看见 —— 看见就停，
  但不保证没落地。

## 9. 测试

`tests/test_rb.py`（22 条，`python -m pytest tests/test_rb.py`）：

* `rb` 这一层：不给子命令印帮助、`-V`、子命令各自的参数；
* `rb cli` 的参数：那句话怎么拼、`-` 读标准输入、`--harness` 不认的名字、只给提交不给仓库、规则
  编译不过、`--resume` 只认会议的数据目录；
* 那一行输出：正常跑完 stdout 只有 `rb web --workspace …`（路径有空格会加引号）；`--json` 时是
  一条 `{"type":"web",…}`；那一场真做了活（活答在 `store/case/quicksort/drafted/do_step.viba` 里）；
  `rb.json` 记着规范目录和规则来处；
* 钉版本：临时建一份真 git 仓库，提交 A 是好规范、工作树改成坏规范（受理答不出话），
  `--norm-commit-id A` 的那一趟照旧做得成活，不钉版本的那一趟派不下活；规则那一份用 `read_rule`
  直接比 A 的字节与工作树的字节；
* 伸手那几路：别人写进 journal、人从这一趟自己这个进程说了一句、驱动权被拿走；这一趟自己的活
  （发单、工单派活、成员答话）一条都不算；只读地看（快照、事件）不算；
* 规则说了算：换一份"谁说都不算伸手"的规则，人说了话也不停；
* `rb web`：印出链接；另起一个**真进程**跑它，一个已经跑完的会在列表里、首页拿得到；
* 整趟：`rb cli` 在跑、另一个进程的 `rb web` 上人说了一句，这一趟停下来、退出码 4、会议状态
  `interrupted`、`rb.json` 里记着那一下，而且停完之后 `rb web` 上照样看得见这一场；
* 接会：`--resume` 接着跑的是同一场会，新说的话进了 journal。

`rb cli` 那一侧用假终端加一张脚本化的能力表（和 `tests/test_web_http.py` 用的是同一套安排），
不叫真模型；`rb web` 那两处起的是真进程、真服务。
