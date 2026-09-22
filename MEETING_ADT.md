# Meeting ADT

> This document is the formal ADT of the RemoteBuddy meeting model (the author's
> own text, kept in sync with the discussion). It describes the core logic of
> the system (the capability layer) only: a Workspace stores data, a Tree records
> lineage, an Image is a lineage snapshot, an Instance is a live line, and
> MeetingData is the fact carrier that can be cited, reviewed and replayed.
> Copying, snapshotting and deduplicating belong to the data layer and are not
> discussed here.

---

## The record layer

The meeting record itself. The types, assertions and notes below all live in
this layer.

```bash
import meeting_core as mc

MeetingData =
    DataClass
  * $meeting_uid str     # basically uuid
  * $meeting_name str
  * $sorted_stream_chunks list[StreamChunkDict]
  * $agent_name2role dict[str, mc.AgentRole]        # only grows inside a meeting: added on join, frozen once frozen
  * $file_kv_store mc.FileKvStore
  * $file_desktop FileDesktop
  * Appendix[{
      user_names = every mc.UserName that appears in any chunk
                     (screen.owner / chat.user_name / score.judge_name /
                      terminal_control.user_name)
                   ∪ every mc.Versioned.author in the file layer
                     (the three chains of file_kv_store / file_desktop's $reorder_indexes)
    }]
  * Assert[{ keys($agent_name2role) ⊆ user_names(this record) }]
  * Hint[{
      reply reminder: ∀ chat message m in the chat stream, if m carries
                 $at_user_name == P and role(P) == $passive_discriminator,
                 then ∃ a later record r (a chat or score produced by P)
                   : r carries an mc.ContentReference that resolves to m
    }]
  * Appendix[{
      owner_at(t) = the TerminalControl.user_name of the control interval
                     that contains t
                     (the interval starts at TerminalControl.start_time; absent
                      = nobody holds the terminal at that moment)
    }]

FileKvStore =
    Object
  * $file_id2file dict[str, mc.VersionedFile]

FileDesktop =
    DataClass
  * $workspaces mc.AppendOnlyList[FileWorkspace]
  * $reorder_indexes mc.Versioned[DesktopMemberOrder]
  * Assert[{
      names are unique within a level, so a path picks exactly one file:
        the $workspace_name of the $workspaces are pairwise different;
        inside one workspace, a fold's $fold_name and the $entry_name of a file
        hanging directly under it are pairwise different;
        inside one fold, the $entry_name of the files in $member_file_ids are
        pairwise different
    }]
  * Assert[{
      a $file_id appears at most once in the tree: an entry carries a name and
      an identity, so no two paths reach the same file (there are no links)
    }]
  * Appendix[{
      # the desktop tree shows the current state only: no mc.ContentReference
      # cites it by $version_time, so the tree carries no version chain (the only
      # things read at a moment are the three chains in file_kv_store);
      # $reorder_indexes records the time and author of the last reordering
    }]

DesktopMemberOrder =
    DataClass
  * $workspace_indexes mc.ReadOnlyList[WorkspaceMemberOrder]
  * Assert[{
      the $workspace_index in $workspace_indexes are exactly
          a permutation of 0 .. len(MeetingData.file_desktop.workspaces) - 1
    }]

WorkspaceMemberOrder =
    DataClass
  * $workspace_index int
  * $file_indexes mc.ReadOnlyList[($file_index int, mc.Optional[FoldMemberOrder])]
  * Assert[{
      ws = MeetingData.file_desktop.workspaces[$workspace_index]
      { $file_index | ($file_index, _) ∈ $file_indexes } == 0 .. len(ws.member_files) - 1
      and for every ($file_index, Some(fo)):
          fo.$member_indexes == 0 .. len((ws.member_files[$file_index] as $fold).member_file_ids) - 1
    }]

FoldMemberOrder =
    DataClass
  * $member_indexes mc.ReadOnlyList[$member_index int]
  # this object only ever appears as some ($file_index, Some(this object));
  # the WorkspaceMemberOrder that holds it asserts that it is the member order of
  # the fold ws.member_files[$file_index]

FileWorkspace =
    DataClass
  * $member_files mc.AppendOnlyList[FileFold | FileEntry]
  * $workspace_name mc.Optional[str]
  * $workspace_status mc.Optional[mc.DataStatus]

FileFold =
    DataClass
  * $fold_name mc.Optional[str]
  * $member_file_ids mc.AppendOnlyList[FileEntry]

FileEntry =
    DataClass
  * $entry_name str
  * $file_id mc.FileId

StreamChunkDict =
    DataClass
  * $stream_name2stream_chunk dict[str, StreamChunk]
  * $start mc.datetime
  * $end mc.Optional[mc.datetime]

StreamChunk =
    Oneof
  | $screen_stream_chunk ScreenStreamChunk
  | $chat_stream_chunk ChatStreamChunk
  | $score_stream_chunk ScoreStreamChunk
  | $terminal_control_stream_chunk TerminalControlStreamChunk

ScreenStreamChunk =
    DataClass
  * $chunks list[ScreenCapture]
  * $stream_name str
  * $owner_user_name mc.UserName

ChatStreamChunk =
    DataClass
  * $stream_name str
  * $chunks list[mc.ChatMessage]
  * Appendix[{
      the chat pane shows = this meeting's chat-stream records merged with its
                    terminal_control stream records, by time
                    (a view for the display layer; every item still belongs to
                     its own stream)
    }]

ScoreStreamChunk =
    DataClass
  * $chunks list[mc.ScoreMessage]

TerminalControlStreamChunk =
    DataClass
  * $chunks list[TerminalControl]
  * Appendix[{
      the chat pane shows ∋ the records of this stream
          # shown as a centered status line: nobody said it, there is no author,
          # and the line names the record's $user_name
          # a hold and a hand-back are not on the record itself but a relation
          # between two adjacent records: only when this record's $user_name
          # differs from the previous one is there a hold or a hand-back
    }]

TerminalControl =
    DataClass
  * $tmux_window_name str
  * $tmux_pane int
  * $user_name mc.Optional[mc.UserName]
  * $start_time mc.datetime
  * Appendix[{
      # absent $user_name = nobody holds the terminal from start_time on: the
      # human handed it back and no agent took over
      # every hold and every hand-back is written as a record, no exceptions
      user_name is absent, or user_name ∈ participants(user_names(MeetingData))
    }]
  * Appendix[{
      terminal_capable = { $human nil } ∪ { a | role(a) == $generator }   # role looks up the meeting's agent_name2role
    }]
  * Assert[{
        user_name is absent, or
        user_name == $human nil, or
        the role of user_name's agent == $generator
    }]

ScreenCapture =
    DataClass
  * $screen_data str                       # plain text (the coordinates the model / references / scores use); every row right-trimmed
  * $terminal_text_content list[mc.TerminalTextSegment]
  * $cursor_x int                          # cell coordinates (0-based); the renderer converts to character coordinates with wcwidth
  * $cursor_y int
  * $screen_width int
  * $screen_height int
  * $captured_at mc.datetime                  # microsecond
  * Assert[{ plain_of($terminal_text_content) == $screen_data }]
  * Assert[{ len(frame_rows($screen_data)) == $screen_height }]
  * Assert[{ all(cell_width(r) <= $screen_width for r in frame_rows(concat_text_field_of_list($terminal_text_content))) }]
  * Assert[{ all(len(seg.text) > 0 for seg in $terminal_text_content) }]
  * Assert[{ 0 <= $cursor_y < $screen_height and 0 <= $cursor_x < $screen_width }]
  * Assert[{ ∀ i: seg_attrs($terminal_text_content[i]) != seg_attrs($terminal_text_content[i+1]) }]
  * Appendix[{
      frame_rows(s)      = s.split('\n')                       # a row = a content row, right-trimmed; no trailing blank rows
      concat_text_field_of_list(list) = ''.join(seg.text for seg in list)
      row_plain(row)     = row.rstrip(' ')
      plain_of(list)     = '\n'.join(row_plain(r) for r in frame_rows(concat_text_field_of_list(list)))
      cell_width(row)    = sum(wcwidth(ch) for ch in row)      # a CJK character is 2 cells wide
      seg_attrs(seg)     = (seg.fg, seg.bg, frozenset(seg.styles))
    }]
```

