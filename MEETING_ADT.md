# Meeting ADT（定稿存档）

> 本文档是 RemoteBuddy 会议模型的形式化定稿（用户原稿，随讨论同步）。
> ADT 只描述系统核心逻辑（能力层）；Workspace 存数据、Tree 记血缘、
> Image 是血统快照、Instance 是活线、MeetingData 是可被引用/审核/回放的事实载体；
> 复制/快照/去重等属数据层，不在此讨论。

---

## 记录层

会议记录本体。下面的类型、断言与说明都在这一层。

```bash
import meeting_core as mc

MeetingData :=
    DataClass
  * $meeting_uid str     # basically uuid
  * $meeting_name str
  * $sorted_stream_chunks list[StreamChunkDict]
  * $agent_name2role dict[str, mc.AgentRole]        # 会内只增不减：join 时加入，冻结后固化
  * $file_kv_store mc.FileKvStore
  * $file_desktop FileDesktop
  * Appendix[{
      user_names := 从各 chunk 收集所有出现的 mc.UserName
                     （screen.owner / chat.user_name / score.judge_name /
                      terminal_control.user_name）
                   ∪ 文件层所有 mc.Versioned.author
                     （file_kv_store 的三条链 / file_desktop 的 $reorder_indexes）
    }]
  * Assert[{ keys($agent_name2role) ⊆ user_names(本记录) }]
  * Hint[{
      回应提醒：∀ chat 消息 m ∈ 聊天流，若 m 含 $at_user_name == P
                且 role(P) == $passive_discriminator，
                则 ∃ 后续记录 r（P 产出的 chat 或 score）
                  : r 含可解析到 m 的 mc.ContentReference
    }]
  * Appendix[{
      owner_at(t) := 含 t 的控制区间的 TerminalControl.user_name
                     （区间由 TerminalControl.start_time 界定；可能缺省 = 此刻无人控制）
    }]

FileDesktop :=
    DataClass
  * $workspaces mc.AppendOnlyList[FileWorkspace]
  * $reorder_indexes mc.Versioned[DesktopMemberOrder]
  * Assert[{
      同一层里名字唯一，路径才定得到一条：
        $workspaces 里各 workspace 的 $workspace_name 两两不同；
        同一个 workspace 里，fold 的 $fold_name 与直接挂着的文件的 $entry_name 两两不同；
        同一个 fold 里，$member_file_ids 里各文件的 $entry_name 两两不同
    }]
  * Appendix[{
      # 桌面树只表示当前状态：没有任何 mc.ContentReference 按 $version_time 引用它，
      # 所以树里不铺版本链（要 as-of 的只有 file_kv_store 那三条链）；
      # $reorder_indexes 记的是最后一次重排的时间与作者
    }]

DesktopMemberOrder :=
    DataClass
  * $workspace_indexes mc.ReadOnlyList[WorkspaceMemberOrder]
  * Assert[{
      $workspace_indexes 里的 $workspace_index 恰好是
          0 .. len(MeetingData.file_desktop.workspaces) - 1 的一个排列
    }]

WorkspaceMemberOrder :=
    DataClass
  * $workspace_index int
  * $file_indexes mc.ReadOnlyList[($file_index int, mc.Optional[FoldMemberOrder])]
  * Assert[{
      ws := MeetingData.file_desktop.workspaces[$workspace_index]
      { $file_index | ($file_index, _) ∈ $file_indexes } == 0 .. len(ws.member_files) - 1
      且 对每个 ($file_index, Some(fo))：
          fo.$member_indexes == 0 .. len((ws.member_files[$file_index] as $fold).member_file_ids) - 1
    }]

FoldMemberOrder :=
    DataClass
  * $member_indexes mc.ReadOnlyList[$member_index int]
  # 本对象只出现在某个 ($file_index, Some(本对象)) 里；
  # 由所在的 WorkspaceMemberOrder 断言它是 ws.member_files[$file_index] 这个 fold 的成员顺序

FileWorkspace :=
    DataClass
  * $member_files mc.AppendOnlyList[FileFold | mc.FileId]
  * $workspace_name mc.Optional[str]
  * $workspace_status mc.Optional[mc.DataStatus]

FileFold :=
    DataClass
  * $fold_name mc.Optional[str]
  * $member_file_ids mc.AppendOnlyList[mc.FileId]

StreamChunkDict :=
    DataClass
  * $stream_name2stream_chunk dict[str, StreamChunk]
  * $start mc.datetime
  * $end mc.Optional[mc.datetime]

StreamChunk :=
    Oneof
  | $screen_stream_chunk ScreenStreamChunk
  | $chat_stream_chunk ChatStreamChunk
  | $score_stream_chunk ScoreStreamChunk
  | $terminal_control_stream_chunk TerminalControlStreamChunk

ScreenStreamChunk :=
    DataClass
  * $chunks list[ScreenCapture]
  * $stream_name str
  * $owner_user_name mc.UserName

ChatStreamChunk :=
    DataClass
  * $stream_name str
  * $chunks list[mc.ChatMessage]
  * Appendix[{
      聊天区展示 := 按时间归并本会议的 chat 流记录与 terminal_control 流记录
                    （展示层视图；每条展示项仍归属自己那条流）
    }]

ScoreStreamChunk :=
    DataClass
  * $chunks list[mc.ScoreMessage]

TerminalControlStreamChunk :=
    DataClass
  * $chunks list[TerminalControl]
  * Appendix[{
      聊天区展示 ∋ 本流记录
          # 以居中的状态行展示：它不是谁发的言，没有作者，行内写明该记录的 $user_name
          # 「接管」与「交还」不是记录自带的，而是相邻两条记录之间的关系：
          #   本条 $user_name 与上一条不同，才谈得上接管或交还
    }]

TerminalControl :=
    DataClass
  * $tmux_window_name str
  * $tmux_pane int
  * $user_name mc.Optional[mc.UserName]
  * $start_time mc.datetime
  * Appendix[{
      # 缺省 $user_name = 从 start_time 起无人控制：人交还了，且没有 agent 接手
      # 每次接管、每次交还都写入一条记录，没有例外
      user_name 缺省，或 user_name ∈ participants(user_names(MeetingData))
    }]
  * Appendix[{
      terminal_capable := { $human () } ∪ { a | role(a) == $generator }   # role 查所在会议的 agent_name2role
    }]
  * Assert[{
        user_name 缺省，或
        user_name == $human () 或
        user_name 的 agent 的 role == $generator
    }]

ScreenCapture :=
    DataClass
  * $screen_data str                       # 纯文本（模型/引用/评分坐标）；逐行右去尾部空格
  * $terminal_text_content list[mc.TerminalTextSegment]
  * $cursor_x int                          # 格坐标（0 起）；与字符坐标的换算由渲染层按 wcwidth 折算
  * $cursor_y int
  * $screen_width int
  * $screen_height int
  * $captured_at mc.datetime                  # microsecond
  * Assert[{ plain_of($terminal_text_content) == $screen_data }]
  * Assert[{ len(frame_rows($screen_data)) == $screen_height }]
  * Assert[{ all(cell_width(r) <= $screen_width for r in frame_rows(concat_text_field_of_list($terminal_text_content))) }]
  * Assert[{ all(len(seg.text) > 0 for seg in $terminal_text_content) }]
  * Assert[{ 0 <= $cursor_y < $screen_height and 0 <= $cursor_x < $screen_width }]
  * Assert[{ ∀ i: seg_attrs($terminal_text_content[i]) != seg_attrs($terminal_text_content[i+1]) }]
  * Appendix[{
      frame_rows(s)      := s.split('\n')                       # 行 = 右去尾空格后的内容行；无尾随空行
      concat_text_field_of_list(list) := ''.join(seg.text for seg in list)
      row_plain(row)     := row.rstrip(' ')
      plain_of(list)     := '\n'.join(row_plain(r) for r in frame_rows(concat_text_field_of_list(list)))
      cell_width(row)    := sum(wcwidth(ch) for ch in row)      # CJK 占 2 格
      seg_attrs(seg)     := (seg.fg, seg.bg, frozenset(seg.styles))
    }]
```

