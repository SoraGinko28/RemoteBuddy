# ═══════════════════════════════════════════════════════════════════════
# RemoteBuddy 服务层接口 ADT
#
# 引擎对外唯一的门。下面每个函数就是一次跨进程调用：客户端（网页 / CLI /
# 飞书 / critic / judge）与 harness 一律经此读写会议事实；进程内直接
# import 引擎模块（e2e_*.py 那种写法）作废。
#
# 分工：本文只写接口保证（顺序、幂等、投递语义、能力门禁、寻址）；
#       类型与数据恒等式见 SERVICE_CONCEPTS_ADT.md。
#
# 通用约定：
#   - 每个函数都带 $caller：身份由服务端从凭据解析，客户端不能自称。
#   - 会内调用一律用 $meeting_uid 寻址；会议是唯一的寻址单位。
#   - 写操作全部走 submit_intent，没有第二个写入口。
# ═══════════════════════════════════════════════════════════════════════

create_session :=
    SessionHandle
    <- $caller Caller                     # 建会者
    <- $request CreateSession             # 首条消息、参与者表、后端、模型
    # 建会即开跑：actor 拿到第一个 cycle，客户端随后用 read_events 跟。
    # 参与者表缺省 = {actor: $generator, critic: $passive_discriminator}。
    # 对应今天的 POST /api/meetings。

list_sessions :=
    list[SessionHandle]
    <- $caller Caller
    # 侧栏用：只给会话级摘要（阶段、游标、参与者、能力），不给事件内容。
    # 对应今天的 GET /api/meetings。

read_events :=
    EventPage
    <- $caller Caller
    <- $meeting_uid str                   # 目标会议
    <- $after int                         # 游标：只要 seq > after；首次传 0
    <- $kinds list[StreamKind]            # 空 = 五条流全要；可只订 chat 减流量
    <- $mode EventMode                    # $all 全量帧（网页）；$latest 只取最新一张（飞书）
    <- Assert[{ 原样返回记录，不改写、不聚合、不重排 }]
    # ↑ 客户端看到的必须就是日志里的东西；裁剪是客户端自己的事。否则
    #   critic 判的、人类看的、回放出来的，不再是同一份事实。
    <- Hint[{ 至少一次投递：客户端按 after 去重，引擎不承诺恰好一次 }]
    # ↑ 断线重连带上次的 next_cursor 继续拉即可，不需要 ack。

read_blob :=
    Blob
    <- $caller Caller
    <- $digest str                        # 事件里带的 content_sha256
    # 截屏帧这类大对象不进事件流，客户端按需取；飞书这种弱端可以永不取。

submit_intent :=
    IntentAck
    <- $caller Caller
    <- $meeting_uid str
    <- $envelope IntentEnvelope           # 幂等键 + 一个意图
    # 唯一的写入口：说话、评分、接管、敲键盘、继续、结束，全是意图。

    <- Assert[{ 先入日志（$received）再执行，结果回写 started/completed/rejected }]
    # ↑ 先记后做：崩在哪一步日志里看得出来，也才谈得上回放与分叉。

    <- Assert[{ 同一 $idempotency_key 重放：不产生第二次效果，返回首次结果 }]
    # ↑ 给飞书 webhook、CLI 重试这类可能重复投递的客户端兜底。

    <- Assert[{ $envelope.intent ∈ {$take_terminal, $release_terminal, $terminal_input}
                ⇒ can_drive_terminal(meeting_uid, caller.user_name) }]
    # ↑ 终端只有一个写者：人类与 generator。critic 再活跃也发不进来。

    <- Assert[{ $envelope.intent == $score ⇒ judge_name := caller.user_name，不接受 payload 自填 }]
    # ↑ 评分必须归到真实评分人：自进化里人类评分与 critic 评分权重不同。

    <- Assert[{ $envelope.intent == $say
                ∧ lookup_role(meeting_uid, caller.user_name) == $passive_discriminator
                ⇒ 本会内 ∃ 人类消息 m 提及 caller.user_name，且本次 say 引用 m }]   # 无召不评论
    # ↑ passive discriminator 的"召"就是人类的 @；active 不受此限。

fork_session :=
    SessionHandle
    <- $caller Caller
    <- $request ForkSession
    <- Hint[{ 重放意图与记录，不重放模型调用 }]
    # 自进化用：同一段历史换提示词 / persona 重跑，结果可归因到那次改动。

end_session :=
    SessionHandle
    <- $caller Caller
    <- $meeting_uid str
    <- $action EndAction                  # $stop 中止 / $complete 人类确认完成 / $cleanup 清 tmux
    <- Assert[{ compute_capabilities(caller, meeting_uid) ∋ $session_lifecycle }]
    # ↑ 只有管得了会话的人才结束得了；"做完了"仍要人类点头（$complete）。
