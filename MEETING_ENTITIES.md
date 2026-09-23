# The entity interfaces — what each process exports, written in viba

Four processes, and therefore four surfaces: the meeting server, the actor, the
assistant and the critic. This chapter writes those surfaces the way
`MEETING_ADT.md` writes the other two — `meeting_core.viba` for the stable types,
`meeting_sdk.viba` for the data interface — and it follows `viba-style.md`:
structure carries what structure can carry, every field and argument is tagged,
a chain's first element is its result, and the `{ ... }` at the end is the hint
an implementation is written from.

Two things this chapter deliberately does not do. It does not describe a
transport: a REST call, a Redis message and an in-process call are three
bindings of the same viba api, and nothing below names any of them. And it does
not give the participants bespoke apis of their own: an actor, an assistant and
a critic are all "a member the meeting can hand the floor to, that reads the
record and appends to it" — what separates them is which records they may write
(role, in `meeting_core`) and what they hold privately (the critic its rules, the
assistant its cadence), neither of which is a cross-entity interface.

## The meeting server — meeting_server.viba

```viba
import meeting_core as mc

MeetingId = str
MemberName = str
Seq = int                      # position in the record: the record's own identity

# One thing that happened, in the one place where things happen. The content is
# whatever the member wrote; the envelope is the server's.
Record =
  Object
  * $seq Seq
  * $author mc.UserName
  * $time mc.datetime
  * $content mc.ChatMessage | mc.ScoreMessage | mc.ScreenContent | mc.TerminalControlReference

# Who may write to the shared terminal now, and until when. The fact is a
# control record; this is the right the server hands out so that only one member
# drives the pane at a time.
Floor =
  Object
  * $member MemberName
  * $taken_at mc.datetime
  * $until mc.datetime

# The record's id: every judgement, every citation and every replay is addressed
# by it, and it is never reused.
RecordId = Seq

Ids =
    mc.Result[list[MeetingId]]
  <- $ctx MeetingId
  <- { the meetings this server holds, newest first }

Open =
    mc.Result[MeetingId]
  <- $env Environment
  <- $task str
  <- { start a meeting whose first record is the human's task }

Records =
    mc.Result[list[Record]]
  <- $env Environment
  <- $meeting MeetingId
  <- $after Seq
  <- { every record after that position, in order — the only way to read the meeting }

Append =
    mc.Result[RecordId]
  <- $env Environment
  <- $meeting MeetingId
  <- $member MemberName
  <- $content mc.ChatMessage | mc.ScoreMessage | mc.ScreenContent | mc.TerminalControlReference
  <- { write one record as that member and hand back its position; the server is the only writer }

Resolve =
    mc.Result[mc.ChatMessage | mc.ScoreMessage | mc.ScreenContent | mc.TerminalControlReference]
  <- $env Environment
  <- $meeting MeetingId
  <- $reference mc.ContentReference
  <- { the record that reference addresses, read back through its own identity }

Claim =
    mc.Result[Floor]
  <- $env Environment
  <- $meeting MeetingId
  <- $member MemberName
  <- { hand that member the terminal, or say why it may not have it }

Release =
    mc.Result[nil]
  <- $env Environment
  <- $meeting MeetingId
  <- $member MemberName
  <- { give the terminal back; the member that held it must be the one asking }

Next =
    mc.Result[Record]
  <- $env Environment
  <- $meeting MeetingId
  <- $after Seq
  <- { wait for the next record past that position, or say that the meeting ended }
```

The three participants call exactly these five: `Records` (read), `Append` (say),
`Resolve` (recover the thing a reference points at), `Claim`/`Release` (hold the
terminal), plus `Next` when they would rather be woken than poll. Nothing in
this surface knows what a rule is, what code is, or who is clever: those live
inside the process that holds them.

## What each participant holds privately

| Process | Private to it | Writes to the record as |
|---|---|---|
| actor | its plan, its terminal actions | chat (`mc.ChatMessage`), the terminal's frames (`mc.ScreenContent`) |
| assistant | its cadence, the rules it checks, its wording | chat, and the checks it ran |
| critic | its rules, how it resolves a case, its measurements | chat, scores (`mc.ScoreMessage`), the checks it ran |

A check is written with `Append` like anything else: a rule's verdict is a
record, so it can be cited, replayed and argued with; nothing about it is a
side channel.

## Bindings

- **REST over HTTP** — one route per api above, the result and its arguments as
  JSON: `POST /meetings` (`Open`), `GET /meetings` (`Ids`),
  `GET /meetings/{id}/records?after=` (`Records`), `POST /meetings/{id}/records`
  (`Append`), `POST /meetings/{id}/resolve` (`Resolve`),
  `POST /meetings/{id}/floor` / `DELETE /meetings/{id}/floor`
  (`Claim`/`Release`), `GET /meetings/{id}/next?after=` (`Next`).
- **Redis pub/sub** — the same `Next` as a message: the server publishes a
  meeting's new record on `meeting:{id}` after each `Append`, and a mention on
  `meeting:{id}:member:{name}`. A published message is a doorbell, never the
  content: whoever it wakes still calls `Records` to read what happened.
- **In process** — the same functions, called directly; the two bindings above
  exist so a participant can live in another process, in another language, or on
  another machine.