---

## 稳定类型 meeting_core.viba

小的、稳定的类型集中在这一章：时间、身份、版本、文本区间、颜色、引用、聊天与评分记录。
它们不随研发变化，后面几章的记录都建立在这一章上。

```bash
# ---------------------------------------------------------------- #
# 1. 时间、可选值、只增列表、只读列表
# ---------------------------------------------------------------- #

datetime :=
    Object
  * $microsecond int                    # 会议里的时间身份：微秒，构造上唯一

Optional[T] := T | nil                  # 缺席就是 nil；不是 tag，是和的形状

AppendOnlyList[T] := list[T]            # 只增：位置即身份。语义是约定，不是结构
ReadOnlyList[T] := list[T]              # 只读：写一次不再改。同上

Result[T] :=
    Oneof
  | $ok T                               # 成功：装值
  | $err str                            # 失败：装一句话

# ---------------------------------------------------------------- #
# 2. 小的稳定类型
# ---------------------------------------------------------------- #

AgentRole :=
    Oneof
  | $generator ()                       # 有 tmux 权限
  | $active_discriminator ()            # 随时评论
  | $passive_discriminator ()           # 有召才评论

UserName :=
    Oneof
  | $agent_name str
  | $human ()

DataStatus :=
    Oneof
  | $deleted ()
  | $read_only ()

Versioned[T] :=
    Object
  * $data T
  * $time datetime
  * $author UserName

TextRegion :=
    Object
  * $start int
  * $end int

Rgb :=
    Object
  * $r int                              # 0..255
  * $g int
  * $b int

Color :=
    Oneof
  | $palette int                        # SGR 调色板下标 0..255
  | $truecolor Rgb

Style :=
    Oneof
  | "bold"
  | "underline"
  | "italic"
  | "reverse"
  | "blink"
  | "dim"

TerminalTextSegment :=
    Object
  * $text str                           # 连续同属性文本；可含 \n（跨行背景色不断开）
  * $fg Optional[Color]                 # nil = 终端默认前景
  * $bg Optional[Color]                 # nil = 终端默认背景
  * $styles set[Style]                  # 空 = 无附加样式；语义是集合，顺序无关

# 取数接口交出来的那一份状态：屏幕一份、文件一份。

ScreenContent :=
    Object
  * $text str
  * $segments list[TerminalTextSegment]
  * $cursor_x int
  * $cursor_y int
  * $width int
  * $height int
  * $captured_at datetime

FileState :=
    Object
  * $name str
  * $meta DataStatus
  * $content str

# ---------------------------------------------------------------- #
# 3. 引用、聊天与评分记录
# ---------------------------------------------------------------- #

FileId :=
    Object
  * $file_id str
  * $entry_name str
  * Assert[{ $file_id ∈ keys(FileKvStore.file_id2file) }]

VersionedFile :=
    Object
  * $file_name Versioned[str]
  * $file_meta Versioned[DataStatus]
  * $file_content Versioned[str]

FileKvStore :=
    Object
  * $file_id2file dict[str, VersionedFile]

ChatMessage :=
    Object
  * $message list[$raw_msg str | $at_user_name UserName | $reference ContentReference]
  * $user_name UserName
  * $time datetime

ScoreMessage :=
    Object
  * $tag str
  * $score Optional[float]
  * $comment Optional[str]
  * $judge_name UserName
  * $time datetime
  * $reference ContentReference
  * Appendix[{
      summon_chained := role($judge_name) != $passive_discriminator
                        ∨ 该消息的 $reference 可解析到一条召唤它的消息
    }]
  * Assert[{ summon_chained }]

ContentReference :=
    Oneof
  * Assert[{
      与 StreamChunk 一一对应：
        screen_stream_chunk            ↔ screen_ref
        chat_stream_chunk              ↔ chat_ref
        score_stream_chunk             ↔ score_ref
        terminal_control_stream_chunk  ↔ terminal_control_ref
    }]
  * Hint[{
      区间不是一种引用：引用区间的起止记录（及其间的若干条）即可表达一段；
      要在别处反复指这段，引用那条承载这些引用的消息即可——
      引用指向的记录本身也可以是一条带引用的消息
    }]
  | $screen_ref ScreenReference
  | $chat_ref ChatReference
  | $score_ref ScoreReference
  | $terminal_control_ref TerminalControlReference
  | $versioned_file_ref VersionedFileReference
  | $versioned_file_content_ref VersionedFileContentReference

VersionedFileReference :=
    Object
  * $file_id FileId
  * $version_time datetime
  * Appendix[{
      # as-of：取该时刻（含）之前的最近一版
      resolve($ref) := $file_id 的版本链中满足
          (Versioned.time ≤ $ref.version_time) 的最后一版
      # name / meta / content 是三条独立版本链，各自按同一规则取；
      # content 链此刻为空（该时刻之前还没有任何一版）→ 本条无解
    }]

VersionedFileContentReference :=
    Object
  * $versioned_file_ref VersionedFileReference
  * $region TextRegion
  * Appendix[{
      resolve($ref) := resolve($ref.$versioned_file_ref) 的 content 上取 $region 那一段
    }]

ScreenReference :=
    Object
  * $capture_time datetime
  * $stream_name str
  * $region TextRegion
  * Appendix[{
        resolve($screen_ref) := $stream_name 流中满足
            (ScreenCapture.captured_at == $screen_ref.capture_time) 的记录
    }]

ChatReference :=
    Object
  * $msg_time datetime
  * $user_name UserName
  * $region TextRegion
  * Appendix[{
        resolve($chat_ref) := 聊天流中满足
            (ChatMessage.time == $chat_ref.msg_time
             ∧ ChatMessage.user_name == $chat_ref.user_name) 的记录
    }]

ScoreReference :=
    Object
  * $judge_time datetime
  * $judge_name UserName
  * Appendix[{
        resolve($score_ref) := 评分流中满足
            (ScoreMessage.judge_name == $score_ref.judge_name
             ∧ ScoreMessage.time == $score_ref.judge_time) 的记录
    }]

TerminalControlReference :=
    Object
  * $start_time datetime
  * $user_name Optional[UserName]
  * $region TextRegion
  * Appendix[{
        resolve($ctrl_ref) := 控制流中满足
            (TerminalControl.start_time == $ctrl_ref.start_time
             ∧ TerminalControl.user_name == $ctrl_ref.user_name) 的记录
    }]
  * Hint[{
      指向的是一次控制区间（起点由 TerminalControl.start_time 界定，
      终点由下一次控制记录的 start_time 界定）；
      区间内没有可切分的原文，所以 region 恒为整行
    }]

# ---------------------------------------------------------------- #
# 完备性自检（在 viba 子模块里跑）：
#
#   from viba.is_complete import is_complete
#   from pathlib import Path
#   stops = {"Assert", "Appendix", "Hint"}
#   print(is_complete(Path("meeting_core.viba").read_text(),
#                     viba_paths=["."], terminators=stops))    # True
#
# Assert / Appendix / Hint 是三个说明用的词，三个都必需：
# FileId、ContentReference、ScoreMessage 身上带着它们。
# ---------------------------------------------------------------- #

```

