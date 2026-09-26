import tempfile
import unittest
import sys
from pathlib import Path

from libtmux.pane import PaneDirection

from tmux_transport import TmuxError, TmuxTransport, workspace_command_text


class FakeResult:
    def __init__(self, stdout=None, stderr=None, returncode=0):
        self.stdout = [] if stdout is None else stdout
        self.stderr = [] if stderr is None else stderr
        self.returncode = returncode


class FakeQuery:
    def __init__(self, objects, key):
        self.objects = objects
        self.key = key

    def get(self, **kwargs):
        expected = kwargs[self.key]
        for item in self.objects.values():
            if getattr(item, self.key) == expected:
                return item
        raise LookupError(expected)


class FakePane:
    def __init__(self, pane_id, server):
        self.pane_id = pane_id
        self.server = server
        self.inputs = []
        self.commands = []
        self.dead = "0"
        self.current_command = "bash"
        self.current_path = "/tmp/workspace"
        self.cursor_x = "4"
        self.cursor_y = "7"
        self.pane_width = "120"
        self.pane_height = "36"
        self.screen = "visible screen"

    def send_keys(self, text, enter=True, suppress_history=False, literal=False):
        self.inputs.append(
            {
                "text": text,
                "enter": enter,
                "suppress_history": suppress_history,
                "literal": literal,
            }
        )

    def enter(self):
        self.inputs.append({"key": "Enter"})

    def cmd(self, command, *arguments):
        self.commands.append((command, arguments))
        if command == "send-keys" and arguments[:1] == ("-H",):
            decoded = bytes(int(value, 16) for value in arguments[1:]).decode("utf-8")
            self.inputs.append(
                {
                    "text": decoded,
                    "enter": False,
                    "suppress_history": False,
                    "literal": True,
                }
            )
        if command == "capture-pane":
            return FakeResult(self.screen.split("\n"))
        return FakeResult()

    def display_message(self, template, get_text=False):
        if template == "#{pane_dead}":
            return [self.dead]
        return [
            f"{self.dead}\t{self.current_command}\t{self.current_path}"
            f"\t{self.cursor_x}\t{self.cursor_y}\t{self.pane_width}\t{self.pane_height}"
        ]


class FakeWindow:
    def __init__(self, server):
        self.server = server
        self.active_pane = FakePane("%11", server)
        server.pane_map[self.active_pane.pane_id] = self.active_pane
        self.options = []
        self.splits = []
        self.layouts = []

    def split(self, **kwargs):
        self.splits.append(kwargs)
        pane = FakePane("%12", self.server)
        self.server.pane_map[pane.pane_id] = pane
        return pane

    def set_window_option(self, option, value):
        self.options.append((option, value))
        return self

    def select_layout(self, layout):
        self.layouts.append(layout)
        return self


class FakeSession:
    def __init__(self, session_name, server):
        self.session_name = session_name
        self.server = server
        self.active_window = FakeWindow(server)
        self.killed = False

    def kill(self):
        self.killed = True
        self.server.session_map.pop(self.session_name, None)


class FakeServer:
    def __init__(self):
        self.pane_map = {}
        self.session_map = {}
        self.new_session_calls = []

    @property
    def panes(self):
        return FakeQuery(self.pane_map, "pane_id")

    @property
    def sessions(self):
        return FakeQuery(self.session_map, "session_name")

    def has_session(self, session_name, exact=True):
        return session_name in self.session_map

    def new_session(self, **kwargs):
        self.new_session_calls.append(kwargs)
        session = FakeSession(kwargs["session_name"], self)
        self.session_map[session.session_name] = session
        return session


class TmuxTransportTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.transport = TmuxTransport(server=self.server)
        self.pane = FakePane("%7", self.server)
        self.server.pane_map[self.pane.pane_id] = self.pane

    def test_creates_two_horizontal_codex_panes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            panes = self.transport.create_two_agent_session(
                "remotebuddy-demo", workdir, "/opt/bin/codex", "test-model"
            )

        self.assertEqual({"proposer": "%11", "reviewer": "%12"}, panes)
        created = self.server.new_session_calls[0]
        self.assertFalse(created["attach"])
        self.assertEqual("meeting", created["window_name"])
        self.assertEqual(str(workdir.resolve()), created["start_directory"])
        self.assertEqual(
            str(Path(sys.executable).resolve().parent),
            created["environment"]["PATH"].split(":", 1)[0],
        )
        command = created["window_command"]
        self.assertIn("--no-alt-screen", command)
        self.assertIn("-s workspace-write", command)
        self.assertIn("-a never", command)
        self.assertIn("projects={", command)
        self.assertIn("trust_level=\"trusted\"", command)
        self.assertIn("-m test-model", command)
        self.assertIn("/usr/bin/env", command)
        self.assertIn("CONDA_DEFAULT_ENV=remotebuddy", command)
        window = self.server.session_map["remotebuddy-demo"].active_window
        self.assertEqual(PaneDirection.Right, window.splits[0]["direction"])
        self.assertFalse(window.splits[0]["attach"])
        self.assertEqual(
            [("allow-rename", "off"), ("remain-on-exit", "on"), ("history-limit", "5000")],
            window.options,
        )
        self.assertEqual(["even-horizontal"], window.layouts)

    def test_library_error_becomes_tmux_error(self):
        def broken_cmd(*_args, **_kwargs):
            raise RuntimeError("send failed")

        self.pane.cmd = broken_cmd
        with self.assertRaisesRegex(TmuxError, "send failed"):
            self.transport.send_literal("%7", "hello")

    def test_send_keys_is_literal_then_enter(self):
        prompt = "$meeting-participant role=proposer; text=$(not-a-shell)"
        self.transport.send_keys("%7", prompt)

        self.assertEqual(
            {
                "text": prompt,
                "enter": False,
                "suppress_history": False,
                "literal": True,
            },
            self.pane.inputs[0],
        )
        self.assertEqual(
            ("send-keys", ("-H", *[f"{byte:02x}" for byte in prompt.encode("utf-8")])),
            self.pane.commands[0],
        )
        self.assertEqual({"key": "Enter"}, self.pane.inputs[1])

    def test_send_hex_stays_byte_faithful(self):
        # Takeover bytes are forwarded verbatim as key bytes — never rewritten
        # into pastes — so the pane sees exactly what the human typed.
        self.transport.send_hex("%7", "7820790d")
        self.assertEqual("x y\r", "".join(item["text"] for item in self.pane.inputs))
        self.assertEqual({"send-keys"}, {cmd for cmd, _args in self.pane.commands})

    def test_send_literal_does_not_submit(self):
        self.transport.send_literal("%7", "draft")
        self.assertEqual(1, len(self.pane.inputs))
        self.assertFalse(self.pane.inputs[0]["enter"])
        self.assertTrue(self.pane.inputs[0]["literal"])

    def test_a_long_line_is_sent_in_several_calls_byte_for_byte(self):
        # tmux rejects one huge `send-keys` with "command too long", so a long
        # line (an agent typing a whole expression) goes over in pieces — the
        # pane must still receive exactly the same bytes, in the same order.
        line = "def quick_sort(items, key=None): return sorted(items, key=key)  # " + "x" * 4000
        self.transport.send_literal("%7", line)
        sends = [args for command, args in self.pane.commands if command == "send-keys"]
        self.assertGreater(len(sends), 1)
        self.assertTrue(all(len(args) - 1 <= self.transport.SEND_HEX_CHUNK_BYTES for args in sends))
        self.assertEqual(line, "".join(item["text"] for item in self.pane.inputs))

    def test_send_keys_rejects_multiline_input(self):
        with self.assertRaises(TmuxError):
            self.transport.send_keys("%7", "first\nsecond")
        self.assertEqual([], self.pane.inputs)

    def test_send_enter_targets_exact_pane(self):
        self.transport.send_enter("%7")
        self.assertEqual([{"key": "Enter"}], self.pane.inputs)

    def test_special_keys_use_tmux_key_names(self):
        self.transport.send_special("%7", "Backspace")
        self.transport.send_special("%7", "Delete")
        self.assertEqual("BSpace", self.pane.inputs[0]["text"])
        self.assertFalse(self.pane.inputs[0]["literal"])
        self.assertEqual("DC", self.pane.inputs[1]["text"])
        self.assertFalse(self.pane.inputs[1]["literal"])

    def test_workspace_command_text_is_safe_and_exact(self):
        self.assertEqual("pwd", workspace_command_text("pwd"))
        # Starting an editor is an ordinary bash command line: it passes
        # through verbatim and takes no separate path argument.
        self.assertEqual(
            "vim -Nu NONE -n '/tmp/work space/note.md'",
            workspace_command_text("vim -Nu NONE -n '/tmp/work space/note.md'"),
        )
        with self.assertRaises(TmuxError):
            workspace_command_text("bash", "notes.md")
        with self.assertRaises(TmuxError):
            workspace_command_text("vim", "notes.md")

    def test_menu_choice_is_literal_and_validated(self):
        self.transport.send_menu_choice("%7", "2")
        self.assertEqual("2", self.pane.inputs[0]["text"])
        self.assertTrue(self.pane.inputs[0]["literal"])
        self.assertEqual({"key": "Enter"}, self.pane.inputs[1])
        with self.assertRaises(TmuxError):
            self.transport.send_menu_choice("%7", "yes")

    def test_capture_uses_full_joined_history_and_exact_pane(self):
        content = self.transport.capture("%7")
        self.assertEqual("visible screen", content)
        self.assertEqual(
            ("capture-pane", ("-p", "-J", "-S", "-")),
            self.pane.commands[0],
        )

    def test_cleanup_targets_only_valid_exact_session(self):
        session = FakeSession("remotebuddy-demo", self.server)
        self.server.session_map[session.session_name] = session
        self.assertTrue(self.transport.kill_session("remotebuddy-demo"))
        self.assertTrue(session.killed)
        self.assertFalse(self.transport.kill_session("remotebuddy-demo"))
        with self.assertRaises(TmuxError):
            self.transport.kill_session("remotebuddy-demo;other")

    def test_creates_one_fixed_size_workspace_pane(self):
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            pane_id = self.transport.create_workspace_session(
                "remotebuddy-shared", workdir
            )

        self.assertEqual("%11", pane_id)
        created = self.server.new_session_calls[0]
        self.assertEqual(120, created["x"])
        self.assertEqual(36, created["y"])
        self.assertEqual(str(workdir.resolve()), created["start_directory"])
        self.assertIn("--noprofile", created["window_command"])
        self.assertIn("CONDA_DEFAULT_ENV=remotebuddy", created["window_command"])
        self.assertEqual("remotebuddy", created["environment"]["CONDA_DEFAULT_ENV"])
        window = self.server.session_map["remotebuddy-shared"].active_window
        self.assertEqual(
            [
                ("allow-rename", "off"),
                ("remain-on-exit", "on"),
                ("history-limit", "5000"),
                ("window-size", "manual"),
            ],
            window.options,
        )
        self.assertEqual([], window.splits)

    def test_visible_capture_preserves_physical_rows_and_targets_exact_pane(self):
        self.assertEqual("visible screen", self.transport.capture_visible("%7"))
        self.assertEqual(("capture-pane", ("-p", "-N")), self.pane.commands[0])
        self.assertEqual(
            {
                "dead": "0",
                "command": "bash",
                "path": "/tmp/workspace",
                "cursor_x": "4",
                "cursor_y": "7",
                "pane_width": "120",
                "pane_height": "36",
            },
            self.transport.pane_info("%7"),
        )
        self.assertTrue(self.transport.pane_alive("%7"))
        self.pane.dead = "1"
        self.assertFalse(self.transport.pane_alive("%7"))

    def test_workspace_commands_and_keystrokes_are_structured(self):
        self.transport.send_workspace_command(
            "%7", "vim -Nu NONE -n '/tmp/workspace/a;$(x).md'"
        )
        self.assertEqual(
            "vim -Nu NONE -n '/tmp/workspace/a;$(x).md'",
            self.pane.inputs[0]["text"],
        )
        self.assertTrue(self.pane.inputs[0]["literal"])
        self.assertEqual({"key": "Enter"}, self.pane.inputs[1])

        self.pane.inputs.clear()
        self.transport.send_tokens(
            "%7",
            [
                {"kind": "literal", "value": "hello $(still-literal)"},
                {"kind": "key", "value": "Escape"},
            ],
        )
        self.assertTrue(self.pane.inputs[0]["literal"])
        self.assertEqual("Escape", self.pane.inputs[1]["text"])
        self.assertFalse(self.pane.inputs[1]["literal"])
        with self.assertRaises(TmuxError):
            self.transport.send_tokens("%7", [{"kind": "key", "value": "F12"}])

        self.pane.inputs.clear()
        self.transport.send_workspace_command("%7", "python", "/tmp/workspace/snake.py")
        self.assertEqual("python3 -- /tmp/workspace/snake.py", self.pane.inputs[0]["text"])


if __name__ == "__main__":
    unittest.main()