---

## Stable types — meeting_core.viba

The small, stable types are gathered in this chapter: time, identity, versions,
text regions, colors, references, chat and score records. They do not change as
the work goes on, and the record layers of the later chapters are built on them.

```bash
# ---------------------------------------------------------------- #
# 1. Time, optional values, append-only lists, read-only lists
# ---------------------------------------------------------------- #

datetime =
    Object
  * $microsecond int                    # time identity inside a meeting: microseconds, unique by construction

Optional[T] = T | nil                  # absence is nil; a sum shape, not a tag

AppendOnlyList[T] = list[T]            # append-only: position is identity. A convention, not a structure
ReadOnlyList[T] = list[T]              # read-only: written once, never changed. Same as above

Result[T] =
    Oneof
  | $ok T                               # success: carries a value
  | $err str                            # failure: carries one sentence

# ---------------------------------------------------------------- #
# 2. Small stable types
# ---------------------------------------------------------------- #

AgentRole =
    Oneof
  | $generator nil                      # holds the terminal
  | $active_discriminator nil           # comments at will
  | $passive_discriminator nil          # comments when summoned

UserName =
    Oneof
  | $agent_name str
  | $human nil

DataStatus =
    Oneof
  | $deleted nil
  | $read_only nil

Versioned[T] =
    Object
  * $data T
  * $time datetime
  * $author UserName

TextRegion =
    Object
  * $start int
  * $end int

Rgb =
    Object
  * $r int                              # 0..255
  * $g int
  * $b int

Color =
    Oneof
  | $palette int                        # SGR palette index 0..255
  | $truecolor Rgb

Style =
    Oneof
  | "bold"
  | "underline"
  | "italic"
  | "reverse"
  | "blink"
  | "dim"

TerminalTextSegment =
    Object
  * $text str                           # one run of text with the same attributes; may contain \n (a background color does not break across rows)
  * $fg Optional[Color]                 # nil = the terminal's default foreground
  * $bg Optional[Color]                 # nil = the terminal's default background
  * $styles set[Style]                  # empty = no extra styles; a set, so order does not matter

# The two states the data interface hands out: one for the screen, one for a file.

ScreenContent =
    Object
  * $text str
  * $segments list[TerminalTextSegment]
  * $cursor_x int
  * $cursor_y int
  * $width int
  * $height int
  * $captured_at datetime

FileState =
    Object
  * $name str
  * $meta Optional[DataStatus]           # nil = writable (the default)
  * $content str

# ---------------------------------------------------------------- #
# 3. References, chat and score records
# ---------------------------------------------------------------- #

FileId =
    Object
  * $file_id str
  * Assert[{ $file_id ∈ keys(FileKvStore.file_id2file) }]

VersionedFile =
    Object
  * $file_name AppendOnlyList[Versioned[str]]                    # the name chain: a rename is one more version
  * $file_meta AppendOnlyList[Versioned[DataStatus]]             # the status chain: no version on it = the default, writable
  * $file_content AppendOnlyList[Versioned[str]]                 # the content chain: append only, older versions stay
  * Appendix[{
      # one chain per part: the three chains FileDesktop's Appendix names;
      # reading at a moment takes the last version with (Versioned.time ≤ the moment cited);
      # a chain only grows, an existing version never moves.
      # an empty status chain = writable: $deleted / $read_only are statuses laid
      # on later, writable is the default
    }]

ChatMessage =
    Object
  * $message list[$raw_msg str | $at_user_name UserName | $reference ContentReference]
  * $user_name UserName
  * $time datetime

ScoreMessage =
    Object
  * $tag str
  * $score Optional[float]
  * $comment Optional[str]
  * $judge_name UserName
  * $time datetime
  * $reference ContentReference
  * Appendix[{
      summon_chained = role($judge_name) != $passive_discriminator
                        ∨ this message's $reference resolves to a message that
                          summoned it
    }]
  * Assert[{ summon_chained }]

ContentReference =
    Oneof
  * Assert[{
      one kind per StreamChunk:
        screen_stream_chunk            ↔ screen_ref
        chat_stream_chunk              ↔ chat_ref
        score_stream_chunk             ↔ score_ref
        terminal_control_stream_chunk  ↔ terminal_control_ref
    }]
  * Hint[{
      a region is not a kind of reference: citing the record a stretch of text
      starts in and the one it ends in (plus the records between them) already
      describes that stretch; to point at the same stretch again elsewhere, cite
      the message that carries those references — the record a reference points
      at may itself be a message that carries references
    }]
  | $screen_ref ScreenReference
  | $chat_ref ChatReference
  | $score_ref ScoreReference
  | $terminal_control_ref TerminalControlReference
  | $versioned_file_ref VersionedFileReference
  | $versioned_file_content_ref VersionedFileContentReference

VersionedFileReference =
    Object
  * $file_id FileId
  * $version_time datetime
  * Appendix[{
      # reading at a moment: the newest version at or before that moment
      resolve($ref) = the last version on $file_id's chain that satisfies
          (Versioned.time ≤ $ref.version_time)
      # name / meta / content are three independent version chains, each resolved
      # by the same rule; an empty content chain at that moment (no version yet)
      # -> this reference has no answer
    }]

VersionedFileContentReference =
    Object
  * $versioned_file_ref VersionedFileReference
  * $region TextRegion
  * Appendix[{
      resolve($ref) = the $region slice of the content of resolve($ref.$versioned_file_ref)
    }]

ScreenReference =
    Object
  * $capture_time datetime
  * $stream_name str
  * $region TextRegion
  * Appendix[{
        resolve($screen_ref) = the record in the $stream_name stream that satisfies
            (ScreenCapture.captured_at == $screen_ref.capture_time)
    }]

ChatReference =
    Object
  * $msg_time datetime
  * $user_name UserName
  * $region TextRegion
  * Appendix[{
        resolve($chat_ref) = the record in the chat stream that satisfies
            (ChatMessage.time == $chat_ref.msg_time
             ∧ ChatMessage.user_name == $chat_ref.user_name)
    }]

ScoreReference =
    Object
  * $judge_time datetime
  * $judge_name UserName
  * Appendix[{
        resolve($score_ref) = the record in the score stream that satisfies
            (ScoreMessage.judge_name == $score_ref.judge_name
             ∧ ScoreMessage.time == $score_ref.judge_time)
    }]

TerminalControlReference =
    Object
  * $start_time datetime
  * $user_name Optional[UserName]
  * $region TextRegion
  * Appendix[{
        resolve($ctrl_ref) = the record in the control stream that satisfies
            (TerminalControl.start_time == $ctrl_ref.start_time
             ∧ TerminalControl.user_name == $ctrl_ref.user_name)
    }]
  * Hint[{
      it points at one control interval (it starts at TerminalControl.start_time
      and ends at the start_time of the next control record); there is no text to
      slice inside that interval, so region always covers the whole row
    }]

# ---------------------------------------------------------------- #
# Assert / Appendix / Hint carry a note and, when there is something to run,
# $python_code { ... }: the note is natural language and is never executed; the
# code must be executable Python and deterministic — the rule author writes it.
#
# The code reaches data through two interfaces only: the data interface
# (meeting_sdk's apis) and reflect. It may follow references as far as it needs
# to — the store's keys, the message that summoned a judge — but it never names
# an implementation detail: no storage layout, no harness object, no Python
# record type. A rule that could see how the data is kept would stop being a
# rule about the meeting.
#
# They are types whose shape is the unit (Object = nil): carrying one is what
# lets a definition be judged (is_compliant).
# ---------------------------------------------------------------- #

Assert[Cond] = Object

Appendix[Note] = Object

Hint[Note] = Object

# ---------------------------------------------------------------- #
# Completeness check (run inside the viba submodule):
#
#   from viba.is_complete import is_complete
#   from pathlib import Path
#   print(is_complete(Path("meeting_core.viba").read_text(),
#                     viba_paths=["."]))                       # True
#
# Assert / Appendix / Hint are units (the three lines above), so the walk never
# reaches a code block: neither completeness nor judgment needs terminators.
# ---------------------------------------------------------------- #

# ---------------------------------------------------------------- #
# Writing the data out (viba.serialize) has to satisfy:
#
#   rule_reflect ∘ serialize ≡ meeting_core_reflect
#
# that is, the writer writes meeting_core object data out as viba code (result);
# the result must satisfy is_compliant(result, the object's definition); and
# reflecting the result with rule_reflect must read exactly what reflecting the
# object data with meeting_core_reflect reads.
# ---------------------------------------------------------------- #

```