这一章是完备的：拿 viba 自带的 `is_complete` 验，terminators 只需要
`Assert` / `Appendix` / `Hint` 三个说明用的词，别的名字全都解析得到。

---

## 取数据接口 meeting_sdk.viba

从 meeting 里取数的只读接口：只有签名，没有实现，也没有写入。
每个函数都带泛型参数 Ctx，并吃一个 $ctx Ctx —— 具体是什么由实现方绑
（会议句柄、连接、快照都行），接口本身不认识它；签名里只有稳定类型。

```bash
import meeting_core as mc

# ---------------------------------------------------------------- #
# 时间：时间就是身份
# ---------------------------------------------------------------- #

chunk_times[Ctx] :=
    mc.Result[list[mc.datetime]]               # 这段时间里每条 chunk 的时刻
  <- $ctx Ctx
  <- $start mc.datetime
  <- $end mc.datetime

streams_between[Ctx] :=
    mc.Result[list[str]]                       # 这段时间里出现过的流名
  <- $ctx Ctx
  <- $start mc.datetime
  <- $end mc.datetime

# ---------------------------------------------------------------- #
# 聊天与评分
# ---------------------------------------------------------------- #

chat_messages[Ctx] :=
    mc.Result[list[mc.ChatMessage]]
  <- $ctx Ctx
  <- $start mc.datetime
  <- $end mc.datetime

score_messages[Ctx] :=
    mc.Result[list[mc.ScoreMessage]]
  <- $ctx Ctx
  <- $start mc.datetime
  <- $end mc.datetime

chat_message_at[Ctx] :=
    mc.Result[mc.ChatMessage]                  # 一个人在一个时刻只有一条
  <- $ctx Ctx
  <- $time mc.datetime
  <- $user mc.UserName

resolve_chat_reference[Ctx] :=
    mc.Result[mc.ChatMessage]
  <- $ctx Ctx
  <- $ref mc.ChatReference

resolve_chat_content_reference[Ctx] :=
    mc.Result[str]                             # 引用那一段的原文
  <- $ctx Ctx
  <- $ref mc.ChatReference

resolve_score_reference[Ctx] :=
    mc.Result[mc.ScoreMessage]
  <- $ctx Ctx
  <- $ref mc.ScoreReference

chat_message_references[Ctx] :=
    mc.Result[list[mc.ContentReference]]       # 这条消息带的引用，按出现顺序
  <- $ctx Ctx
  <- $time mc.datetime
  <- $user mc.UserName

score_message_reference[Ctx] :=
    mc.Result[mc.ContentReference]             # 这条评分带的引用
  <- $ctx Ctx
  <- $time mc.datetime
  <- $judge mc.UserName

first_chunk_time[Ctx] :=
    mc.Result[mc.datetime]                     # 会议里第一条 chunk 的时刻
  <- $ctx Ctx

last_chunk_time[Ctx] :=
    mc.Result[mc.datetime]                     # 最后一条
  <- $ctx Ctx

# ---------------------------------------------------------------- #
# 屏幕：按时间找、按引用找，都给那一屏的内容，不给抓屏记录本身
# ---------------------------------------------------------------- #
# 断言与说明留在记录层，这里只把那一屏的值交出来。

screen_at[Ctx] :=
    mc.Result[mc.ScreenContent]                # 这一时刻那一屏的内容
  <- $ctx Ctx
  <- $time mc.datetime

resolve_screen_reference[Ctx] :=
    mc.Result[mc.ScreenContent]
  <- $ctx Ctx
  <- $ref mc.ScreenReference

# ---------------------------------------------------------------- #
# 人：参会的人，和某一刻谁拿着终端
# ---------------------------------------------------------------- #

participants[Ctx] :=
    mc.Result[list[mc.UserName]]
  <- $ctx Ctx

roles[Ctx] :=
    mc.Result[dict[str, mc.AgentRole]]
  <- $ctx Ctx

owner_at[Ctx] :=
    mc.Result[(mc.UserName | nil)]             # nil = 此刻无人控制
  <- $ctx Ctx
  <- $time mc.datetime

control_owner_of[Ctx] :=
    mc.Result[(mc.UserName | nil)]             # nil = 那一次交还之后没人接手
  <- $ctx Ctx
  <- $ref mc.TerminalControlReference

# ---------------------------------------------------------------- #
# 文件层
# ---------------------------------------------------------------- #
# VersionedFile 是整份记录：名字 / 状态 / 内容各自的历史都在里面。
# 问「某一时刻」或「最新」给的是那一份状态，不是整份记录。

# 按路径取 FileId：$path 要么 /workspace_name/entry_name，
# 要么 /workspace_name/fold_name/entry_name。每层名字唯一（见 FileDesktop 的 Assert），
# 所以路径定得到一条。

file_id_by_path[Ctx] :=
    mc.Result[mc.FileId]
  <- $ctx Ctx
  <- $path str

file_kv_store[Ctx] :=
    mc.Result[mc.FileKvStore]
  <- $ctx Ctx

files_at[Ctx] :=
    mc.Result[dict[str, mc.FileState]]
  <- $ctx Ctx
  <- $time mc.datetime

latest_file[Ctx] :=
    mc.Result[mc.FileState]                    # 最新的那一份状态
  <- $ctx Ctx
  <- $file mc.FileId

resolve_versioned_file_reference[Ctx] :=
    mc.Result[mc.FileState]                    # 引用那一刻的文件状态
  <- $ctx Ctx
  <- $ref mc.VersionedFileReference

resolve_versioned_file_content_reference[Ctx] :=
    mc.Result[str]                             # 引用那一段的原文
  <- $ctx Ctx
  <- $ref mc.VersionedFileContentReference

```

