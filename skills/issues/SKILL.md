---
name: issues
description: Track work for this project with jh, a local gh issue clone. Use for picking the next task, recording progress, and closing work.
---

`jh` is `gh` for a local tracker: `jh issue create|list|view|edit|close|reopen|comment`,
`jh label ...`, `jh milestone ...`. Same flags, same `--json` fields, same `--jq`.
Additions: `--blocked-by`, `jh issue list --ready`, `jh board`.
Read an issue and its comments: `jh issue view N --json body,comments --jq '.body, (.comments[] | .body)'`.
Start a task: `jh issue edit N --add-label in-progress`. Leave findings as comments.
Capture something noticed mid-task at once: `jh issue create -l draft ...`. A draft is not
ready; the session that takes it writes it up first, then removes the label.
Close with `jh issue close N -r completed -c "what changed"`.
Repo comes from JH_REPO; the server must be reachable at JH_SERVER (default 127.0.0.1:7411).
