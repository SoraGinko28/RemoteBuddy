# RemoteBuddy

One person and one agent share a single terminal pane: the agent sees the conversation and the
visible screen, and answers one structured action at a time. A **meeting** is what one command
makes of that arrangement.

`rb` is the command. Its two subcommands share the same meeting data (`<workspace>/.meeting/`):

```
rb cli "写一个快速排序，放到 quicksort.py"    run one meeting from one prompt, then stop
rb web --workspace ~/work                   serve a page over that workspace's meetings
```

What a meeting *does* — which roles are in it, which steps a ticket runs, which conditions the
critic applies — is not in this repository. It is a **norm**, written in viba, in its own
repository (`RbNorm`, the norm under its `rb_norm/` package) next to this tree. This repository is
the facility: it reads the norm, runs its programs, and records every answer.

## Install

The quickest path is the deploy script:

```bash
./deploy.sh          # conda env, pinned dependencies, doctor checks, then the page
./deploy.sh --check  # deploy and verify only
```

By hand: create the environment, install the pinned dependencies, then write the two ignored
configuration files from their examples.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp model_api/config.example.toml model_api/config.toml
cp model_api/auth.example.json model_api/auth.json
chmod 600 model_api/auth.json          # keys live here, one per provider
```

Put `rb` on `PATH`:

```bash
cat > ~/.local/bin/rb <<'SH'
#!/bin/sh
exec <this tree>/.venv/bin/python <this tree>/rb.py "$@"
SH
chmod +x ~/.local/bin/rb
```

Without the entry point, `<this tree>/.venv/bin/python <this tree>/rb.py cli "…"` is the same
command.

## `rb cli`

One prompt, one meeting, run to a stop, then exit. There is no TUI and nothing waits for input.

```bash
rb cli "写一个快速排序，放到 quicksort.py"
```

```
PROMPT                               the prompt; several words are joined, `-` reads stdin
--workspace DIR                      where the meeting runs; default: the current directory
--meeting-id ID                      the meeting's name; default: generated
--resume MEETING                     carry on the meeting whose data directory this is
--harness NAME                       default backend for new sessions: claude, responses,
                                     codex, trae (aliases accepted)
--norm-repo-path DIR                 the norm repository, or its rb_norm/ package
--norm-commit-id SHA                 read that version of the norm
--no-interference-rule-path FILE     the viba program that decides what counts as interference
--no-interference-rule-commit-id SHA read that version of the rule
--json                               print the one line as a JSON object
-h / -V                              help / version
```

The only line it writes to stdout is the command that shows this meeting:

```
rb web --workspace /Users/…/work
```

That command can be run right away, while the meeting is still going. With `--json` the line is
`{"type":"web","command":…,"meeting_id":…,"workspace":…}`. Errors go to stderr, one sentence; the
exit code says what happened:

```
0   the meeting ran and finished
1   no subcommand (the help was printed)
2   bad arguments, or the meeting could not start
3   the flow verdict was false: mgmt_critic says the meeting did not finish, and the problems it
    named are on stderr
