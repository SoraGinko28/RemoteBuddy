import tempfile
import unittest
from pathlib import Path

from meeting_v2 import MeetingStore
from terminal_text import encode_segments, parse_segments, plain_of
from workspace_capture import capture_workspace, strip_ansi


ANSI_FRAME = (
    "bash-3.2$ \x1b[1;32mgit\x1b[0m status\n"
    "\x1b[31mmodified:\x1b[0m  notes.md\n"
    "\x1b[4mfile\x1b[0m.txt"
)
PLAIN_FRAME = (
    "bash-3.2$ git status\n"
    "modified:  notes.md\n"
    "file.txt"
)


class AnsiTransport:
    def __init__(self):
        self.pane_id = "%1"

    def pane_info(self, _pane_id):
        return {
            "dead": "0",
            "command": "bash",
            "path": "/workspace",
            "cursor_x": "0",
            "cursor_y": "3",
            "pane_width": "120",
            "pane_height": "36",
        }

    def capture_visible(self, _pane_id):
        return PLAIN_FRAME

    def capture_visible_ansi(self, _pane_id):
        return ANSI_FRAME


class PlainTransport:
    """Transport without the ANSI capture method: legacy fallback path."""

    def __init__(self):
        self.pane_id = "%1"

    def pane_info(self, _pane_id):
        return {
            "dead": "0",
            "command": "bash",
            "path": "/workspace",
            "cursor_x": "0",
            "cursor_y": "3",
            "pane_width": "120",
            "pane_height": "36",
        }

    def capture_visible(self, _pane_id):
        return PLAIN_FRAME


class StripAnsiTest(unittest.TestCase):
    def test_colors_bold_and_underline_are_stripped(self):
        self.assertEqual(PLAIN_FRAME, strip_ansi(ANSI_FRAME))

    def test_plain_text_passes_through_unchanged(self):
        self.assertEqual("hello\nworld", strip_ansi("hello\nworld"))

    def test_osc_and_truecolor_sequences_are_removed(self):
        text = "\x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\ \x1b[38;2;10;20;30mhi\x1b[0m"
        self.assertEqual("link hi", strip_ansi(text))

    def test_other_control_sequences_are_not_mangled(self):
        self.assertEqual("a\r\nb", strip_ansi("a\r\nb"))


class ParseSegmentsTest(unittest.TestCase):
    def test_plain_projection_equals_stripped_text(self):
        segments = parse_segments(ANSI_FRAME)
        self.assertEqual(PLAIN_FRAME, plain_of(segments))

    def test_canonical_merge_of_adjacent_same_attributes(self):
        segments = parse_segments("\x1b[31mred\x1b[31mred2\x1b[0mplain")
        self.assertEqual(2, len(segments))
        self.assertEqual("redred2", segments[0]["text"])
        self.assertEqual({"palette": 1}, segments[0]["fg"])
        self.assertEqual("plain", segments[1]["text"])
        self.assertIsNone(segments[1]["fg"])

    def test_styles_and_truecolor_are_parsed(self):
        frame = "\x1b[1;38;2;10;20;30mstyled\x1b[0m plain"
        segments = parse_segments(frame)
        self.assertEqual(2, len(segments))
        self.assertEqual(["bold"], segments[0]["styles"])
        self.assertEqual({"truecolor": {"r": 10, "g": 20, "b": 30}}, segments[0]["fg"])
        self.assertEqual(" plain", segments[1]["text"])

    def test_256_palette_and_background(self):
        segments = parse_segments("\x1b[48;5;200mbg\x1b[0m x")
        self.assertEqual({"palette": 200}, segments[0]["bg"])

    def test_plain_of_right_trims_rows(self):
        self.assertEqual(
            "a\nb",
            plain_of([{"text": "a  \nb  ", "fg": None, "bg": None, "styles": []}]),
        )

    def test_newline_survives_inside_text(self):
        segments = parse_segments("\x1b[42mfull\nrow\x1b[0m")
        self.assertIn("\n", segments[0]["text"])
        self.assertEqual("full\nrow", plain_of(segments))


class EncodeSegmentsTest(unittest.TestCase):
    def test_encode_round_trips_to_same_canonical_segments(self):
        frame = (
            "\x1b[1;32mgit\x1b[0m status\n"
            "\x1b[48;5;200mbg\x1b[0m\n"
            "\x1b[38;2;10;20;30mhi\x1b[0m"
        )
        parsed = parse_segments(frame)
        re_parsed = parse_segments(encode_segments(parsed))
        self.assertEqual(parsed, re_parsed)

    def test_encode_preserves_plain_projection(self):
        segments = parse_segments("\x1b[31ma \n b\x1b[0m c")
        self.assertEqual(
            plain_of(segments),
            plain_of(parse_segments(encode_segments(segments))),
        )

    def test_default_segment_encodes_with_self_contained_reset(self):
        self.assertEqual("\x1b[0mplain", encode_segments(
            [{"text": "plain", "fg": None, "bg": None, "styles": []}]
        ))
        self.assertEqual("plain", plain_of(parse_segments("\x1b[0mplain")))


class CaptureSegmentsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        skill = self.root / "SKILL.md"
        skill.write_text("---\nname: test\ndescription: t.\n---\nBe specific.", encoding="utf-8")
        self.store = MeetingStore.create(
            self.root,
            "segments-demo",
            "Run a command.",
            skill,
            "remotebuddy-segments-demo",
            "test-model",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_capture_keeps_plain_content_and_stores_segments(self):
        transport = AnsiTransport()
        self.store.set_session(transport.pane_id)
        capture_workspace(self.store, transport)
        screens = [
            event for event in self.store.events() if event["kind"] == "screen"
        ]
        self.assertEqual(1, len(screens))
        self.assertEqual(PLAIN_FRAME, screens[0]["content"])
        self.assertEqual(PLAIN_FRAME, plain_of(screens[0]["data"]["segments"]))
        self.assertEqual(
            encode_segments(screens[0]["data"]["segments"]),
            screens[0]["data"]["ansi"],
        )
        self.assertEqual("36", str(screens[0]["data"]["pane_height"]))

    def test_legacy_transport_without_ansi_falls_back_to_plain(self):
        transport = PlainTransport()
        self.store.set_session(transport.pane_id)
        capture_workspace(self.store, transport)
        screens = [
            event for event in self.store.events() if event["kind"] == "screen"
        ]
        self.assertEqual(1, len(screens))
        self.assertEqual(PLAIN_FRAME, screens[0]["content"])
        self.assertNotIn("segments", screens[0]["data"])
        self.assertNotIn("ansi", screens[0]["data"])


if __name__ == "__main__":
    unittest.main()