---

## Harness 内部（不进入 MeetingData）



```bash
# 这一节的概念只约束 RemoteBuddy Harness 自身的实现，
# 它们记录的信息一律不写入存储系统，也不属于任何一条流。
Keystroke :=
    DataClass
  * $data KeystrokeStr
  * $user_name mc.UserName
  * $stroke_time mc.datetime
  * Appendix[{
      owner_at(t) := 见 MeetingData 的同名 Appendix
      $user_name 必须等于 owner_at($stroke_time)：
      只有当时的控制者能产生按键；无人控制时不产生按键
    }]

KeystrokeStr :=
    Oneof
  | $text str
  | $ctrl str
  | $rest ($millisecond int)
```

---

## 通用 helper



```bash
LazyEvaluated[T] :=
    Object
  * $cached mc.Optional[T]
  * $getter (Result[T] <- $ctx Any)

Tree[T] :=
    Object
  * $data T
  * $parent LazyEvaluated[mc.Optional[Tree[T]]]
  * $children list[LazyEvaluated[Tree[T]]]
```

---

## 对象层（去中心化协作 / 血缘 / 镜像 / 实例）



```bash
# RemoteBuddy supports decentralized collaboration.
# i.e. Agents may come from different service-provider companies.
ServiceProvider :=
    Object
  * $service_provider_uid UUID[str]
  * ...

# environments dockable like OS/Library/Application and Codex/ClaudeCode/...
RunningEnvironment :=
    Object
  * $running_env_uid UUID[str]
  * ...

Configuration :=
    Object
  * $configuration_uid UUID[str]
  * ...

# context like session/trajectory/meeting_record: mainly time-series data.
ContextData :=
    Object
  * $context_data_uid UUID[str]
  * ...

# abstraction for working directory whose payload is of type T.
Workspace[T] :=
    Object
  * $workspace_uuid UUID[str]
  * ...
  * Appendix[{ the data in workspace is of type T }]

# Composition / inheritance
AgentServiceProvider   := ServiceProvider
AgentRunningEnvironment := RunningEnvironment
AgentConfiguration     := Configuration
AgentContextData       := ContextData
AgentWorkspace         := Workspace[{Any data used by agent}]

MeetingServiceProvider   := ServiceProvider
MeetingRunningEnvironment := RunningEnvironment
MeetingConfiguration     := Configuration
MeetingContextData       := MeetingData
MeetingWorkspace         := Workspace[MeetingData]

# ---- lineage ----
AgentTreeNode :=
    Object
  * $uid UUID[str]
  * $data LazyEvaluated[AgentContextData]    # 从 agent workspace 读取该血缘点记录的句柄

AgentTree := Tree[AgentTreeNode]

MeetingTreeNode :=
    Object
  * $uid UUID[str]
  * $data LazyEvaluated[MeetingContextData]  # 从 meeting workspace 读取该血缘点记录的句柄
  * $agent_name2agent_node dict[str, AgentTreeNode]
  * Assert[{ keys(agent_name2agent_node) are user names in MeetingContextData }]

MeetingTree := Tree[MeetingTreeNode]

# ---- images（不可变血统快照；tree_node = None ⇔ 尚未提交过）----
AgentPrototypeImage :=
    Object
  * $service_provider AgentServiceProvider
  * $running_env ReadOnly[AgentRunningEnvironment]
  * $configuration ReadOnly[AgentConfiguration]
  * $agent_tree_node mc.Optional[ReadOnly[AgentTreeNode]]
  * Assert[{ $service_provider interprets $running_env.running_env_uid,
             $configuration.configuration_uid, and $agent_tree_node.uid }]

MeetingPrototypeImage :=
    Object
  * $service_provider MeetingServiceProvider
  * $running_env ReadOnly[MeetingRunningEnvironment]
  * $configuration ReadOnly[MeetingConfiguration]
  * $meeting_tree_node mc.Optional[ReadOnly[MeetingTreeNode]]
  * $agent_name2agent_prototype_image dict[str, AgentPrototypeImage]
  * Assert[{ $service_provider interprets $running_env.running_env_uid,
             $configuration.configuration_uid, and $meeting_tree_node.uid }]
  * Assert[{ keys($agent_name2agent_prototype_image) are user names in $meeting_tree_node.data }]

# ---- instances（活：必有 image；活尾巴续写在 workspace）----
AgentInstance :=
    Object
  * $agent_prototype_image AgentPrototypeImage
  * $workspace AgentWorkspace
  * Assert[{ $agent_prototype_image.service_provider interprets $workspace.workspace_uid }]

MeetingInstance :=
    Object
  * $agent_prototype_image MeetingPrototypeImage
  * $workspace MeetingWorkspace
  * $agent_name2agent_instance dict[str, AgentInstance]
  * Assert[{ $agent_prototype_image.service_provider interprets $workspace.workspace_uid }]
  * Assert[{ keys($agent_name2agent_instance) are user names in $agent_prototype_image.meeting_tree_node.data }]
  * Assert[{ keys($agent_name2agent_instance) and keys($agent_prototype_image.agent_name2agent_prototype_image) are disjoint }]
```

