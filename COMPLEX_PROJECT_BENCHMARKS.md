# RemoteBuddy Complex Project Benchmarks

These benchmarks exercise the current shared Bash, Vim, and Python workflow.
Each project must be created inside one meeting workspace, executed in the
shared terminal, and completed only after its visible self-tests pass.

## 1. Dependency Graph Scheduler

Build `dependency_scheduler.py` with:

- task registration with dependencies and integer duration;
- deterministic topological ordering;
- missing-dependency validation and cycle reporting;
- earliest start and finish times for unlimited workers;
- critical-path reconstruction;
- self-tests for a diamond graph, disconnected tasks, missing dependencies,
  deterministic tie breaking, and a cycle.

The script must print `SCHEDULER TESTS PASSED`.

## 2. Formula Spreadsheet Engine

Build `formula_sheet.py` with:

- A1 cell names and literal numeric cells;
- formulas containing `+`, `-`, `*`, `/`, parentheses, and cell references;
- `SUM(A1:A3)` range evaluation;
- dependency-aware recomputation after a source cell changes;
- clear invalid-reference, division-by-zero, syntax, and cycle errors;
- self-tests for precedence, ranges, chained dependencies, recomputation, and
  every error class.

The script must print `SPREADSHEET TESTS PASSED`.

## 3. Event-Sourced Task Board

Build `event_task_board.py` with:

- events for create, rename, assign, move, tag, and close;
- deterministic replay into `todo`, `doing`, and `done` columns;
- validation for duplicate IDs and illegal transitions;
- JSON Lines serialization and restoration without external packages;
- snapshots at any sequence, per-user workload, tag filtering, and an audit
  trail for one task;
- self-tests for replay equivalence, historical snapshots, filtering,
  workload, serialization, and invalid events.

The script must print `EVENT BOARD TESTS PASSED`.

## Shared Acceptance Criteria

- One retained tmux pane is the only execution environment.
- The Agent sends a concrete acknowledgement before acting.
- Chat progress never claims a terminal change before Screen evidence exists.
- The file is created through Vim and run through the structured Python action.
- The event stream contains no `error`, `api_error`, `phase_mismatch`, or
  rejected terminal action.
- An independent Python 3.12 execution produces the same success marker.

## 2026-08-26 Baseline Results

| Run | Outcome | Evidence |
|---|---|---|
| `bench-event-board` | Functional pass | 53 lines, 2,470 bytes, 1,261 Screen events, zero rejected actions, zero errors, retained one-pane tmux session, and an independent replay/query/JSONL black-box test passed. |
| `20260826-110835-f622e2` | Diagnostic pass | The scheduler eventually printed `SCHEDULER TESTS PASSED` and passed an independent Python 3.12 run after two real syntax failures and three Vim repair cycles. It is not a clean protocol pass because earlier actions were rejected. |
| `bench-scheduler-final` | Failed | Complex repair repeatedly requested unsafe Vim regex substitutions, then a recovery App Server turn timed out. No stale action was replayed. |
| `bench-log-v2` | Failed black-box acceptance | The Agent claimed `LOG ANALYZER TESTS PASSED`, but the marker was absent from Screen and the file contained no assertions. Independent testing found blank lines incorrectly reported as malformed input. The correction cycle timed out before editing. |
| `bench-formula-v2` | Failed | The Agent reached real Python tracebacks and attempted local repair, but unsupported/incorrect Vim edit sequences caused repeated rejection and later edits did not persist reliably. Final file still had a syntax error. |

System observations from these runs:

- Event appends originally scanned and rehashed the whole growing stream for every Screen frame. Tail-based sequence allocation and deferred stream hashing removed the O(n squared) path.
- A stop request during long typing originally took about 90 seconds. Publishing `stop.request` before lock contention reduced the measured response to 0.125 seconds.
- The action budget is 60 actions or 15 minutes per human request. Large Vim actions generated more than 1,000 replayable Screen changes without using paste.
- The Harness now requires a completed Vim or Python action before accepting `progress`, requires a claimed uppercase `TESTS PASSED` marker to appear in the latest Screen before accepting `done`, and forces the first reply to be an acknowledgement rather than a premature question.
- Safe Vim support now includes bounded navigation/deletion commands and literal-only substitutions, while Shell execution, expression substitutions, command pipes, raw control characters, and regex-heavy repair remain rejected.
