# The entity interfaces — what each process exports, written in viba

Four processes, and therefore four surfaces: the meeting server, the actor, the
assistant and the critic. Each one exports two things — the **concepts** it owns,
which are the types the others need in order to talk about what it does, and the
**api** it offers, which is the functions that make it work. One chapter per
entity, written the way `MEETING_ADT.md` writes its two (`meeting_core.viba` for
the stable types, `meeting_sdk.viba` for the data interface), and following
`viba-style.md`: structure carries what structure can carry, every member and
every argument carries a self-explaining tag, a chain's first element is its
result, and the `{ ... }` at the end of a chain is the hint an implementation is
written from.

What is *not* in a surface matters as much. A transport is not: a REST call, a
Redis message and an in-process call are three bindings of the same viba api, and
no chapter below names any of them. Neither is a private matter: the actor's
plan, the assistant's cadence and whose words it uses, the critic's rules and how
it resolves a case are held inside those processes, and they appear here only as
the concepts each process owns.

The one thing the four surfaces have in common is the record. An entity says what
it has to say by writing to it and reads what the others said by reading it, so
`meeting_server`'s concepts are the vocabulary the other three chapters are
written in, and go first.

## The meeting server — meeting_server.viba

```viba
import meeting_core as mc

# A meeting's identity: one conversation, addressable from anywhere — the record it
# is, the terminal it was held on, the files it wrote.
MeetingId = str

# A position in a meeting's record. Positions only grow and are never reused, so a
# position is what a reader remembers instead of the records themselves.
Seq = int

# One thing that happened, in the one place where things happen: an item of one of
# the meeting's four streams. The content carries its own author and its own moment,
# which is what lets a record be cited; the position is the server's, which is what
# lets it be addressed.
Record =
  Object
  * $seq Seq
  * $content (mc.ChatMessage | mc.ScoreMessage | mc.ScreenContent | mc.TerminalControlReference)

Ids =
    mc.Result[list[MeetingId]]
  <- $env Environment
  <- { the meetings this server holds, newest first }

Open =
    mc.Result[MeetingId]
  <- $env Environment
  <- $task str
  <- $members dict[str, mc.AgentRole]
  <- { start a meeting: the task is its first record, the roster is who may speak in
       it, and the human is a member of every meeting }

Records =
    mc.Result[list[Record]]
  <- $env Environment
  <- $meeting MeetingId
  <- $after Seq
  <- { every record after that position, in order — the only way to read a meeting }

Append =
    mc.Result[Seq]
  <- $env Environment
  <- $meeting MeetingId
  <- $member mc.UserName
  <- $content (mc.ChatMessage | mc.ScoreMessage | mc.ScreenContent | mc.TerminalControlReference)
  <- { write one record as that member and hand back its position; the server is the
       only writer, so a content that names its own author must name that member }

Claim =
    mc.Result[nil]
  <- $env Environment
  <- $meeting MeetingId
  <- $member mc.UserName
  <- { hand that member the terminal — only one member holds it at a time, and only
       the human and an actor may hold it — or say who holds it instead; taking it is
       written down, so the record shows when the terminal changed hands }

Release =
    mc.Result[nil]
  <- $env Environment
  <- $meeting MeetingId
  <- $member mc.UserName
  <- { give the terminal back; the member that holds it must be the one asking }

Next =
    mc.Result[Record]
  <- $env Environment
  <- $meeting MeetingId
  <- $after Seq
  <- { wait for the next record past that position, or say that the meeting ended }
```

Reading the *content* of a record — a message's text, the screen at a moment, the
file a reference names — is `meeting_sdk`'s api, already written in
`MEETING_ADT.md`; the server's own surface is the record and the floor. What a
record holds is exactly what a member wrote, so nothing in this surface knows
what a rule is, what code is, or who is clever: those live inside the process
that holds them.

## The actor — actor.viba

```viba
import meeting_core as mc
import meeting_server as ms

# What this actor is doing right now.
WorkState =
  Oneof
  | $idle nil                          # it has read everything the meeting says
  | $working nil                       # a turn is in flight: it has read the record and has
                                       # not yet said what it did

State =
    mc.Result[WorkState]
  <- $env Environment
  <- { what this actor is doing right now — the answer the others time their turns by }

Work =
    mc.Result[ms.Seq]
  <- $env Environment
  <- $after ms.Seq
  <- { one turn: read the record past that position, do what it asks on the terminal,
       say what it did, and hand back the position it got to; what it plans, and how
       it types, are its own }
```