4   somebody interfered, so the meeting was stopped
```

Everything a meeting writes stays under `<workspace>/.meeting/<meeting-id>/`: the journal
(`streams/events.jsonl`), the conversation (`conversation.md`), the case with every answer
(`store/`), the tickets and their attempts (`tickets/`, `ticket_progress/`), and `rb.json`, this
driver's own side note — which norm, which version, which rule, who interfered.

`--resume` takes the meeting's data directory and carries it on: with a prompt it says one more
thing, without one it picks up the tickets that did not finish.

### Which norm, and which version

`--norm-repo-path` names a repository or the norm directory inside it. `--norm-commit-id` pins a
version: the tree of that commit is exported to a temporary directory, so an edit in the working
tree cannot change the meeting under it, and the directory is removed afterwards. Without a commit
the directory as it stands is read and no version is recorded. With neither, the norm is looked
for at `../RbNorm/rb_norm/`; `REMOTEBUDDY_NORM` points elsewhere.

A directory that does not look like a norm — no `ticket_programs/`, `intake/` or `acceptance/` —
is refused before the meeting opens.

### What stops a meeting

While the meeting runs, `rb cli` watches the journal and the driver lock. It stops when somebody
else writes to the meeting: a journal line this run did not write, a line the human typed in
`rb web`, or another process taking the driver. The rule that decides this is itself a viba program
(`--no-interference-rule-path`); the built-in one treats everything this run did not do as
interference, and the rule is a program, so a meeting can be told to tolerate any of it.

## Writing the norm behind `rb cli`

A norm is a directory: one table of members, viba programs in three folders, an optional fourth,
and the type document they are written against. It holds no task of its own — the prompt arrives
at the meeting, not in the norm.

```
rb_norm/
  members                  who is in the meeting, one line each
  intake/*.viba            is this prompt work, and what has to be delivered
  ticket_programs/*.viba   what one ticket runs, step by step
  acceptance/*.viba        the conditions the tech critic applies to the work
  constitution.viba        the types the norm is written against; a document, not a program
  due/*.viba               optional: an extra "is it time for this ticket"
```

### `members`

One member per line: its name, the api it implements, and how to find it.

```
actor        actor_api         agent
tech_critic  tech_critic_api   tech_critic
mgmt_critic  mgmt_critic_api   mgmt_critic
assistant    assistant_api     assistant
```

`agent` is the meeting's agent — in a real meeting it does the work. `tech_critic` runs the
acceptance conditions locally; `mgmt_critic` checks the flow locally; `assistant` issues tickets
and says the closing line. `scripted <module>:<function>`, `dummy_actor` and `dummy_tech_critic`
are stand-ins for tests; `process <command…>` starts another process that takes tickets itself.

### `intake/`

Every program in `intake/` answers one thing: the name of the file this prompt has to end up as. A
prompt is work when one of them answers a name; when none does, the meeting just talks to the agent
and no ticket is issued. A norm can hold several, for several kinds of work.

### `ticket_programs/`

One program is one ticket, and its steps are calls on the role apis. `do_the_work.viba` in `RbNorm`
carries the whole flow in one ticket: greet, draft, report, judge, revise, report again, judge
again, the flow check, and the closing note.

```viba
greeted = actor.acknowledge << $env ($sub_env << args.env << "greeted") << $about (...)
drafted = actor.do_step     << $env ($sub_env << args.env << "drafted") << $task (...) << $after greeted
...

flow = mgmt_critic.check << $env ($sub_env << args.env << "flow") << $judged judged_revision

__impl__ = assistant.closing_note
  << $env ($sub_env << args.env << "closing")
  << $result revised << $verdict judged_revision << $flow flow
```

Each step writes the address its own name gives, on its own sub-environment. Which member takes a
step is decided by the role api that step calls — the actor does work, `tech_critic` judges it,
`mgmt_critic` checks the flow, the assistant closes. A ticket may be handed out when every address
it reads is answered and none of its own answers is; a program that reads an address it writes
never runs.

`due/<ticket>.viba`, where present, adds one more condition: it reads the case's values and answers
whether this ticket is due now.

### `acceptance/`

One program is one condition: an environment in, a bool out. It names no meeting and no version of
the code — what is under judgement is a snapshot in the case. The operators a condition may call
belong to the `tech_critic` side.

### The interfaces

Which operators a role has is declared in `RbApiDeclare` — `rb_api/actor_api.viba` and one file per
role — not in this repository and not in the norm: both take it as a submodule at the same commit.
A norm may add its own `<role>_api.viba` for a new role; it may not shadow a declared one.

## `rb web`

```bash
rb web --workspace ~/work [--host 127.0.0.1] [--port 0]
```

Serves the workspace's page: every meeting in it, running or finished, with the conversation, the
shared terminal, the ticket board and the record. The workspace defaults to the current directory.
It binds to loopback only and prints its URL. This is the page `rb cli` tells you to open; the
meeting runs whether or not the page is up.

## Backends

`model_api/config.toml` selects one backend per meeting:

- `responses` — `POST /responses` only; needs `OPENAI_API_KEY`.
- `codex-app-server` — the Codex CLI's own login, one ephemeral session per work cycle.
- `claude-code` — Claude Code with its built-in tools disabled and one MCP server that exposes
  `remotebuddy_action`; needs `DEEPSEEK_API_KEY`.
- `trae-agent` — the TraeCode CLI, which speaks the Codex App Server protocol.
- `trae-acp` — the same CLI over ACP. Any ACP-speaking agent can sit behind this entry: point
  `[harnesses.trae-acp] binary` at it, for example a one-line wrapper around `dsh --profile acp`.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

Two tmux tests expect the `remotebuddy` conda environment (`CONDA_DEFAULT_ENV`) and fail without it.

## More

`RB.md` is the manual for `rb` (Chinese), `tickets/README.md` maps the harness, and
`MEETING_ADT.md` is the design of the meeting data.
