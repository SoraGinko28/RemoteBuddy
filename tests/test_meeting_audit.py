"""Tests for meeting_audit: audit entry point and report rendering."""

import json


class TestAuditRuns:
    def _write_run(self, tmp_path) -> object:
        import datetime as dt
        from datetime import timezone
        import meeting_adt as adt
        import meeting_loader as loader

        run_dir = tmp_path / "20260902-153537-7f1bea"
        (run_dir / "streams").mkdir(parents=True)
        (run_dir / "meeting.json").write_text(json.dumps({
            "meeting_id": "20260902-153537-7f1bea",
            "session_name": "demo",
        }), encoding="utf-8")
        moment = dt.datetime(2026, 9, 2, 7, 35, 37, tzinfo=timezone.utc)
        chat = {
            "seq": 1, "event_id": "e-1", "captured_at": "2026-09-02T07:35:37Z",
            "kind": "chat", "actor": "human", "phase": "created",
            "content": "hi", "message_type": None, "data": {},
        }
        screen = {
            "seq": 2, "event_id": "e-2", "captured_at": "2026-09-02T07:35:38Z",
            "kind": "screen", "actor": "workspace", "phase": "x",
            "content": "ab\ncd", "message_type": None,
            "data": {"cursor_x": 0, "cursor_y": 0, "pane_width": 80,
                     "pane_height": 24},
        }
        (run_dir / "streams" / "events.jsonl").write_text(
            json.dumps(chat) + "\n" + json.dumps(screen) + "\n", encoding="utf-8")
        meeting, summary = loader.load_meeting_data_from_run(run_dir)
        assert summary.mapped["screen"] == 1
        # frame assertions pass after normalization
        from meeting_validate import validate_meeting_data
        report = validate_meeting_data(meeting)
        assert report.ok, report.violations
        return run_dir

    def test_audit_marks_run_ok(self, tmp_path) -> None:
        import meeting_audit as audit
        run_dir = self._write_run(tmp_path)
        # put the run under a runs/ root directory
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        (runs_root / run_dir.name).symlink_to(run_dir, target_is_directory=True)
        rows = audit.audit_runs(runs_root)
        assert len(rows) == 1
        assert rows[0]["ok"] is True
        assert rows[0]["event_total"] == 2

    def test_render_contains_run_and_conclusion(self, tmp_path) -> None:
        import meeting_audit as audit
        markdown = audit.render_markdown([
            {"run": "r1", "event_total": 5, "mapped": {"chat": 2},
             "violations": 0, "by_assert": {}, "ok": True, "error": None},
        ])
        assert "r1" in markdown
        assert "Conclusion" in markdown
        assert "| 2 |" in markdown

    def test_audit_missing_dir_raises(self, tmp_path) -> None:
        import pytest
        import meeting_audit as audit
        with pytest.raises(FileNotFoundError):
            audit.audit_runs(tmp_path / "no-runs")