This chapter is complete: run viba's own `is_complete` on it and Assert / Appendix /
Hint are units, every other name resolves, and not one terminator is needed.

---

## The data interface — meeting_sdk.viba

Read-only interfaces that take data out of a meeting: signatures only, no
implementation, no writes. Every getter carries the generic parameter Ctx and
takes a $ctx Ctx — what that is is bound by the implementation (a meeting
handle, a connection, a snapshot), the interface itself does not know; the
signatures only mention stable types. The make_* group at the end takes no ctx:
it builds reference values and never touches a record.

```bash
import meeting_core as mc

# ---------------------------------------------------------------- #
# Time: time is identity
# ---------------------------------------------------------------- #

now[Ctx] =
		mc.Result[mc.datetime]
	<- $ctx Ctx 

chunk_times[Ctx] =
    mc.Result[list[mc.datetime]]               # the moment of every chunk in this span
  <- $ctx Ctx
  <- $start mc.datetime
  <- $end mc.datetime

streams_between[Ctx] =
    mc.Result[list[str]]                       # the stream names seen in this span
  <- $ctx Ctx
  <- $start mc.datetime
  <- $end mc.datetime

# ---------------------------------------------------------------- #
# Chat and scores
# ---------------------------------------------------------------- #

chat_messages[Ctx] =
    mc.Result[list[mc.ChatMessage]]
  <- $ctx Ctx
  <- $start mc.datetime
  <- $end mc.datetime

score_messages[Ctx] =
    mc.Result[list[mc.ScoreMessage]]
  <- $ctx Ctx
  <- $start mc.datetime
  <- $end mc.datetime

chat_message_at[Ctx] =
    mc.Result[mc.ChatMessage]                  # one person has one message at a moment
  <- $ctx Ctx
  <- $time mc.datetime
  <- $user mc.UserName

resolve_chat_reference[Ctx] =
    mc.Result[mc.ChatMessage]
  <- $ctx Ctx
  <- $ref mc.ChatReference

resolve_chat_content_reference[Ctx] =
    mc.Result[str]                             # the text the reference points at
  <- $ctx Ctx
  <- $ref mc.ChatReference

resolve_score_reference[Ctx] =
    mc.Result[mc.ScoreMessage]
  <- $ctx Ctx
  <- $ref mc.ScoreReference

chat_message_references[Ctx] =
    mc.Result[list[mc.ContentReference]]       # the references this message carries, in order
  <- $ctx Ctx
  <- $time mc.datetime
  <- $user mc.UserName

score_message_reference[Ctx] =
    mc.Result[mc.ContentReference]             # the reference this score carries
  <- $ctx Ctx
  <- $time mc.datetime
  <- $judge mc.UserName

first_chunk_time[Ctx] =
    mc.Result[mc.datetime]                     # the moment of the meeting's first chunk
  <- $ctx Ctx

last_chunk_time[Ctx] =
    mc.Result[mc.datetime]                     # the last one
  <- $ctx Ctx

# ---------------------------------------------------------------- #
# Screen: by time or by reference, either way the content of that screen,
# not the capture record itself
# ---------------------------------------------------------------- #
# The assertions and notes stay in the record layer; this hands out the value.

screen_at[Ctx] =
    mc.Result[mc.ScreenContent]                # the content of that screen at that moment
  <- $ctx Ctx
  <- $time mc.datetime

resolve_screen_reference[Ctx] =
    mc.Result[mc.ScreenContent]
  <- $ctx Ctx
  <- $ref mc.ScreenReference

# ---------------------------------------------------------------- #
# People: who is in the meeting, and who holds the terminal at a moment
# ---------------------------------------------------------------- #

participants[Ctx] =
    mc.Result[list[mc.UserName]]
  <- $ctx Ctx

roles[Ctx] =
    mc.Result[dict[str, mc.AgentRole]]
  <- $ctx Ctx

owner_at[Ctx] =
    mc.Result[(mc.UserName | nil)]             # nil = nobody holds the terminal at that moment
  <- $ctx Ctx
  <- $time mc.datetime

control_owner_of[Ctx] =
    mc.Result[(mc.UserName | nil)]             # nil = nobody took over after that hand-back
  <- $ctx Ctx
  <- $ref mc.TerminalControlReference

# ---------------------------------------------------------------- #
# The file layer
# ---------------------------------------------------------------- #
# VersionedFile is the whole record: the history of name / status / content each
# lives in it. Asking for "at a moment" or "the latest" gives that state, not the
# whole record.

# FileId by path: $path is either /workspace_name/entry_name or
# /workspace_name/fold_name/entry_name. Names are unique per level (see
# FileDesktop's Assert), so a path picks exactly one file.

file_id_by_path[Ctx] =
    mc.Result[mc.FileId]
  <- $ctx Ctx
  <- $path str

file_datetimes[Ctx] =
    mc.Result[list[mc.datetime]]
  <- $ctx Ctx
  <- $file mc.FileId

files_at[Ctx] =
    mc.Result[dict[str, mc.FileState]]
  <- $ctx Ctx
  <- $time mc.datetime

latest_file[Ctx] =
    mc.Result[mc.FileState]                    # the latest state
  <- $ctx Ctx
  <- $file mc.FileId

resolve_versioned_file_reference[Ctx] =
    mc.Result[mc.FileState]                    # the file's state at the moment cited
  <- $ctx Ctx
  <- $ref mc.VersionedFileReference

resolve_versioned_file_content_reference[Ctx] =
    mc.Result[str]                             # the text the reference points at
  <- $ctx Ctx
  <- $ref mc.VersionedFileContentReference

read_text[Ctx] = latest_file[Ctx]

# ---------------------------------------------------------------- #
# Building reference values: no ctx (never touches a record), always named make_*
# ---------------------------------------------------------------- #

make_text_region =
    mc.TextRegion
  <- $start int
  <- $end int

make_screen_reference =
    mc.ScreenReference
  <- $capture_time mc.datetime
  <- $stream_name str
  <- $region mc.TextRegion

make_chat_reference =
    mc.ChatReference
  <- $msg_time mc.datetime
  <- $user_name mc.UserName
  <- $region mc.TextRegion

make_score_reference =
    mc.ScoreReference
  <- $judge_time mc.datetime
  <- $judge_name mc.UserName

make_terminal_control_reference =
    mc.TerminalControlReference
  <- $start_time mc.datetime
  <- $user_name mc.UserName | nil          # absent = nobody took over after that hand-back
  <- $region mc.TextRegion

make_versioned_file_reference =
    mc.VersionedFileReference
  <- $file_id mc.FileId
  <- $version_time mc.datetime

make_versioned_file_content_reference =
    mc.VersionedFileContentReference
  <- $versioned_file_ref mc.VersionedFileReference
  <- $region mc.TextRegion


local_version_hint = Hint[{
	Ctx is python object in current process
	usage:
  	import meeting_local_sdk as sdk
}]

remote_version_hint = Hint[{
	Ctx is meeting uid
	every api should be backed by REST URL
	usage:
  	import meeting_remote_sdk as sdk
  	
 	every api should return a valid viba script as result with definition of 'entry = ...'
 	
}]
```

