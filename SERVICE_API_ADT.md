# RemoteBuddy 服务层接口 ADT

> 引擎对外唯一的门：客户端（网页 / CLI / 飞书 / critic / judge）与 harness
> 一律经此读写会议事实，进程内直接 import 引擎模块的用法作废。
> 本文只写接口（行为）；接口涉及的类型见 SERVICE_CONCEPTS_ADT.md。

ServiceAPI :=
    DataClass
  * $endpoints list[str]
  #   ["create_session", "list_sessions", "read_events", "read_blob",
  #    "submit_intent", "fork_session", "end_session"]
  * Assert[{ ∀ 出参：不含 tmux pane_id、进程句柄、API key、模型原始响应 }]
  * Hint[{ 落盘布局（runs/<uid>/meeting.json、streams/events.jsonl）是实现细节，
            换存储不动协议 }]

create_session :=
    SessionHandle
    <- $caller Caller
    <- $request CreateSession
    <- Assert[{ 返回的 $capabilities == compute_capabilities(caller, meeting_uid) }]

list_sessions :=
    list[SessionHandle]
    <- $caller Caller

read_events :=
    EventPage
    <- $caller Caller
    <- $meeting_uid str
    <- $after int                          # 游标：只要 seq > after
    <- $kinds list[StreamKind]             # 空 = 全部五条流
    <- $mode EventMode
    <- Assert[{ chunks 内 seq 严格递增；$next_cursor ≥ after }]
    <- Assert[{ chunks ⊆ MeetingData 的记录，不改写、不聚合、不重排 }]
    <- Hint[{ 至少一次投递：客户端按 after 去重，引擎不承诺恰好一次 }]

read_blob :=
    Blob
    <- $caller Caller
    <- $digest str
    <- Assert[{ digest == hash(bytes) }]

submit_intent :=
    IntentAck
    <- $caller Caller
    <- $meeting_uid str
    <- $envelope IntentEnvelope
    <- Assert[{ 意图先入日志（$received）再执行；结果回写 started/completed/rejected }]
    <- Assert[{ 同一 $idempotency_key 重放：不产生第二次效果，返回首次结果 }]
    <- Assert[{ $envelope.intent ∈ {$take_terminal, $release_terminal, $terminal_input}
                ⇒ can_drive_terminal(meeting_uid, caller.user_name) }]
    <- Assert[{ $envelope.intent == $score ⇒ judge_name := caller.user_name，不接受 payload 自填 }]
    <- Assert[{ $envelope.intent == $say
                ∧ lookup_role(meeting_uid, caller.user_name) == $passive_discriminator
                ⇒ 本会内 ∃ 人类消息 m 提及 caller.user_name，且本次 say 引用 m }]   # 无召不评论
    <- Assert[{ 拒绝时必须给出原因，原因与本次意图一并入日志 }]

fork_session :=
    SessionHandle
    <- $caller Caller
    <- $request ForkSession
    <- Hint[{ 重放意图与记录，不重放模型调用 }]

end_session :=
    SessionHandle
    <- $caller Caller
    <- $meeting_uid str
    <- $action EndAction
    <- Assert[{ compute_capabilities(caller, meeting_uid) ∋ $session_lifecycle }]
