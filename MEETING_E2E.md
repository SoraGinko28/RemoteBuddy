# RemoteBuddy 真实用户路径 E2E 实测报告

> 目的：加固/重构必须**实测对最终用户的影响**，不能用自定规格的单测自嗨。
> 手段：真 agent + 真 tmux + 真 HTTP，把「用户」完整走一遍，对留下的 journal
> 断言用户可感知的不变量。可复跑：`.venv/bin/python e2e_user_path.py`。

## 一次实测（2026-09-06，refactor_mult_agent_base）

用户旅程：

1. human 发任务（列出目录、回复 DONE）→ 服务创建会议，真 agent（claude-code / deepseek）在 tmux 干活；
2. agent 干完回到 waiting → human 接管（acquire）；
3. human 用 `terminal/input` 真输入 `echo E2E_MARKER_9182` → 回显出现在后续画面；
4. human 交还（release）→ 回 waiting；随后 stop 冻结会议。

结果摘要（`e2e_user_path.py` stdout）：

```json
{
  "meeting_id": "e2e-cdcbb21f",
  "agent_task": "列出当前目录内容（ls -la），然后回复 DONE_E2E_7E3。不要写文件。",
  "total_events": 45,
  "elapsed_s": 12.8,
  "acquire_latency_ms": 102,
  "agent_chat_count_by_first_reply": 1,
  "control_acquired": 1,
  "control_released": 1,
  "human_action_events": 3,
  "human_action_statuses": ["received", "started", "completed"],
  "loader_mapped": {"chat": 3, "screen": 7, "terminal_control": 1},
  "validator_violations": 0,
  "passed": true,
  "failures": []
}
```

## 一次实测（2026-09-27，AgentCmd：agent 交命令与 diff，不再自己敲键盘）

工作区是**测试工作区** `/Users/bytedance/tmp/test`（先给它建了 git 仓库：harness 拍快照、核对
落盘结果要一个仓库，`.gitignore` 里排掉 `.meeting/` 和 `__pycache__/`）。

用户旅程：

1. human 说一句题面（建 `greet.py` + `test_greet.py`，跑通 pytest）；
2. agent 交一份 `diff`（两个新文件），harness 在共享终端里逐文件用 vim 落盘、核对，**一轮对上**；
3. 随后 `command` 跑 pytest：先读到返回码 1（没装 pytest），装上再跑，返回码 0；
4. 会议走到 `completion_proposed`。

可复跑：`.venv/bin/python e2e_agent_cmd.py --workspace /Users/bytedance/tmp/test --keep`

```json
{
  "meeting_id": "agent-cmd-3132d2b3",
  "workspace": "/Users/bytedance/tmp/test",
  "phase": "completion_proposed",
  "elapsed_s": 14.5,
  "action_kinds": ["command", "diff", "done"],
  "diff_results": [
    {"kind": "diff", "rounds": 1, "retcode": "0",
     "files": ["greet.py", "test_greet.py"], "mismatched_files": [], "stopped": false}
  ],
  "cmd_results": [
    {"cmd": "python3 -m pytest -q", "retcode": "1"},
    {"cmd": "python3 -m pip install --break-system-packages -q pytest", "retcode": "0"},
    {"cmd": "python3 -m pytest -q", "retcode": "0"}
  ],
  "greet_py": true, "test_greet_py": true, "pytest_returncode": 0,
  "passed": true, "failures": []
}
```

这一轮还逼出过一个真 bug：`vim` 起来之后、把终端设成 raw 模式之前有一小段，那段时间敲进去的
按键仍按行输入算 —— `C-c`（我们用来退出插入模式的键）会变成信号，把还在初始化的 vim 打断，
剩下的按键就落到 shell 手里（journal 里能直接看到 `1Gidef greet(name):` 被 shell 回显、
`:wq` 被当命令执行）。修法是等编辑器**真的把屏幕接管过去**（前台是编辑器，而且画面已经不是
刚才那一屏）再开始敲。修完在同一个工作区连跑 8 次、CPU 满载再跑 8 次，都是第一轮就对。

## 断言的不变量（用户可感知）与结果

| # | 不变量 | 用户意义 | 结果 |
|---|---|---|---|
| 1 | agent 回话 + screen/action 事件出现 | 用户发话后 agent 真在干活 | pass |
| 2 | control acquired/released 各 ≥1、时序正确 | 前端接管/交还的依据完整 | pass |
| 3 | 用户输入动作 received→started→completed 簿记齐全 | 接管期间的键击不丢（前端簿记合并依赖） | pass |
| 4 | 用户输入的回显出现在 journal 画面里 | 用户真能看到自己敲的字符 | pass |
| 5 | 事件 seq 严格递增且唯一、captured_at 不倒退 | 回放排序/游标跟随的前提 | pass |
| 6 | 终态 control_owner 已释放 | 交还后不再误占终端 | pass |
| 7 | 这份真记录过 meeting_validate：0 违规 | G 链校验器对真实用户数据成立（非纸面） | pass |

## 物证

- 本场真会 journal 收在 `runs/e2e-cdcbb21f/`（45 事件），
  已并入 `MEETING_AUDIT.md` 全量审计：18/18 目录合规（0 违规）。
- 实测耗时 12.8s；接管延迟 102ms（acquire 轮询粒度 1s，实际更快）。

## 边界与成本提示

- 走的是服务真实 HTTP 面（`/api/session|meetings|messages|control/acquire|control/release|terminal/input|stop`），
  与浏览器同路径；CSRF/Host 校验全真。
- 会消耗模型 API 额度与几分钟；这是刻意为之——"别自嗨"的代价。
- 若模型或网络不可达，脚本超时报错退出非 0（可作环境性拦截，不算通过）。