---

## Inside the Harness (never in MeetingData)



```bash
# The concepts in this section only constrain RemoteBuddy's own Harness; what
# they record is never written to storage and belongs to no stream.
Keystroke =
    DataClass
  * $data KeystrokeStr
  * $user_name mc.UserName
  * $stroke_time mc.datetime
  * Appendix[{
      owner_at(t) = see the Appendix of the same name on MeetingData
      $user_name must equal owner_at($stroke_time):
      only whoever holds the terminal at that moment produces a keystroke, and
      nobody produces one while nobody holds it
    }]

KeystrokeStr =
    Oneof
  | $text str
  | $ctrl str
  | $rest ($millisecond int)
```

---

## Shared helpers



```bash
LazyEvaluated[T] =
    Object
  * $cached mc.Optional[T]
  * $getter (Result[T] <- $ctx Any)

Tree[T] =
    Object
  * $data T
  * $parent LazyEvaluated[mc.Optional[Tree[T]]]
  * $children list[LazyEvaluated[Tree[T]]]
```

---

## The object layer (decentralized collaboration / lineage / images / instances)



```bash
# RemoteBuddy supports decentralized collaboration.
# i.e. Agents may come from different service-provider companies.
ServiceProvider =
    Object
  * $service_provider_uid UUID[str]
  * ...

# environments dockable like OS/Library/Application and Codex/ClaudeCode/...
RunningEnvironment =
    Object
  * $running_env_uid UUID[str]
  * ...

Configuration =
    Object
  * $configuration_uid UUID[str]
  * ...

# context like session/trajectory/meeting_record: mainly time-series data.
ContextData =
    Object
  * $context_data_uid UUID[str]
  * ...

# abstraction for working directory whose payload is of type T.
Workspace[T] =
    Object
  * $workspace_uuid UUID[str]
  * ...
  * Appendix[{ the data in workspace is of type T }]

# Composition / inheritance
AgentServiceProvider   = ServiceProvider
AgentRunningEnvironment = RunningEnvironment
AgentConfiguration     = Configuration
AgentContextData       = ContextData
AgentWorkspace         = Workspace[{Any data used by agent}]

MeetingServiceProvider   = ServiceProvider
MeetingRunningEnvironment = RunningEnvironment
MeetingConfiguration     = Configuration
MeetingContextData       = MeetingData
MeetingWorkspace         = Workspace[MeetingData]

# ---- lineage ----
AgentTreeNode =
    Object
  * $uid UUID[str]
  * $data LazyEvaluated[AgentContextData]    # a handle to this lineage point's record, read from the agent workspace

AgentTree = Tree[AgentTreeNode]

MeetingTreeNode =
    Object
  * $uid UUID[str]
  * $data LazyEvaluated[MeetingContextData]  # a handle to this lineage point's record, read from the meeting workspace
  * $agent_name2agent_node dict[str, AgentTreeNode]
  * Assert[{ keys(agent_name2agent_node) are user names in MeetingContextData }]

MeetingTree = Tree[MeetingTreeNode]

# ---- images (immutable lineage snapshots; tree_node = None ⇔ never committed) ----
AgentPrototypeImage =
    Object
  * $service_provider AgentServiceProvider
  * $running_env ReadOnly[AgentRunningEnvironment]
  * $configuration ReadOnly[AgentConfiguration]
  * $agent_tree_node mc.Optional[ReadOnly[AgentTreeNode]]
  * Assert[{ $service_provider interprets $running_env.running_env_uid,
             $configuration.configuration_uid, and $agent_tree_node.uid }]

MeetingPrototypeImage =
    Object
  * $service_provider MeetingServiceProvider
  * $running_env ReadOnly[MeetingRunningEnvironment]
  * $configuration ReadOnly[MeetingConfiguration]
  * $meeting_tree_node mc.Optional[ReadOnly[MeetingTreeNode]]
  * $agent_name2agent_prototype_image dict[str, AgentPrototypeImage]
  * Assert[{ $service_provider interprets $running_env.running_env_uid,
             $configuration.configuration_uid, and $meeting_tree_node.uid }]
  * Assert[{ keys($agent_name2agent_prototype_image) are user names in $meeting_tree_node.data }]

# ---- instances (alive: always has an image; the live tail is appended in the workspace) ----
AgentInstance =
    Object
  * $agent_prototype_image AgentPrototypeImage
  * $workspace AgentWorkspace
  * Assert[{ $agent_prototype_image.service_provider interprets $workspace.workspace_uid }]

MeetingInstance =
    Object
  * $agent_prototype_image MeetingPrototypeImage
  * $workspace MeetingWorkspace
  * $agent_name2agent_instance dict[str, AgentInstance]
  * Assert[{ $agent_prototype_image.service_provider interprets $workspace.workspace_uid }]
  * Assert[{ keys($agent_name2agent_instance) are user names in $agent_prototype_image.meeting_tree_node.data }]
  * Assert[{ keys($agent_name2agent_instance) and keys($agent_prototype_image.agent_name2agent_prototype_image) are disjoint }]
```

