"""meeting_loader — 把 runs/<meeting_id>/ 真 journal 装载成 MeetingData。

设计初衷（对齐 MEETING_ADT.md + meeting_adt.py）：
- 只做「events.jsonl + meeting.json → MeetingData」的投影，只读不改 journal。
- ADT 五流 = 能力层；loader 是存储格式切片的一部分：
  kind↔五流映射表是存储格式的核心，字段不符处留给校验器报、再回改。
- 不在地层做任何复制/去重/索引：loader 保持朴素顺序读取。

kind ↔ 五流映射（对照 runs/20260902-153537-7f1bea）：
  chat                    -> 聊天流 ChatMessage（content 即文本，actor→UserName）
  screen                  -> 屏幕流 ScreenCapture（content 即画面纯文本）
  control (acquired)      -> 控制流 TerminalControl（human 接管区间起点）
  action / state / error  -> 不映射（运营噪音，非五流内容）
  keystroke / score       -> 现 journal 无事件 → 空流（语义上允许缺流）

已知局限（留给校验报告标注，不改 journal）：
  - 现 journal 无逐键事件、无评分事件、无 role 元数据；
    agent_name2role 只能按「出现的 agent 都是 generator」推断（单 agent 会议成立）。
  - control 事件只含 acquired/released，无 tmux 窗格信息：
    TerminalControl 记 tmux_window_name/pane 为空缺，由拥有 pane 的运行方补。
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import meeting_adt as adt

# 控制事件 phase 里判定 acquired/released 的取值
_ACQUIRED_PHASES = {"human_control"}
_RELEASED_PHASES = {"agent_thinking", "waiting_for_user"}

# 运营噪音 kind：不映射进五流
_NOISE_KINDS = {"action", "state", "error"}

# 非终端占有者 actor：harness 是 RemoteBuddy 自身，不构成 terminal_control 记录
# （其 acquired 只是会话层接管，不改变「谁握着 tmux」的语义）
_NON_OWNER_ACTORS = {"harness"}


@dataclass
class LoadSummary:
    """一次装载的计数摘要（供打印/测试断言）。"""

    event_total: int = 0
    mapped: dict = field(default_factory=dict)  # stream_name -> record 数
    skipped_noise: int = 0
    skipped_unrecognized: int = 0
    skipped_malformed: int = 0


def _role_map_from_events(events: list[dict], manifest: dict) -> dict:
    """推断 agent_name2role。

    现 journal 无角色元数据：凡出现过的非 human agent 一律推断为 generator
    （单 agent 会议的 tmux 拥有者）。$human 永不入 role map。
    """
    agents: set[str] = set()
    for event in events:
        actor = event.get("actor")
        if isinstance(actor, str) and actor not in ("human", "workspace", "harness"):
            agents.add(actor)
    return {name: adt.AgentRole.GENERATOR for name in sorted(agents)}


def _time_of(event: dict) -> Optional[object]:
    captured = event.get("captured_at")
    if isinstance(captured, str):
        try:
            return adt._iso_parse(captured)
        except ValueError:
            return None
    return None


def _chat_message(event: dict) -> Optional[adt.ChatMessage]:
    content = event.get("content")
    if not isinstance(content, str):
        return None
    time = _time_of(event)
    if time is None:
        return None
    user_name = adt.HUMAN_USER_NAME if event.get("actor") == "human" else event.get("actor", "")
    if not user_name:
        return None
    return adt.ChatMessage(
        user_name=user_name,
        time=time,
        message=[adt.RawMessage(text=content)],
    )


def _normalize_frame(content: str, height: int) -> str:
    """把 journal 里保存的屏幕文本归一化为 ADT screen_data。

    规则（ADT：screen_data 逐行右去尾空格、无尾随空行、行数==height）：
      1. 每行 rstrip(' ')；
      2. 末尾整行空行剥掉（无尾随空行）；
      3. 不足 height 行时空行补足 —— 终端满帧是 height 行，
         journal 常省略底部空行，投影为满帧记录。
    返回行数恰为 height 的文本。
    """
    rows = [r.rstrip(" ") for r in content.split("\n")]
    while rows and rows[-1] == "":
        rows.pop()
    if len(rows) < height:
        rows.extend([""] * (height - len(rows)))
    return "\n".join(rows[:height])


def _screen_capture(event: dict) -> Optional[adt.ScreenCapture]:
    content = event.get("content")
    if not isinstance(content, str) or not content:
        return None
    time = _time_of(event)
    if time is None:
        return None
    data = event.get("data") or {}
    height = int(data.get("pane_height", 0) or 0)
    width = int(data.get("pane_width", 0) or 0)
    screen_data = _normalize_frame(content, height) if height > 0 else content
    return adt.ScreenCapture(
        screen_data=screen_data,
        terminal_text_content=[adt.TerminalTextSegment(text=screen_data)],
        cursor_x=int(data.get("cursor_x", 0) or 0),
        cursor_y=int(data.get("cursor_y", 0) or 0),
        screen_width=width,
        screen_height=height,
        captured_at=time,
    )


def _terminal_controls(events: list[dict]) -> "list[adt.TerminalControl]":
    controls: list[adt.TerminalControl] = []
    for event in events:
        if event.get("kind") != "control":
            continue
        phase = event.get("phase")
        if phase not in _ACQUIRED_PHASES:
            continue
        status = (event.get("data") or {}).get("status")
        if status != "acquired":
            continue
        time = _time_of(event)
        if time is None:
            continue
        actor = event.get("actor")
        if actor in _NON_OWNER_ACTORS:
            continue  # harness 非终端占有者，不落记录
        user_name = adt.HUMAN_USER_NAME if actor == "human" else actor or ""
        if not user_name:
            continue
        controls.append(adt.TerminalControl(user_name=user_name, start_time=time))
    controls.sort(key=lambda c: c.start_time or _dt.datetime.min)
    return controls


def _screen_owner_runs(screens: "list[adt.ScreenCapture]",
                       controls: "list[adt.TerminalControl]",
                       default_owner: str) -> "list[adt.ScreenStreamChunk]":
    """按连续同 owner 把屏幕流切成 chunk。

    owner(t)：落在某 human 接管区间内 => $human；否则默认 generator agent。
    """
    spans: list[tuple] = []
    for control in controls:
        if adt.is_human(control.user_name):
            spans.append((control.start_time, None, adt.HUMAN_USER_NAME))

    def owner_of(capture: adt.ScreenCapture) -> str:
        for start, _end, owner in spans:
            if start is not None and capture.captured_at is not None \
                    and capture.captured_at >= start:
                return owner
        return default_owner

    chunks: list[adt.ScreenStreamChunk] = []
    for capture in screens:
        owner = owner_of(capture)
        if not chunks or chunks[-1].owner_user_name != owner:
            chunks.append(adt.ScreenStreamChunk(
                stream_name=adt.SCREEN_STREAM_NAME, owner_user_name=owner,
                chunks=[capture]))
        else:
            chunks[-1].chunks.append(capture)
    return chunks


def load_meeting_data_from_run(run_dir: Path) -> tuple[adt.MeetingData, LoadSummary]:
    """从 runs/<id> 装载 MeetingData；缺文件时抛 FileNotFoundError。"""
    run_dir = Path(run_dir)
    manifest_path = run_dir / "meeting.json"
    events_path = run_dir / "streams" / "events.jsonl"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing meeting.json in {run_dir}")
    if not events_path.is_file():
        raise FileNotFoundError(f"missing events.jsonl in {run_dir}")

    with manifest_path.open(encoding="utf-8") as fh:
        manifest = json.load(fh)
    events: list[dict] = []
    event_total = 0
    with events_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            event_total += 1
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # 坏行：算进 total，不计入 events
    events.sort(key=lambda e: e.get("seq", 0))

    summary = LoadSummary(event_total=event_total)
    summary.skipped_malformed = event_total - len(events)
    chats: list[adt.ChatMessage] = []
    screens: list[adt.ScreenCapture] = []
    for event in events:
        kind = event.get("kind")
        if kind == "chat":
            msg = _chat_message(event)
            if msg is not None:
                chats.append(msg)
                summary.mapped[adt.CHAT_STREAM_NAME] = summary.mapped.get(
                    adt.CHAT_STREAM_NAME, 0) + 1
        elif kind == "screen":
            cap = _screen_capture(event)
            if cap is not None:
                screens.append(cap)
                summary.mapped[adt.SCREEN_STREAM_NAME] = summary.mapped.get(
                    adt.SCREEN_STREAM_NAME, 0) + 1
        elif kind in _NOISE_KINDS:
            summary.skipped_noise += 1
        else:
            summary.skipped_unrecognized += 1

    controls = _terminal_controls(events)
    summary.mapped[adt.TERMINAL_CONTROL_STREAM_NAME] = len(controls)

    role_map = _role_map_from_events(events, manifest)
    default_owner = next(iter(sorted(role_map)), "")

    chunks: list = _screen_owner_runs(screens, controls, default_owner)
    if chats:
        chunks.append(adt.ChatStreamChunk(
            stream_name=adt.CHAT_STREAM_NAME, chunks=chats))
    if controls:
        chunks.append(adt.TerminalControlStreamChunk(
            stream_name=adt.TERMINAL_CONTROL_STREAM_NAME, chunks=controls))
    # score / keystroke 无事件 → 缺流（空流语义等价，不落 chunk）
    chunks.sort(key=_chunk_start_time)

    meeting = adt.MeetingData(
        meeting_uid=str(manifest.get("meeting_id") or run_dir.name),
        meeting_name=str(manifest.get("session_name") or run_dir.name),
        agent_name2role=role_map,
        sorted_stream_chunks=chunks,
    )
    return meeting, summary


def _chunk_start_time(chunk: object):
    chunks = getattr(chunk, "chunks", [])
    if not chunks:
        return None
    first = chunks[0]
    for attr in ("captured_at", "time", "start_time"):
        if hasattr(first, attr) and getattr(first, attr) is not None:
            return getattr(first, attr)
    return None
