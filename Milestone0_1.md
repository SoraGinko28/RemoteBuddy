# Milestone 0.1: 双 Agent 会议 Demo

这个 Demo 用一个 Python Harness 驱动同一 tmux 会话中的两个真实 Codex。会议固定为三轮：提案、质疑、修订。议题不写死，必须在运行时传入 UTF-8 Markdown brief。

## 使用

```bash
python3 demo.py doctor
python3 demo.py run --brief /absolute/path/to/topic.md
python3 demo.py status <meeting-id>
python3 demo.py stop <meeting-id>
```

`run` 还接受 `--meeting-id`、`--model`、`--turn-timeout` 和 `--capture-interval`。默认单轮超时 300 秒，每秒检查并记录一次发生变化的 pane 画面。

## 协议

- 两个参与者共享 `$meeting-participant` Skill，Harness 为每轮注入角色、目标、turn ID 和唯一输出文件。
- Harness 在投递会议提示前只处理两种已知启动界面：更新提示明确选择 “Skip”，目录信任确认选择 “Yes”。其他确认界面不会被自动接受；Harness 不会修改用户的全局 Codex 配置。
- Agent 仅被允许用 `pwd`、`cd` 和 `vim`；这是提示层软约束，不是系统调用隔离。
- Agent 必须把正文写入 `turns/<turn-id>.md`，以固定文件标记结尾，并在终端提交对应的完成回复。
- Harness 同时看到有效文件和终端完成回复后，才追加聊天事件并推进下一轮。
- 为避开 Codex TUI 的快速粘贴保护，literal 提示与提交 Enter 之间保留 5 秒稳定窗口；期间仍持续 capture 和完整性检查。
- Harness 不解析普通 Codex TUI 内容；终端画面只作为独立 screen stream 保存。
- 任一回合超时、pane 退出、输入被修改或回合文件无效都会立即失败，且不会重试。

## 输出

每次会议位于 `runs/<meeting-id>/`。`meeting.json` 是原子更新的状态清单；`streams/chat.jsonl` 是已接受的发言；两条 `screen.*.jsonl` 保存去重后的完整 pane 快照；`conversation.md` 始终由聊天流重建。

成功、失败和人工中断后都会保留 tmux session。CLI 会打印 attach 命令；`stop` 是唯一的显式清理入口。

## 当前限制

运行需要 Python 3.9+、已登录的 Codex CLI、vim 和 tmux。`doctor` 只报告缺失项，不会安装软件，也不会回退到 screen。本阶段没有 Web UI、音视频、自由讨论、自动重试或硬命令隔离。
