# RemoteBuddy 服务层概念 ADT

> SERVICE_API_ADT.md 中接口涉及的类型与派生行为。
> 已定义于 MEETING_ADT.md：UserName / AgentRole / StreamChunk /
> ContentReference / ScoreTag / MeetingData

Caller :=
    DataClass
  * $user_name UserName                  # "$human" 或 agent 名
  * $role Optional[AgentRole]            # 人类取 void
  * $token str

Capability :=
    Oneof
  | $say                                 # 说话
  | $score                               # 评分 / 审核
  | $terminal_take                       # 拿终端
  | $terminal_input                      # 敲键盘
  | $session_lifecycle                   # 暂停 / 继续 / 结束 / 清理
  | $fork                                # 从某条记录分叉重放

StreamKind :=
    Oneof
  | $screen
  | $chat
  | $score
  | $keystroke
  | $terminal_control

EventMode :=
    Oneof
  | $all                                 # 全量帧（网页）
  | $latest                              # 只取最新一张（飞书）

EndAction :=
    Oneof
  | $stop
  | $complete
  | $cleanup

IntentStatus :=
    Oneof
  | $received
  | $rejected

BackendChoice :=
    Oneof
  | $claude_code
  | $trae_agent
  | $codex_app_server
  | $responses

Phase :=
    Oneof
  | $created
  | $starting
  | $agent_thinking
  | $agent_acting
  | $waiting_for_user
  | $completion_proposed
  | $human_control
  | $api_error
  | $cycle_timeout
  | $completed
  | $failed
  | $interrupted

CreateSession :=
    DataClass
  * $message str
  * $participants dict[str, AgentRole]   # 缺省 {actor: $generator, critic: $passive_discriminator}
  * $backend BackendChoice
  * $model Optional[str]                 # void = 后端默认
  * Assert[{ ∃! name : $participants[name] == $generator }]

SessionHandle :=
    DataClass
  * $meeting_uid str
  * $participants dict[str, AgentRole]
  * $phase Phase
  * $cursor int                          # 事件尾 seq
  * $capabilities list[Capability]       # = compute_capabilities(caller, meeting_uid)

EventPage :=
    DataClass
  * $chunks list[StreamChunk]
  * $next_cursor int
  * Assert[{ chunks 内 seq 严格递增；$next_cursor ≥ 请求的 after }]
  * Assert[{ chunks ⊆ MeetingData 的记录，不改写、不聚合、不重排 }]

Blob :=
    DataClass
  * $digest str
  * $bytes bytes
  * Assert[{ $digest == hash($bytes) }]
  * Hint[{ 截屏帧等大对象只走 read_blob；事件里只放 digest }]

SayIntent :=
    DataClass
  * $content str
  * $references list[ContentReference]

ScoreIntent :=
    DataClass
  * $tag ScoreTag
  * $reference ContentReference
  * $comment Optional[str]

TerminalInput :=
    DataClass
  * $bytes bytes

Intent :=
    Oneof
  | $say SayIntent
  | $score ScoreIntent
  | $take_terminal
  | $release_terminal
  | $terminal_input TerminalInput
  | $resume_actor                       # 让 actor 继续干活
  | $end EndAction

IntentEnvelope :=
    DataClass
  * $idempotency_key str
  * $intent Intent
  * Assert[{ $idempotency_key ≠ "" }]

IntentAck :=
    DataClass
  * $seq int
  * $status IntentStatus
  * $reason Optional[str]
  * Assert[{ $status == $rejected ⇒ $reason ≠ void }]

ForkSession :=
    DataClass
  * $from_meeting str
  * $from_seq int
  * $overrides dict[str, str]            # 提示词 / persona / 策略版本
  * Assert[{ $from_seq ≤ 源会议的事件尾 seq }]

# ─── 派生行为 ─────────────────────────────────────────────────────────

lookup_role :=
    Optional[AgentRole]
    <- $meeting_uid str
    <- $user_name UserName
    <- Appendix[{
         人类取 void；agent 取本会 agent_name2role 中的角色
       }]

can_drive_terminal :=
    bool
    <- $meeting_uid str
    <- $u UserName
    <- Appendix[{
         terminal_capable := { $human () } ∪ { a | lookup_role(meeting_uid, a) == $generator }
       }]
    <- Assert[{ can_drive_terminal ⇔ u ∈ terminal_capable }]

compute_capabilities :=
    list[Capability]
    <- $caller Caller
    <- $meeting_uid str
    <- Assert[{ $terminal_take ∈ compute_capabilities ⇔ can_drive_terminal(meeting_uid, caller.user_name) }]
