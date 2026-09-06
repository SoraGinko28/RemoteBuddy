# RemoteBuddy MeetingData 存储格式规格

> 存储层定稿，随目标链 G1–G14 固化；**schema 唯一源是 `meeting_adt.py`**
> （每个类型都带 `*_to_dict / *_from_dict`，本文只做面向人类的摘要）。
> 与 ADT（`MEETING_ADT.md`）分层：ADT 讲能力层不变量，本文讲这些记录如何落盘。

## 1. 目录布局（沿用现有 runs/ 结构）

```
runs/<meeting_uid>/
  meeting.json            # 会议清单：uid/name/role map 等元数据（见 §4）
  streams/events.jsonl    # 原始事件 journal（append-only，写入源）
  workspace/              # 工作目录快照（数据层，不在本文）
```

- 冻结后 `meeting.json` 固化只读；新事件只 **append** 到 `events.jsonl`，绝不原地改。
- loader（`meeting_loader.py`）把 events.jsonl 投影成 MeetingData，**只读不改 journal**。

## 2. 五流的落盘 JSON（`stream_chunk_to_dict` / `from_dict`）

通用 chunk 信封：`{"stream_name": <五流之一>, ...chunk 特有字段}`；
MeetingData 清单里 `sorted_stream_chunks` 是这些 chunk JSON 的列表，按首记录时间升序。

| 流 | chunk 特有字段 | 单条记录 |
|---|---|---|
| screen | `owner_user_name`, `chunks[]` | `ScreenCapture`：`screen_data`(纯文本满帧) / `terminal_text_content[]`(带样式段) / `cursor_x,y` / `screen_width,height` / `captured_at`(ISO8601 UTC, `Z`) |
| chat | `chunks[]` | `ChatMessage`：`user_name` / `time` / `message[]`，段元素 = `{"raw": str}` \| `{"at_user_name": str}` \| `{"ref": <ContentReference>}`（ref 键由 G8 加入） |
| score | `chunks[]` | `ScoreMessage`：`tag` / `score`\|`comment`(nullable) / `judge_name` / `time` / `reference` |
| keystroke | `chunks[]` | `Keystroke`：`data`(={text\|ctrl\|rest.ms}) / `user_name` / `stroke_time` |
| terminal_control | `chunks[]` | `TerminalControl`：`tmux_window_name` / `tmux_pane` / `user_name` / `start_time` |

样式/颜色 JSON：
`TerminalTextSegment` = `{"text", "fg"|null, "bg"|null, "styles":[...]}`
`Color` = `null`（默认） \| `{"palette": 0..255}` \| `{"r","g","b"}`（均 0..255）
`styles` 是 Style 名字符串数组（bold/underline/italic/reverse/blink/dim），落盘前排序保持确定性。

ContentReference 单键信封：
`{"screen_ref": {capture_time, stream_name, region:{start,end}}}`
`{"chat_ref":   {msg_time, user_name, region:{start,end}}}`
`{"score_ref":  {judge_time, judge_name}}`
**引用只存键，不存目标内容副本**；resolve 靠同会流内按键查找（`resolve_*`）。

## 3. events.jsonl kind ↔ 五流映射

| journal kind | 目标流 | 映射规则 |
|---|---|---|
| `chat` | chat | content→文本段；actor human→`"$human"`，否则 actor 即 agent 名；time=captured_at |
| `screen` | screen | content 经**帧归一化**（§5）→ ScreenCapture；cursor/尺寸取 data |
| `control` (data.status=acquired, actor≠harness) | terminal_control | acquired 时刻即区间起点；release 只推进不落记录 |
| `action` / `state` / `error` | — | 运营噪音，跳过 |
| （无事件 kind） | keystroke / score | **缺流即空流**，不落 chunk |

## 4. 会议清单（meeting.json 语义 → MeetingData）

`MeetingData` JSON（`meeting_data_to_dict`）：
```json
{
  "meeting_uid": "...", "meeting_name": "...",
  "agent_name2role": {"alice": "$generator", "bob": "$passive_discriminator"},
  "sorted_stream_chunks": [ {chunk json}, ... ]
}
```

- `agent_name2role` **只增不减**：join 加入，冻结后固化。human（`"$human"`）永不入表。
- 现 journal 无角色元数据 → loader 把出现的 agent 推断为 generator（审计注明；
  修订建议：将来把 role map 持久化进清单）。
- `keys(agent_name2role) ⊆ user_names(记录)` 是 MeetingData 不变量（校验器管）。

## 5. 投影归一化（loader 的 writer 边界职责）

screen content 进入 ADT 前：
1. 每行 `rstrip(' ')`（screen_data 语义：逐行右去尾空格）；
2. 剥掉末尾整行空行（无尾随空行）；
3. 行数不足 `pane_height` 时补空行到满帧 → 满足 `rows==height`。

## 6. 空流 / 空 chunk

无事件的流可以整流缺失（等价空流），也可以显式落空 chunk
（如 `ScoreStreamChunk(chunks=[])`）；两种表示校验等价。

## 7. 校验入口

`meeting_validate.validate_meeting_data(meeting)` → 违规清单
`meeting_audit.audit_runs(Path("runs"))` → 全量报告（`python -m meeting_audit --write`）。

## 8. 待办（不改代码的标注）

1. ADT 文档点明 `sorted_stream_chunks` 允许缺流。
2. role map 随清单持久化，替代推断。
3. journal 开始记录逐键与评分事件，keystroke/score 流才有数据。
