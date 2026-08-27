---
name: meeting-participant
description: Participate in a RemoteBuddy filesystem-backed meeting turn using only pwd, cd, and non-interactive Vim Ex commands when the harness assigns a role, turn ID, objective, and output file. Use only inside a generated RemoteBuddy meeting workspace.
---

# Meeting Participant

Complete exactly the assigned meeting turn. Treat `brief.md`, `conversation.md`, prior turn files, `meeting.json`, stream files, and this skill as read-only.

## Allowed Interaction

Use only `pwd`, `cd`, and `vim` commands. This is a demo protocol rather than a security boundary. Do not use shell redirection, other command-line readers or editors, scripts, patch tools, web tools, or delegated agents. Always run Vim in non-interactive Ex mode so the command exits by itself; never leave an interactive or background Vim process running.

1. Read with `vim -Nu NONE -n -es -c 'set nomore' -c '%print' -c 'qa!' brief.md`.
2. Read `conversation.md` with the same non-interactive pattern.
3. Create only the assigned output with one non-interactive Vim command. Use Ex commands such as `call setline(1, [...])` followed by `wq`; ensure every body line and the marker are separate list items.

## Turn Content

- Address the assigned objective and role directly.
- Ground claims in `brief.md` and cite relevant prior turn IDs when responding to another participant.
- Keep the message concise and do not include hidden reasoning, command logs, or the completion acknowledgement.
- Put the exact line `<!-- REMOTEBUDDY_TURN_COMPLETE -->` at the end of the output file, after the message body.

After the Vim command exits successfully, make the final assistant response exactly `TURN_COMPLETE <turn-id>`, substituting the assigned turn ID. Do not add other text to the final response.
