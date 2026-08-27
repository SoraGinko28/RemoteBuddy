# RemoteBuddy Workspace Demo

RemoteBuddy v2 is a local collaboration workspace for one human and one Agent. Both participants share one tmux pane. The Agent receives new chat events plus the latest visible terminal screen, then returns one structured action at a time.

The application uses Python 3.12 in the `remotebuddy` Conda environment plus `libtmux` for typed terminal control. Responses remains the default backend. An optional Codex App Server experiment uses the locally installed and authenticated Codex CLI without giving Codex a second terminal.

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

`auth.json` must contain only an API key:

```json
{"api_key":"..."}
```

On macOS or Linux, restrict the auth file:

```bash
chmod 600 model_api/auth.json
```

`config.toml` accepts the original four-field profile and the Codex-style profile. In the latter, set `model_provider`, `model_reasoning_effort`, and `[model_providers.<name>] wire_api = "responses"`; the provider `base_url` is used for `POST /responses`. Other Codex settings such as `notify` are validated but never executed by RemoteBuddy.

Choose the backend with these optional top-level fields:

```toml
agent_backend = "responses" # or "codex-app-server"
codex_dynamic_tools = false # experimental second phase; Codex backend only
```

`codex-app-server` uses `codex app-server --stdio`, the current Codex CLI login, an ephemeral thread, empty execution environments, a private read-only working directory, and the same RemoteBuddy action schema. Any Codex Shell, file, MCP, Web, approval, or sub-Agent event is treated as a protocol violation and is never forwarded to tmux.

Check the environment:

```bash
python3 demo.py doctor
```

## Run

```bash
python3 web_v2.py
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). The service never binds to a non-loopback address.

New sessions are created from the Web interface. The CLI manages existing v2 sessions:

```bash
python3 demo.py status <meeting-id>
python3 demo.py stop <meeting-id>
python3 demo.py cleanup <meeting-id>
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

The default model client uses only `POST /responses`. It does not require a `/conversations` endpoint: the local event log is sent as bounded chat and action history with each request, together with the latest shared terminal screen. This works with Responses-only gateways such as a proxy whose configured base URL is `https://rust.cat`. In App Server mode, each active work cycle gets a new ephemeral Codex thread; the first turn receives reconstructed local history and later turns receive only new events plus the latest screen.

The Agent can request only `pwd`, `cd`, `vim`, approved Vim keys, observation, or a visible chat message. Paths are resolved inside the session workspace. This is an application allowlist, not an operating-system sandbox; a person attaching directly to tmux is outside that boundary.

Agent `done` means "wait for the human." Only the human can complete a session. Completion retains tmux for inspection; stop interrupts the session and removes tmux.

Schema v1 meeting files are not changed, migrated, or deleted. The v2 Web and CLI interfaces intentionally hide them.

## Test

```bash
python3 -m unittest discover -s tests -v
```
