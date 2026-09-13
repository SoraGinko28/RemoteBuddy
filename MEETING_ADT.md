# Meeting ADT（定稿存档）

> 本文档是 RemoteBuddy 会议模型的形式化定稿（用户原稿，随讨论同步）。
> ADT 只描述系统核心逻辑（能力层）；Workspace 存数据、Tree 记血缘、
> Image 是血统快照、Instance 是活线、MeetingData 是可被引用/审核/回放的事实载体；
> 复制/快照/去重等属数据层，不在此讨论。

---

## 记录层

```
AgentRole :=
    Oneof
  | $generator                   # 有tmux权限
  | $active_discriminator        # 随时评论
  | $passive_discriminator       # 有召才评论

MeetingData :=
    DataClass
  * $meeting_uid str     # basically uuid
  * $meeting_name str
  * $sorted_stream_chunks list[StreamChunkDict]
  * $agent_name2role dict[str, AgentRole]        # 会内只增不减：join 时加入，冻结后固化
  * Appendix[{
      user_names := 从各 chunk 收集所有出现的 UserName
                     （screen.owner / chat.user_name / score.judge_name /
                      terminal_control.user_name）
    }]
  * Assert[{ keys($agent_name2role) ⊆ user_names(本记录) }]
  * Hint[{
      回应提醒：∀ chat 消息 m ∈ 聊天流，若 m 含 $at_user_name == P
                且 role(P) == $passive_discriminator，
                则 ∃ 后续记录 r（P 产出的 chat 或 score）
                  : r 含可解析到 m 的 ContentReference
    }]
  * Appendix[{
      owner_at(t) := 含 t 的控制区间的 TerminalControl.user_name
                     （区间由 TerminalControl.start_time 界定；可能缺省 = 此刻无人控制）
    }]

StreamChunkDict :=
    DataClass
  * $stream_name2stream_chunk dict[str, StreamChunk]
  * $start datetime
  * $end Optional[datetime]

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
  * $owner_user_name UserName

ChatStreamChunk :=
    DataClass
  * $stream_name str
  * $chunks list[ChatMessage]
  * Appendix[{
      聊天区展示 := 按时间归并本会议的 chat 流记录与 terminal_control 流记录
                    （展示层视图；每条展示项仍归属自己那条流）
    }]

ScoreStreamChunk :=
    DataClass
  * $chunks list[ScoreMessage]

TerminalControlStreamChunk :=
    DataClass
  * $chunks list[TerminalControl]
  * Appendix[{
      聊天区展示 ∋ 本流记录
          # 以居中的状态行展示：它不是谁发的言，没有作者，行内写明该记录的 $user_name
          # 「接管」与「交还」不是记录自带的，而是相邻两条记录之间的关系：
          #   本条 $user_name 与上一条不同，才谈得上接管或交还
    }]

UserName :=
    Oneof
  | $agent_name str
  | $human ()

TerminalControl :=
    DataClass
  * $tmux_window_name str
  * $tmux_pane int
  * $user_name Optional[UserName]
  * $start_time datetime
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
  * $terminal_text_content list[TerminalTextSegment]
  * $cursor_x int                          # 格坐标（0 起）；与字符坐标的换算由渲染层按 wcwidth 折算
  * $cursor_y int
  * $screen_width int
  * $screen_height int
  * $captured_at datetime                  # microsecond
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

TerminalTextSegment :=
    DataClass
  * $text str                          # 连续同属性文本；可含 \n（跨行背景色不断开）
  * $fg Optional[Color]                # None = 终端默认前景
  * $bg Optional[Color]                # None = 终端默认背景
  * $styles set[Style]                 # 空 = 无附加样式；语义是集合，顺序无关

Color :=
    Oneof
  | $palette int                       # SGR 调色板下标 0..255
  | $truecolor Rgb

Rgb :=
    DataClass
  * $r int                             # 0..255
  * $g int
  * $b int

Style :=
    Oneof
  | "bold"
  | "underline"
  | "italic"
  | "reverse"
  | "blink"
  | "dim"

ChatMessage :=
    DataClass
  * $message list[$raw_msg str | $at_user_name UserName | $reference ContentReference]
  * $user_name UserName
  * $time datetime

ScoreMessage :=
    DataClass
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
  | $screen_ref ScreenReference
  | $chat_ref ChatReference
  | $score_ref ScoreReference
  | $terminal_control_ref TerminalControlReference
  * Assert[{
      与 StreamChunk 一一对应：每种流的记录恰有一个 reference 变体
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

ScreenReference :=
    DataClass
  * $capture_time datetime
  * $stream_name str
  * $region TextRegion
  * Appendix[{
        resolve($screen_ref) := $stream_name 流中满足
            (ScreenCapture.captured_at == $screen_ref.capture_time) 的记录
    }]

ChatReference :=
    DataClass
  * $msg_time datetime
  * $user_name UserName
  * $region TextRegion
  * Appendix[{
        resolve($chat_ref) := 聊天流中满足
            (ChatMessage.time == $chat_ref.msg_time
             ∧ ChatMessage.user_name == $chat_ref.user_name) 的记录
    }]

ScoreReference :=
    DataClass
  * $judge_time datetime
  * $judge_name UserName
  * Appendix[{
        resolve($score_ref) := 评分流中满足
            (ScoreMessage.judge_name == $score_ref.judge_name
             ∧ ScoreMessage.time == $score_ref.judge_time) 的记录
    }]

TerminalControlReference :=
    DataClass
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

TextRegion :=
    DataClass
  * $start int
  * $end int
```

---

## Harness 内部（不进入 MeetingData）

```
# 这一节的概念只约束 RemoteBuddy Harness 自身的实现，
# 它们记录的信息一律不写入存储系统，也不属于任何一条流。
Keystroke :=
    DataClass
  * $data KeystrokeStr
  * $user_name UserName
  * $stroke_time datetime
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

```
Error := Oneof | ...      # designed by Agent

Result[T] =
    Oneof
  | $data T
  | $error Error

LazyEvaluated[T] :=
    Object
  * $cached Optional[T]
  * $getter (Result[T] <- $ctx Any)

Tree[T] :=
    Object
  * $data T
  * $parent LazyEvaluated[Optional[Tree[T]]]
  * $children list[LazyEvaluated[Tree[T]]]
```

---

## 对象层（去中心化协作 / 血缘 / 镜像 / 实例）

```
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
  * $agent_tree_node Optional[ReadOnly[AgentTreeNode]]
  * Assert[{ $service_provider interprets $running_env.running_env_uid,
             $configuration.configuration_uid, and $agent_tree_node.uid }]

MeetingPrototypeImage :=
    Object
  * $service_provider MeetingServiceProvider
  * $running_env ReadOnly[MeetingRunningEnvironment]
  * $configuration ReadOnly[MeetingConfiguration]
  * $meeting_tree_node Optional[ReadOnly[MeetingTreeNode]]
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

```
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
    <- $role AgentRole
```
