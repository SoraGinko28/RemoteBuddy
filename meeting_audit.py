"""meeting_audit — 全量审计 runs/ 下所有 journal，输出校验报告（Markdown）。

用法：
  python -m meeting_audit            # 打印表格
  python -m meeting_audit --write    # 把报告写入 MEETING_AUDIT.md

设计初衷：
- 校验器只负责「一条记录符不符合 ADT」；审计器负责「一批记录（一次会议、
  全部历史 journal）整体是否合规」，并产出可复核的书面报告。
- 不改任何 journal；对加载不了的目录记录原因而不是静默吞掉。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import meeting_validate as validator


def audit_runs(runs_root: Path) -> list[dict]:
    """遍历 runs/ 下的会议目录，逐一会话 加载+校验。"""
    rows: list[dict] = []
    if not runs_root.is_dir():
        raise FileNotFoundError(f"no runs dir at {runs_root}")
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        row: dict = {"run": run_dir.name}
        try:
            report, summary = validator.validate_run_dir(run_dir)
            row.update({
                "event_total": summary.event_total,
                "mapped": dict(summary.mapped),
                "violations": report.count,
                "by_assert": dict(report.by_assert()),
                "ok": report.ok,
                "error": None,
            })
        except FileNotFoundError as exc:
            row.update({"event_total": None, "mapped": None,
                        "violations": None, "by_assert": None,
                        "ok": False, "error": str(exc)})
        rows.append(row)
    return rows


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if value is True:
        return "ok"
    if value is False:
        return "FAIL"
    return str(value)


def render_markdown(rows: list[dict]) -> str:
    lines = [
        "# MeetingData 全量审计报告",
        "",
        "> 由 `python -m meeting_audit --write` 生成，可复跑。",
        "> 校验器：`meeting_validate.validate_meeting_data`；投影：`meeting_loader`。",
        "",
        "## 逐会议结果",
        "",
        "| run | events | chat | screen | keystroke | score | terminal_control | violations | 结论 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        mapped = row.get("mapped") or {}
        lines.append(
            "| {run} | {ev} | {chat} | {screen} | {key} | {score} | {tc} | {viol} | {ok} |".format(
                run=row["run"],
                ev=_cell(row.get("event_total")),
                chat=_cell(mapped.get("chat")),
                screen=_cell(mapped.get("screen")),
                key=_cell(mapped.get("keystroke")),
                score=_cell(mapped.get("score")),
                tc=_cell(mapped.get("terminal_control")),
                viol=_cell(row.get("violations")),
                ok=_cell(row.get("ok")) if row.get("error") is None else row["error"][:60],
            )
        )
    ok_count = sum(1 for r in rows if r.get("ok"))
    loadable = sum(1 for r in rows if r.get("error") is None)
    lines += [
        "",
        f"结论：可加载 {loadable}/{len(rows)}，全部合规（0 违规）。" if ok_count == loadable
        else f"结论：可加载 {loadable}/{len(rows)}，合规 {ok_count}。",
        "",
        "## 审计中确认的投影决策",
        "",
        "1. **screen 帧归一化**：journal 的屏幕文本逐行右去尾空格、剥末尾空行，"
        "不足 pane_height 时空行补齐 → 投影后的 ScreenCapture 满足 ADT 全部帧断言"
        "（plain_of==screen_data / 行数==height / 行宽≤width）。",
        "2. **harness 不算终端占有者**：actor=harness 的 acquired 只是 RemoteBuddy "
        "会话层接管，不构成 TerminalControl（tmux 拥有者仍是 human 或 generator agent）。",
        "3. **role map 推断**：现 journal 无角色元数据，出现的 agent 一律推断为 "
        "generator（单 agent 会议成立）。",
        "4. **缺流即空流**：无逐键/评分事件的会议不落 keystroke/score chunk，"
        "校验按空流处理（等价语义）。",
        "",
        "## 给 ADT/存储的修订建议（不改代码，只做标注）",
        "",
        "1. MeetingData 的 `sorted_stream_chunks` 允许缺流（缺 = 空流）宜在 ADT 文档点明。",
        "2. `agent_name2role` 应随会议持久化（建议写入清单），不要再靠推断；"
        "将来多 agent / 审核链需要真实角色。",
        "3. journal 需要开始记录逐键事件与评分事件，keystroke/score 流才有数据可验。",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path("runs")
    rows = audit_runs(root)
    markdown = render_markdown(rows)
    if "--write" in args:
        Path("MEETING_AUDIT.md").write_text(markdown, encoding="utf-8")
        print(f"written MEETING_AUDIT.md ({len(rows)} runs)")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
