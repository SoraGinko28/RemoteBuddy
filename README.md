# RemoteBuddy Workspace Demo

RemoteBuddy v2 is a local collaboration workspace for one human and one Agent. Both participants share one tmux pane. The Agent receives new chat events plus the latest visible terminal screen, then returns one structured action at a time.

The application uses Python 3.12 in the `remotebuddy` Conda environment plus `libtmux` for typed terminal control. Responses remains the default backend. Optional Codex App Server and Claude Code harnesses are managed as isolated child processes without giving either harness a second terminal.

## Configure

The quickest path is the deploy script, which creates the conda env, installs
pinned dependencies, runs the doctor checks, and starts the Web UI:

```bash
./deploy.sh          # deploy and launch
./deploy.sh --check  # deploy and verify only
```

Or manually: create the project environment once with conda-forge only, install
the pinned Python dependency, then activate it:

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

The Codex backend authenticates purely through the local CLI login — `codex login` with an API key or a ChatGPT-plan (subscription) account both work, and `model_api/auth.json` is only required for the `responses` backend and the DeepSeek provider. The New Session dialog can also leave the model to the Codex CLI's own default (recommended for ChatGPT-plan logins, which may not have access to a hard-coded model id); such sessions store an empty model and omit the `model` parameter from every Codex turn.

`trae-agent` drives ByteDance's internal TraeCode CLI (`traecli`), which implements the same Codex App Server protocol, so it shares the whole codex-app-server integration: ephemeral threads, the private read-only working directory, and the protocol-violation filter. It authenticates via `traecli login` (Trae account), never needs a stored API key, and also supports leaving the model to the CLI's own default. A custom executable can be set with `[harnesses.trae-agent] binary = "/path/to/traecli"`.