---

## 关键操作



```bash
GetTreeFromAgentTreeNode := Result[AgentTree] <- AgentTreeNode

InstantiateAgent :=
    Result[AgentInstance]
    <- $service_provider ServiceProvider
    <- $running_env ReadOnly[AgentRunningEnvironment]
    <- $configuration ReadOnly[AgentConfiguration]
    <- $workspace AgentWorkspace
    <- Assert[{ $service_provider interprets $running_env.running_env_uid,
                $configuration.configuration_uid, and $workspace.workspace_uid }]

SpawnAgent :=
    Result[AgentInstance]
    <- $image AgentPrototypeImage
    <- $workspace AgentWorkspace
    <- Assert[{ $image.service_provider interprets $workspace.workspace_uid }]

MakeAgentPrototypeImage := Result[$ret AgentPrototypeImage] <- $self AgentInstance <- Hint[{
    1. (old context data) + (data in workspace) -> (new context data).
    2. create child node in AgentTree
}]

GetTreeFromMeetingTreeNode := Result[MeetingTree] <- $node MeetingTreeNode

InstantiateMeeting :=
    Result[MeetingInstance]
    <- $service_provider MeetingServiceProvider
    <- $running_env ReadOnly[MeetingRunningEnvironment]
    <- $configuration ReadOnly[MeetingConfiguration]
    <- $workspace MeetingWorkspace
    <- Assert[{ $service_provider interprets $running_env.running_env_uid,
                $configuration.configuration_uid, and $workspace.workspace_uid }]

SpawnMeeting :=
    Result[MeetingInstance]
    <- $image MeetingPrototypeImage
    <- $workspace MeetingWorkspace
    <- $agent_name2new_agent_workspace dict[str, AgentWorkspace]
    <- Hint[{
        1. 以 $image.meeting_tree_node.data 为基底恢复会议记录；
        2. 对 $image.agent_name2agent_prototype_image 中每个 agent 调 SpawnAgent，
           生成 $agent_name2agent_instance；
        3. 产出可运行的 MeetingInstance。
        4. image里的Agent的 name 只有出现在$agent_name2new_agent_workspace中，才能复苏。
    }]
    <- Assert[{ keys($agent_name2new_agent_workspace)
                ⊆ keys($image.agent_name2agent_prototype_image) }]
    <- Assert[{ $image.service_provider interprets $workspace.workspace_uid }]

MakeMeetingPrototypeImage := Result[$ret MeetingPrototypeImage] <- $self MeetingInstance <- Hint[{
    1. (old context data) + (data in workspace) -> (new context data).
    2. join Map[MakeAgentPrototypeImage]($agent_name2agent_instance) and $self.agent_prototype_image.agent_name2agent_prototype_image
       into $ret.agent_name2agent_prototype_image.
    3. create child node in MeetingTree.
}]

JoinMeeting :=
    Result[$self MeetingInstance]
    <- $self MeetingInstance
    <- $agent_name str
    <- $agent_instance AgentInstance
    <- $role mc.AgentRole
```