---

## Key operations



```bash
GetTreeFromAgentTreeNode = Result[AgentTree] <- AgentTreeNode

InstantiateAgent =
    Result[AgentInstance]
    <- $service_provider ServiceProvider
    <- $running_env ReadOnly[AgentRunningEnvironment]
    <- $configuration ReadOnly[AgentConfiguration]
    <- $workspace AgentWorkspace
    <- Assert[{ $service_provider interprets $running_env.running_env_uid,
                $configuration.configuration_uid, and $workspace.workspace_uid }]

SpawnAgent =
    Result[AgentInstance]
    <- $image AgentPrototypeImage
    <- $workspace AgentWorkspace
    <- Assert[{ $image.service_provider interprets $workspace.workspace_uid }]

MakeAgentPrototypeImage = Result[$ret AgentPrototypeImage] <- $self AgentInstance <- Hint[{
    1. (old context data) + (data in workspace) -> (new context data).
    2. create child node in AgentTree
}]

GetTreeFromMeetingTreeNode = Result[MeetingTree] <- $node MeetingTreeNode

InstantiateMeeting =
    Result[MeetingInstance]
    <- $service_provider MeetingServiceProvider
    <- $running_env ReadOnly[MeetingRunningEnvironment]
    <- $configuration ReadOnly[MeetingConfiguration]
    <- $workspace MeetingWorkspace
    <- Assert[{ $service_provider interprets $running_env.running_env_uid,
                $configuration.configuration_uid, and $workspace.workspace_uid }]

SpawnMeeting =
    Result[MeetingInstance]
    <- $image MeetingPrototypeImage
    <- $workspace MeetingWorkspace
    <- $agent_name2new_agent_workspace dict[str, AgentWorkspace]
    <- Hint[{
        1. restore the meeting record on top of $image.meeting_tree_node.data;
        2. call SpawnAgent for every agent in
           $image.agent_name2agent_prototype_image to build
           $agent_name2agent_instance;
        3. hand out a runnable MeetingInstance.
        4. an agent in the image comes back only if its name appears in
           $agent_name2new_agent_workspace.
    }]
    <- Assert[{ keys($agent_name2new_agent_workspace)
                ⊆ keys($image.agent_name2agent_prototype_image) }]
    <- Assert[{ $image.service_provider interprets $workspace.workspace_uid }]

MakeMeetingPrototypeImage = Result[$ret MeetingPrototypeImage] <- $self MeetingInstance <- Hint[{
    1. (old context data) + (data in workspace) -> (new context data).
    2. join Map[MakeAgentPrototypeImage]($agent_name2agent_instance) and $self.agent_prototype_image.agent_name2agent_prototype_image
       into $ret.agent_name2agent_prototype_image.
    3. create child node in MeetingTree.
}]

JoinMeeting =
    Result[$self MeetingInstance]
    <- $self MeetingInstance
    <- $agent_name str
    <- $agent_instance AgentInstance
    <- $role mc.AgentRole
```
