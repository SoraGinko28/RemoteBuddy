# RemoteBuddy Workspace Demo

RemoteBuddy v2 is a local collaboration workspace for one human and one Agent. Both participants share one tmux pane. The Agent receives new chat events plus the latest visible terminal screen, then returns one structured action at a time.

The application uses Python 3.12 in the `remotebuddy` Conda environment plus `libtmux` for typed terminal control. Responses remains the default backend. Optional Codex App Server and Claude Code harnesses are managed as isolated child processes without giving either harness a second terminal.

## Configure

Create the project environment once with conda-forge only, install the pinned
Python dependency, then activate it:

```bash
~/miniconda3/bin/conda create -n remotebuddy --override-channels -c conda-forge python=3.12 pip
~/miniconda3/envs/remotebuddy/bin/python -m pip install -r requirements.txt
source ~/miniconda3/etc/profile.d/conda.sh
conda activate remotebuddy
```

Create these two ignored files from the examples in `model_api/`:

```text
model_api/config.toml
model_api/auth.json
```

API keys can also be entered from the Web interface: the New Session dialog accepts an API key and an optional Base URL for the selected provider. The key is written to `model_api/auth.json` (chmod 600) and the Base URL override to the ignored `model_api/overrides.json`; neither value is ever returned to the browser.

`auth.json` accepts dedicated provider keys. The legacy single `api_key` alias is still supported by itself:

```json
{
  "OPENAI_API_KEY": "...",
  "DEEPSEEK_API_KEY": "..."
}
```

On macOS or Linux, restrict the auth file:

```bash
chmod 600 model_api/auth.json
```

`config.toml` accepts the original four-field profile, the Codex-style profile, and a provider-aware profile. Existing files require no migration. A provider-aware configuration can make all three supported combinations available:

```toml
default_backend = "responses"
default_provider = "openai"
request_timeout_seconds = 120
reasoning_effort = "low"

[providers.openai]
base_url = "https://api.openai.com/v1"
wire_api = "responses"
auth_key = "OPENAI_API_KEY"
model = "gpt-5.6-luna"

[providers.deepseek]
base_url = "https://api.deepseek.com/anthropic"
wire_api = "anthropic"
auth_key = "DEEPSEEK_API_KEY"
model = "deepseek-v4-pro[1m]"
fast_model = "deepseek-v4-flash"

[harnesses.claude-code]
binary = "claude"
```

`codex-app-server` uses `codex app-server --stdio`, the current Codex CLI login, an ephemeral thread, empty execution environments, a private read-only working directory, and the same RemoteBuddy action schema. Any Codex Shell, file, MCP, Web, approval, or sub-Agent event is treated as a protocol violation and is never forwarded to tmux.

`claude-code/deepseek` requires Claude Code v2.1.248 or newer and the pinned Python MCP dependency. Install Claude Code using the [official setup instructions](https://code.claude.com/docs/en/setup), then configure `DEEPSEEK_API_KEY`. RemoteBuddy starts Claude with restricted and bare modes, disables all built-in tools, loads one explicit local MCP server, and injects the key only into the child environment. The MCP server exposes only `remotebuddy_action`; RemoteBuddy validates and executes that action through the same runner used by the other backends. There is no automatic fallback when Claude Code, MCP, or the DeepSeek key is unavailable.

The Claude integration currently supports macOS and POSIX Linux. Windows support is deferred together with the tmux transport.

Check the environment:

```bash
python3 demo_v2.py doctor
```

## Run

```bash
python3 web_v2.py
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). The service never binds to a non-loopback address.

New sessions are created from the Web interface. The CLI manages existing v2 sessions:

```bash
python3 demo_v2.py status <meeting-id>
python3 demo_v2.py stop <meeting-id>
python3 demo_v2.py cleanup <meeting-id>
```

## Session Model

Each v2 session is stored under `runs/<meeting-id>/`:

```text
meeting.json
conversation.md
workspace/
streams/events.jsonl
.agents/skills/workspace-collaborator/SKILL.md
```

`events.jsonl` is the replay source of truth. It contains globally ordered `chat`, `screen`, `action`, `control`, `state`, and `error` events. `conversation.md` is rebuilt only from chat events.

The default model client uses only `POST /responses`. It does not require a `/conversations` endpoint: the local event log is sent as bounded chat and action history with each request, together with the latest shared terminal screen. This works with Responses-only gateways such as a proxy whose configured base URL is `https://rust.cat`. Stateful Codex and Claude Code backends receive one fresh ephemeral session per active work cycle. The first turn receives reconstructed local history and later turns receive only new events plus the latest screen; vendor transcripts are never used for recovery.

The Agent can request only `pwd`, `cd`, `vim`, approved Vim keys, observation, or a visible chat message. A `chat` or `done` action may also attach content references that quote earlier `chat` or `screen` events by sequence number and character region — the same immutable reference shape (excerpt plus source hash) that human messages use. Paths are resolved inside the session workspace. This is an application allowlist, not an operating-system sandbox; a person attaching directly to tmux is outside that boundary.

Agent `done` means "wait for the human." Only the human can complete a session. Completion retains tmux for inspection; stop interrupts the session and removes tmux.

Scores are human-only annotations. The Agent has no score action, score events are never shown to Agent prompts, and human-authored chat messages cannot be scored by anyone — while any earlier chat or screen content, including the human's own messages, can be quoted into the conversation as a reference.

Schema v1 meeting files are not changed, migrated, or deleted. The v2 Web and CLI interfaces intentionally hide them.

## Test

```bash
python3 -m unittest discover -s tests -v
```