`claude-code/deepseek` requires Claude Code v2.1.248 or newer and the pinned Python MCP dependency. Install or update Claude Code using the [official setup instructions](https://code.claude.com/docs/en/setup), then configure `DEEPSEEK_API_KEY`. When `binary = "claude"`, RemoteBuddy checks `PATH` and also discovers the standard Claude Desktop macOS install locations; a custom executable can be set with an absolute `binary` path. RemoteBuddy starts Claude with restricted and bare modes, disables all built-in tools, loads one explicit local MCP server, and injects the key only into the child environment. The MCP server exposes only `remotebuddy_action`; RemoteBuddy validates and executes that action through the same runner used by the other backends. There is no automatic fallback when Claude Code, MCP, or the DeepSeek key is unavailable.

The Claude integration currently supports macOS and POSIX Linux. Windows support is deferred together with the tmux transport.

Check the environment:

```bash
python3 demo.py doctor
```

## Run

```bash
python3 web.py
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). The service never binds to a non-loopback address.

New sessions are created from the Web interface. The CLI manages existing v2 sessions:

```bash
python3 demo.py status <meeting-id>
python3 demo.py stop <meeting-id>
python3 demo.py cleanup <meeting-id>
```

## Session Model

A session lives in a **workspace** — one directory, the project being worked on. Every byte of
meeting data goes under `<workspace>/.meeting/<meeting-id>/` and the Agent's terminal opens in the
workspace root itself, so several sessions can share one project:

```text
<workspace>/.meeting/<meeting-id>/
  meeting.json
  conversation.md
  streams/events.jsonl
  .agents/skills/workspace-collaborator/SKILL.md
```

The workspace is the project root by default, or whatever `--workspace DIR` names. The new-session
dialog takes a directory — type it, or press 选择目录… for the macOS folder dialog (Finder), because
a browser cannot hand back an absolute path from its own directory picker:

```text
POST /api/choose-directory {"directory": "<current value>"}   the system picker; an empty
                                                              `directory` back means cancelled
```

The directories used so far are recorded in `<workspace>/.meeting/workspaces`, so a session opened
in another project stays on the list across restarts. The page groups the list by workspace and
names each group by that directory's basename.

`MeetingStore` still reads the older `runs/<meeting-id>/` layout (with a fresh `workspace/` inside
the meeting directory); the Web service no longer writes it.

Chat takes images: paste into the composer or use the paperclip. The bytes are stored as a file of
the meeting (`attachments/image-<n>.<ext>`) and the message records only the name and the content
hash, so the journal stays text. A picture goes to the model as a picture — `input_image` on the
Responses API, an image block for Claude Code, `localImage` for Codex / Trae — so understanding it is
the model's own ability rather than something the Agent has to approximate with a command-line tool.

`events.jsonl` is the replay source of truth. It contains globally ordered `chat`, `screen`, `action`, `control`, `state`, and `error` events. `conversation.md` is rebuilt only from chat events.

The default model client uses only `POST /responses`. It does not require a `/conversations` endpoint: the local event log is sent as bounded chat and action history with each request, together with the latest shared terminal screen. This works with Responses-only gateways such as a proxy whose configured base URL is `https://rust.cat`. Stateful Codex and Claude Code backends receive one fresh ephemeral session per active work cycle. The first turn receives reconstructed local history and later turns receive only new events plus the latest screen; vendor transcripts are never used for recovery.

The Agent works only through the shared terminal. Structured `pwd`, `cd`, and `python` commands keep workspace path resolution; beyond those, the Agent may run general bash command lines with two restricted classes. Deletion-class commands (`rm`, `rmdir`, `unlink`, `shred`, `truncate`, `dd`, `mkfs`, `find -delete`, `rsync --delete`, `git clean`/`git rm`) are rejected wherever they appear in a chained or piped line, and so is anything that would leave the shared pane's visible foreground (background `&` jobs, `nohup`, `setsid`, `disown`, `tmux`, `screen`, `open`, `osascript`, `exec`). One keystroke action types into whatever program holds the foreground — the harness keeps no per-program rules, so how to drive an editor is the Agent's own business, guided by the Skill; observation and visible chat messages complete the action set. A `chat` or `done` action may also attach content references that quote earlier `chat` or `screen` events by sequence number and character region — the same immutable reference fields (excerpt plus source hash) that human messages use. Structured command paths are resolved inside the session workspace; general bash commands are not path-confined, so this remains an application policy, not an operating-system sandbox; a person attaching directly to tmux is outside that boundary.

Agent `done` means "wait for the human." Only the human can complete a session. Completion retains tmux for inspection; stop interrupts the session and removes tmux.

Any earlier chat or screen content, including the human's own messages, can be quoted into the conversation as a reference.

Schema v1 meeting files are not changed, migrated, or deleted. The v2 Web and CLI interfaces intentionally hide them.

## Tickets

A meeting runs on tickets. The norm itself is viba and lives in its own repository (`RbNorm`, next
to this tree, the norm under its `rb_norm/` package; `REMOTEBUDDY_NORM` points elsewhere): the
ticket programs, the requests that decide which human lines are work, and the rules the critic
judges by. The program carries none of it.

The interfaces between the two (which operators actor, assistant and critic have) live in a third
repository, `RbApiDeclare`, and **both repositories depend on it as a git submodule**
(`RemoteBuddy-core/RbApiDeclare/`, `RbNorm/RbApiDeclare/`) at the same commit. The facility reads its
own copy, checks the norm's copy is on that commit, puts the directory on every ticket's
`$VIBA_PATH`, and records the version per handler in the ticket's
`$handler_name2api_declare_repo_commit_id`.

What the facility writes into a meeting directory is declared in this repository
(`tickets/ticket.viba`): a ticket (`Ticket`), one attempt at it (`TicketStage`), and a ticket's whole
progress (`TicketProgress = AppendOnlyList[TicketStage]`). The program fills those in and reads them
back by that declaration, and refuses on the spot when the two disagree — no member list is written
in Python.

`tickets/` is the harness: a ticket is one call written on the file system
(`<meeting>/tickets/<uid>/ticket.viba`), its progress is one file per attempt
(`<meeting>/ticket_progress/<uid>/<time>-<handler>.viba`), and the answers land in the case under
`<meeting>/store/<case>/`. The assistant receives the human message, works out which ticket is due
from the values in the case, and writes it; every member of the table (`<norm>/members`) takes the
tickets whose newest attempt is not its own, runs them with its own capability table, and appends
one attempt. A ticket is done when one of its attempts answered `$ok`.

The page shows and drives it:

```text
GET  /api/meetings/<meeting-id>/tickets           the board: which ticket is due, what each one
                                                  reads, who answered, every attempt, the record
```

`tickets/README.md` maps every part.

## Test

```bash
.venv/bin/python -m pytest tests -q
```

Two tmux tests (`test_creates_one_fixed_size_workspace_pane`, `test_creates_two_horizontal_codex_panes`)
expect the `remotebuddy` conda environment (`CONDA_DEFAULT_ENV`) and fail without it.
