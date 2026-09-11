# RemoteBuddy 服务层接口 ADT

> 引擎对外唯一的门。客户端（网页 / CLI / 飞书 / critic / judge）与 harness
> 一律经此读写会议事实；进程内直接 import 引擎模块的用法作废。
> 类型与数据恒等式见 SERVICE_CONCEPTS_ADT.md，本文只写接口保证。

create_session :=
    SessionHandle
    <- $caller Caller
    <- $request CreateSession

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
    <- Assert[{ 原样返回记录，不改写、不聚合、不重排 }]
    <- Hint[{ 至少一次投递：客户端按 after 去重，引擎不承诺恰好一次 }]

read_blob :=
    Blob
    <- $caller Caller
    <- $digest str

submit_intent :=
    IntentAck
    <- $caller Caller
    <- $meeting_uid str
    <- $envelope IntentEnvelope
    <- Assert[{ 先入日志（$received）再执行，结果回写 started/completed/rejected }]
    <- Assert[{ 同一 $idempotency_key 重放：不产生第二次效果，返回首次结果 }]
    <- Assert[{ $envelope.intent ∈ {$take_terminal, $release_terminal, $terminal_input}
                ⇒ can_drive_terminal(meeting_uid, caller.user_name) }]
    <- Assert[{ $envelope.intent == $score ⇒ judge_name := caller.user_name，不接受 payload 自填 }]
    <- Assert[{ $envelope.intent == $say
                ∧ lookup_role(meeting_uid, caller.user_name) == $passive_discriminator
                ⇒ 本会内 ∃ 人类消息 m 提及 caller.user_name，且本次 say 引用 m }]   # 无召不评论

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