## The assistant — assistant.viba

```viba
import meeting_core as mc
import meeting_server as ms

# When this assistant last looked at the record, and how long it may stay away: its
# own clock, and the only thing it keeps between looks.
Cadence =
  Object
  * $looked_at mc.datetime
  * $least_interval_seconds int

Watch =
    mc.Result[nil]
  <- $env Environment
  <- $least_interval_seconds int
  <- { keep the meeting under observation at least this often, and hand back when it
       ends; watching is this loop: look, wait for the doorbell, look again }

Look =
    mc.Result[ms.Seq]
  <- $env Environment
  <- $after ms.Seq
  <- { one look at the record past that position: hand the next move on, ask for what is
       still owed, or say the meeting is done — with the reasons it found in the record,
       and in this assistant's own words }
```

## The critic — critic.viba

```viba
import meeting_core as mc
import meeting_server as ms

# What one rule answered about one case.
Verdict =
  Oneof
  | $holds nil                         # the rule ran, and the case satisfies it
  | $objects str                       # the rule ran, and the case violates it: how to say
                                       # it, in words fit to repeat to whoever wrote it
  | $not_run str                       # the rule is what is wrong: why it could not run

# The identity of a rule set. Two judgments made under the same hash were made under
# the same rules, so a verdict can be replayed, and a rule set can be replaced without
# an older verdict escaping with it.
RuleSetVersion =
  Object
  * $rules_hash str
  * $rules list[str]

# What one rule said about one case: the material it judged, the rule that spoke about
# it, and the verdict itself.
Evaluation =
  Object
  * $case mc.ContentReference          # the material judged: which frame, which region of it
  * $rule str                          # which of the rules spoke
  * $rules RuleSetVersion              # the set that rule was read from
  * $verdict Verdict
  * $judge_name mc.UserName
  * $time mc.datetime
  * Appendix[{
      the case is material written under the case's own address: the evidence the rule read
      stays with the case, so the same judgment can be replayed and argued with
    }]

Rules =
    mc.Result[RuleSetVersion]
  <- $env Environment
  <- { the rules this critic judges by, and the identity of that set }

Review =
    mc.Result[list[Evaluation]]
  <- $env Environment
  <- $after ms.Seq
  <- { judge every case the record past that position brings this critic, and hand back
       what was said; a verdict reaches the record as a score — its tag names the rule,
       its reference is the case — so anyone can cite it }
```

A verdict is written with `Append` like anything else, and that is the point: it is
a record, so it can be cited, replayed and argued with, and nothing about it is a
side channel.

## What each entity owns

| Process | Concepts it owns | Writes to the record as | Keeps to itself |
|---|---|---|---|
| meeting server | `MeetingId`, `Seq`, `Record` | every record: it is the only writer | the meetings, and who holds the terminal |
| actor | `WorkState` | chat, and the terminal's frames | its plan, and how it types |
| assistant | `Cadence` | chat | whose words it uses, and when it looks |
| critic | `Verdict`, `RuleSetVersion`, `Evaluation` | chat, and scores | its rules, and how it resolves a case |

## Bindings

- **The record** is the one channel every entity shares: read with `Records` (or
  wait with `Next`), speak with `Append`.
- **REST over HTTP** — the server's api, one route per function: `GET /meetings`
  (`Ids`), `POST /meetings` (`Open`), `GET /meetings/{id}/records?after=`
  (`Records`), `POST /meetings/{id}/records` (`Append`),
  `POST /meetings/{id}/floor` and `DELETE /meetings/{id}/floor`
  (`Claim`/`Release`), `GET /meetings/{id}/next?after=` (`Next`).
- **Redis pub/sub** — the doorbell that saves a participant from polling: after
  each `Append` the server publishes the new position on `meeting:{id}`, and a
  mention on `meeting:{id}:member:{name}`. A published message is a doorbell,
  never the content: whoever it wakes still calls `Records` to read what happened.
- **In process** — `Work`, `Watch`, `Look` and `Review` are called directly by the
  process that runs them, against the same meeting over the same record. A
  participant is woken by the doorbell, and takes `$after` from its own memory:
  the assistant from its `Cadence`, the others from the position they last read.
