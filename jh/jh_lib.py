"""Model, event log and replay for jh.

Everything here is pure apart from appending to and reading the per-repo
`events.jsonl` file. The server wraps a `Store`; the tests drive it directly.

The store is the only writer. Every change is validated against the current
in-memory state, written as one JSON object on one line of the log (flushed
and fsynced), and then applied to the state with the same `apply_event`
function that replay uses, so a restart reproduces the state exactly.
"""

from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PORT = 7411
DEFAULT_HOST = "127.0.0.1"
IN_PROGRESS_LABEL = "in-progress"
DRAFT_LABEL = "draft"
GLOSSARY_LABEL = "glossary"

# gh 2.88.1 `gh issue list --json` field list, plus the jh extensions.
ISSUE_FIELDS_GH = [
    "assignees",
    "author",
    "body",
    "closed",
    "closedAt",
    "closedByPullRequestsReferences",
    "comments",
    "createdAt",
    "id",
    "isPinned",
    "labels",
    "milestone",
    "number",
    "projectCards",
    "projectItems",
    "reactionGroups",
    "state",
    "stateReason",
    "title",
    "updatedAt",
    "url",
]
ISSUE_FIELDS_EXT = ["blockedBy", "blocking"]
ISSUE_FIELDS = ISSUE_FIELDS_GH + ISSUE_FIELDS_EXT

# gh 2.88.1 `gh label list --json` field list.
LABEL_FIELDS = [
    "color",
    "createdAt",
    "description",
    "id",
    "isDefault",
    "name",
    "updatedAt",
    "url",
]

# GitHub milestone object field names (gh has no milestone command).
MILESTONE_FIELDS = [
    "closedAt",
    "createdAt",
    "description",
    "dueOn",
    "id",
    "number",
    "state",
    "title",
    "updatedAt",
    "url",
]

STATE_REASONS = ("completed", "not planned", "duplicate")
SEARCH_QUALIFIERS = ("is", "state", "label", "milestone", "no", "assignee", "author")

EVENT_TYPES = frozenset(
    {
        "repo.created",
        "issue.created",
        "issue.edited",
        "issue.labeled",
        "issue.unlabeled",
        "issue.milestoned",
        "issue.demilestoned",
        "issue.assigned",
        "issue.unassigned",
        "issue.blocked",
        "issue.unblocked",
        "issue.commented",
        "issue.comment_edited",
        "issue.comment_deleted",
        "issue.closed",
        "issue.reopened",
        "issue.deleted",
        "label.created",
        "label.edited",
        "label.deleted",
        "milestone.created",
        "milestone.edited",
        "milestone.closed",
    }
)

_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class JhError(Exception):
    """A user-facing error carrying an HTTP status code.

    Attributes:
        status: HTTP status the server should answer with.
        message: Human-readable message, printed by the client.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def now_ts() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_home() -> Path:
    """Return the log base directory: `JH_HOME` or `~/.local/share/jh`."""
    env = os.environ.get("JH_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "jh"


# --------------------------------------------------------------------------
# Event log
# --------------------------------------------------------------------------


class EventLog:
    """One append-only JSONL file.

    Attributes:
        path: Location of the `events.jsonl` file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> list[dict[str, Any]]:
        """Return every event in the log, in file order.

        A trailing partial line (a crash mid-write) is ignored.
        """
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    break
        return events

    def append(self, event: dict[str, Any]) -> None:
        """Append one event and flush it to disk before returning."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())


# --------------------------------------------------------------------------
# Repo state and replay
# --------------------------------------------------------------------------


class RepoState:
    """In-memory state of one repo, derived purely from its events.

    Attributes:
        name: Repo name.
        issues: Issue number to issue dict (deleted issues stay, flagged).
        labels: Label name to label dict.
        milestones: Milestone number to milestone dict.
        seq: Sequence number of the last applied event.
        next_number: Next issue number to assign.
        next_comment_id: Next comment id to assign.
        events: Every applied event, for `GET /:repo/events`.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.issues: dict[int, dict[str, Any]] = {}
        self.labels: dict[str, dict[str, Any]] = {}
        self.milestones: dict[int, dict[str, Any]] = {}
        self.seq = 0
        self.next_number = 1
        self.next_comment_id = 1
        self.next_milestone = 1
        self.events: list[dict[str, Any]] = []

    # -- lookups -----------------------------------------------------------

    def issue(self, number: int) -> dict[str, Any]:
        """Return a live (not deleted) issue or raise 404."""
        issue = self.issues.get(number)
        if issue is None or issue["deleted"]:
            raise JhError(
                404, f"Could not resolve to an issue with the number of {number}."
            )
        return issue

    def live_issues(self) -> list[dict[str, Any]]:
        """Return all non-deleted issues, newest first."""
        return sorted(
            (i for i in self.issues.values() if not i["deleted"]),
            key=lambda i: -i["number"],
        )

    def label(self, name: str) -> dict[str, Any]:
        """Return a label or raise 404."""
        label = self.labels.get(name)
        if label is None:
            raise JhError(404, f"'{name}' not found")
        return label

    def milestone(self, ref: int | str) -> dict[str, Any]:
        """Return a milestone by number or title, or raise 404."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            milestone = self.milestones.get(int(ref))
            if milestone is not None:
                return milestone
        for milestone in self.milestones.values():
            if milestone["title"] == ref:
                return milestone
        raise JhError(404, f"no milestone found with title or number {ref!r}")

    def is_blocked(self, issue: dict[str, Any]) -> bool:
        """Return whether any blocker of the issue is still open."""
        return any(self._is_open(n) for n in issue["blockedBy"])

    def is_draft(self, issue: dict[str, Any]) -> bool:
        """Return whether the issue is a captured note not yet written up.

        A draft carries the `draft` label. It is never ready: a session may
        not start it and no decision may be put from it until it is written
        to the project's issue standard and the label removed.
        """
        return DRAFT_LABEL in issue["labels"]

    def _is_open(self, number: int) -> bool:
        blocker = self.issues.get(number)
        return (
            blocker is not None
            and not blocker["deleted"]
            and blocker["state"] == "OPEN"
        )

    def blocking(self, number: int) -> list[int]:
        """Return the numbers of live issues that list `number` as a blocker."""
        return sorted(
            i["number"]
            for i in self.issues.values()
            if not i["deleted"] and number in i["blockedBy"]
        )

    def would_cycle(self, number: int, blockers: list[int]) -> bool:
        """Return whether making `blockers` block `number` creates a cycle."""
        seen: set[int] = set()
        stack = list(blockers)
        while stack:
            current = stack.pop()
            if current == number:
                return True
            if current in seen:
                continue
            seen.add(current)
            issue = self.issues.get(current)
            if issue is not None and not issue["deleted"]:
                stack.extend(issue["blockedBy"])
        return False

    # -- replay ------------------------------------------------------------

    def apply_event(self, event: dict[str, Any]) -> None:
        """Apply one event to the state. Used for both replay and live writes."""
        kind = event["type"]
        ts = event["ts"]
        self.seq = max(self.seq, event["seq"])
        self.events.append(event)

        if kind == "repo.created":
            return

        if kind.startswith("label."):
            self._apply_label(kind, event)
            return
        if kind.startswith("milestone."):
            self._apply_milestone(kind, event, ts)
            return

        number = event["number"]
        if kind == "issue.created":
            self.issues[number] = {
                "number": number,
                "title": event["title"],
                "body": event.get("body", ""),
                "state": "OPEN",
                "stateReason": None,
                "labels": list(event.get("labels", [])),
                "milestone": event.get("milestone"),
                "assignees": list(event.get("assignees", [])),
                "author": event["actor"],
                "comments": [],
                "createdAt": ts,
                "updatedAt": ts,
                "closedAt": None,
                "blockedBy": sorted(set(event.get("blockedBy", []))),
                "deleted": False,
            }
            self.next_number = max(self.next_number, number + 1)
            return

        issue = self.issues.get(number)
        if issue is None:
            return
        issue["updatedAt"] = ts
        if kind == "issue.edited":
            for field in ("title", "body"):
                if field in event:
                    issue[field] = event[field]
        elif kind == "issue.labeled":
            for name in event["labels"]:
                if name not in issue["labels"]:
                    issue["labels"].append(name)
        elif kind == "issue.unlabeled":
            issue["labels"] = [n for n in issue["labels"] if n not in event["labels"]]
        elif kind == "issue.milestoned":
            issue["milestone"] = event["milestone"]
        elif kind == "issue.demilestoned":
            issue["milestone"] = None
        elif kind == "issue.assigned":
            for login in event["assignees"]:
                if login not in issue["assignees"]:
                    issue["assignees"].append(login)
        elif kind == "issue.unassigned":
            issue["assignees"] = [
                a for a in issue["assignees"] if a not in event["assignees"]
            ]
        elif kind == "issue.blocked":
            issue["blockedBy"] = sorted(
                set(issue["blockedBy"]) | set(event["blockedBy"])
            )
        elif kind == "issue.unblocked":
            issue["blockedBy"] = [
                n for n in issue["blockedBy"] if n not in event["blockedBy"]
            ]
        elif kind == "issue.commented":
            issue["comments"].append(
                {
                    "id": event["id"],
                    "author": event["actor"],
                    "body": event["body"],
                    "createdAt": ts,
                    "updatedAt": ts,
                }
            )
            self.next_comment_id = max(self.next_comment_id, event["id"] + 1)
        elif kind == "issue.comment_edited":
            for comment in issue["comments"]:
                if comment["id"] == event["id"]:
                    comment["body"] = event["body"]
                    comment["updatedAt"] = ts
        elif kind == "issue.comment_deleted":
            issue["comments"] = [c for c in issue["comments"] if c["id"] != event["id"]]
        elif kind == "issue.closed":
            issue["state"] = "CLOSED"
            issue["stateReason"] = event.get("stateReason", "completed")
            issue["closedAt"] = ts
        elif kind == "issue.reopened":
            issue["state"] = "OPEN"
            issue["stateReason"] = None
            issue["closedAt"] = None
        elif kind == "issue.deleted":
            issue["deleted"] = True
            for other in self.issues.values():
                if number in other["blockedBy"]:
                    other["blockedBy"] = [n for n in other["blockedBy"] if n != number]

    def _apply_label(self, kind: str, event: dict[str, Any]) -> None:
        ts = event["ts"]
        name = event["name"]
        if kind == "label.created":
            self.labels[name] = {
                "name": name,
                "color": event.get("color", "ededed"),
                "description": event.get("description", ""),
                "createdAt": ts,
                "updatedAt": ts,
            }
        elif kind == "label.edited":
            label = self.labels.get(name)
            if label is None:
                return
            label["updatedAt"] = ts
            for field in ("color", "description"):
                if field in event:
                    label[field] = event[field]
            new_name = event.get("newName")
            if new_name and new_name != name:
                label["name"] = new_name
                del self.labels[name]
                self.labels[new_name] = label
                for issue in self.issues.values():
                    issue["labels"] = [
                        new_name if n == name else n for n in issue["labels"]
                    ]
        elif kind == "label.deleted":
            self.labels.pop(name, None)
            for issue in self.issues.values():
                if name in issue["labels"]:
                    issue["labels"] = [n for n in issue["labels"] if n != name]

    def _apply_milestone(self, kind: str, event: dict[str, Any], ts: str) -> None:
        number = event["number"]
        if kind == "milestone.created":
            self.milestones[number] = {
                "number": number,
                "title": event["title"],
                "description": event.get("description", ""),
                "dueOn": event.get("dueOn"),
                "state": "OPEN",
                "createdAt": ts,
                "updatedAt": ts,
                "closedAt": None,
            }
            self.next_milestone = max(self.next_milestone, number + 1)
            return
        milestone = self.milestones.get(number)
        if milestone is None:
            return
        milestone["updatedAt"] = ts
        if kind == "milestone.edited":
            for field in ("title", "description", "dueOn"):
                if field in event:
                    milestone[field] = event[field]
            if event.get("state") == "OPEN":
                milestone["state"] = "OPEN"
                milestone["closedAt"] = None
        elif kind == "milestone.closed":
            milestone["state"] = "CLOSED"
            milestone["closedAt"] = ts


# --------------------------------------------------------------------------
# Store: validation, event emission, JSON rendering
# --------------------------------------------------------------------------


class Store:
    """All repos under one base directory, with the write path.

    Attributes:
        home: Base directory holding `<repo>/events.jsonl`.
        base_url: Server URL used for `url` fields, e.g. `http://127.0.0.1:7411`.
        repos: Repo name to state.
    """

    def __init__(self, home: Path | None = None, base_url: str | None = None) -> None:
        self.home = Path(home) if home is not None else default_home()
        self.base_url = (base_url or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}").rstrip(
            "/"
        )
        self.repos: dict[str, RepoState] = {}
        self._logs: dict[str, EventLog] = {}
        self.replay()

    # -- loading -----------------------------------------------------------

    def replay(self) -> None:
        """Rebuild every repo's state from its log."""
        self.repos = {}
        self._logs = {}
        if not self.home.exists():
            return
        for path in sorted(self.home.glob("*/events.jsonl")):
            name = path.parent.name
            state = RepoState(name)
            for event in EventLog(path).read():
                state.apply_event(event)
            self.repos[name] = state
            self._logs[name] = EventLog(path)

    def repo(self, name: str) -> RepoState:
        """Return a repo state or raise 404 listing the known repos."""
        state = self.repos.get(name)
        if state is None:
            known = ", ".join(sorted(self.repos)) or "(none)"
            raise JhError(404, f"repo {name!r} not found; known repos: {known}")
        return state

    def repo_list(self) -> list[dict[str, Any]]:
        """Return the known repos as `{name, url, issues}` dicts."""
        return [
            {"name": n, "url": f"{self.base_url}/{n}", "issues": len(s.live_issues())}
            for n, s in sorted(self.repos.items())
        ]

    def repo_create(self, name: str, actor: str) -> dict[str, Any]:
        """Create a repo (issue space)."""
        if not _REPO_NAME.match(name or ""):
            raise JhError(422, f"invalid repo name {name!r}")
        if name in self.repos:
            raise JhError(422, f"repo {name!r} already exists")
        state = RepoState(name)
        self.repos[name] = state
        self._logs[name] = EventLog(self.home / name / "events.jsonl")
        self._commit(state, actor, {"type": "repo.created", "name": name})
        return {"name": name, "url": f"{self.base_url}/{name}", "issues": 0}

    def _commit(
        self, state: RepoState, actor: str, event: dict[str, Any]
    ) -> dict[str, Any]:
        """Stamp, append and apply one event."""
        assert event["type"] in EVENT_TYPES, event["type"]
        full = {"seq": state.seq + 1, "ts": now_ts(), "actor": actor}
        full.update(event)
        self._logs[state.name].append(full)
        state.apply_event(full)
        return full

    # -- issues ------------------------------------------------------------

    def issue_create(
        self, repo: str, actor: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Create an issue. Labels, milestone and blockers must exist."""
        state = self.repo(repo)
        title = (data.get("title") or "").strip()
        if not title:
            raise JhError(422, "title can't be blank")
        labels = _as_list(data.get("labels"))
        for name in labels:
            state.label(name)
        milestone = None
        if data.get("milestone") not in (None, ""):
            milestone = state.milestone(data["milestone"])["number"]
        blocked_by = _as_numbers(data.get("blockedBy"))
        for number in blocked_by:
            state.issue(number)
        event = {
            "type": "issue.created",
            "number": state.next_number,
            "title": title,
            "body": data.get("body") or "",
        }
        if labels:
            event["labels"] = labels
        if milestone is not None:
            event["milestone"] = milestone
        assignees = _as_list(data.get("assignees"))
        if assignees:
            event["assignees"] = assignees
        if blocked_by:
            event["blockedBy"] = blocked_by
        full = self._commit(state, actor, event)
        return self.issue_json(state, state.issue(full["number"]))

    def issue_get(self, repo: str, number: int) -> dict[str, Any]:
        """Return one issue's full JSON."""
        state = self.repo(repo)
        return self.issue_json(state, state.issue(number))

    def issue_list(self, repo: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """List issues with the command-line filters.

        Args:
            repo: Repo name.
            params: Any of `state` (open|closed|all, default open), `labels`
                (list or comma string), `milestone` (number or title),
                `assignee`, `author`, `search`, `limit` (default 30),
                `ready`, `blocked_by`, `blocking`.

        Returns:
            Matching issues as full JSON objects, newest first.
        """
        state = self.repo(repo)
        want_state = (params.get("state") or "open").lower()
        if want_state not in ("open", "closed", "all"):
            raise JhError(
                422,
                f'invalid argument "{want_state}" for "-s, --state" flag: valid values are {{open|closed|all}}',
            )
        labels = _as_list(params.get("labels"))
        milestone = params.get("milestone")
        milestone_number = None
        if milestone not in (None, ""):
            milestone_number = state.milestone(milestone)["number"]
        assignee = params.get("assignee") or None
        author = params.get("author") or None
        search = _parse_search(params.get("search") or "")
        limit = int(params.get("limit") or 30)
        if limit < 1:
            raise JhError(422, f"invalid limit: {limit}")
        ready = _truthy(params.get("ready"))
        blocked_by = params.get("blocked_by")
        blocking = params.get("blocking")
        if blocked_by not in (None, ""):
            state.issue(int(blocked_by))
        if blocking not in (None, ""):
            state.issue(int(blocking))

        if "state" in search:
            want_state = search["state"]

        results = []
        for issue in state.live_issues():
            if want_state != "all" and issue["state"] != want_state.upper():
                continue
            if labels and not all(n in issue["labels"] for n in labels):
                continue
            if milestone_number is not None and issue["milestone"] != milestone_number:
                continue
            if assignee and assignee not in issue["assignees"]:
                continue
            if author and issue["author"] != author:
                continue
            if ready and (
                issue["state"] != "OPEN"
                or state.is_blocked(issue)
                or state.is_draft(issue)
            ):
                continue
            if (
                blocked_by not in (None, "")
                and int(blocked_by) not in issue["blockedBy"]
            ):
                continue
            if (
                blocking not in (None, "")
                and issue["number"] not in state.issues[int(blocking)]["blockedBy"]
            ):
                continue
            if not _search_matches(state, issue, search):
                continue
            results.append(self.issue_json(state, issue))
            if len(results) >= limit:
                break
        return results

    def issue_edit(
        self, repo: str, actor: str, number: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Edit an issue; emits one event per kind of change.

        Args:
            repo: Repo name.
            actor: Who is making the change.
            number: Issue number.
            data: Any of `title`, `body`, `labels` (replace), `addLabels`,
                `removeLabels`, `milestone` (number, title or null),
                `assignees`, `addAssignees`, `removeAssignees`, `blockedBy`
                (replace), `addBlockedBy`, `removeBlockedBy`, `state`
                (open|closed), `stateReason`, `comment`.

        Returns:
            The updated issue JSON.
        """
        state = self.repo(repo)
        issue = state.issue(number)

        edited: dict[str, Any] = {}
        if "title" in data and data["title"] is not None:
            if not str(data["title"]).strip():
                raise JhError(422, "title can't be blank")
            edited["title"] = data["title"]
        if "body" in data and data["body"] is not None:
            edited["body"] = data["body"]

        add_labels = _as_list(data.get("addLabels"))
        remove_labels = _as_list(data.get("removeLabels"))
        if "labels" in data and data["labels"] is not None:
            wanted = _as_list(data["labels"])
            add_labels += [n for n in wanted if n not in issue["labels"]]
            remove_labels += [n for n in issue["labels"] if n not in wanted]
        for name in add_labels:
            state.label(name)
        for name in remove_labels:
            state.label(name)

        milestone_change: tuple[str, Any] | None = None
        if "milestone" in data:
            if data["milestone"] in (None, ""):
                if issue["milestone"] is not None:
                    milestone_change = ("issue.demilestoned", None)
            else:
                target = state.milestone(data["milestone"])["number"]
                if target != issue["milestone"]:
                    milestone_change = ("issue.milestoned", target)

        add_assignees = _as_list(data.get("addAssignees"))
        remove_assignees = _as_list(data.get("removeAssignees"))
        if "assignees" in data and data["assignees"] is not None:
            wanted = _as_list(data["assignees"])
            add_assignees += [a for a in wanted if a not in issue["assignees"]]
            remove_assignees += [a for a in issue["assignees"] if a not in wanted]

        add_blocked = _as_numbers(data.get("addBlockedBy"))
        remove_blocked = _as_numbers(data.get("removeBlockedBy"))
        if "blockedBy" in data and data["blockedBy"] is not None:
            wanted = _as_numbers(data["blockedBy"])
            add_blocked += [n for n in wanted if n not in issue["blockedBy"]]
            remove_blocked += [n for n in issue["blockedBy"] if n not in wanted]
        for blocker in add_blocked:
            if blocker == number:
                raise JhError(422, f"issue #{number} cannot block itself")
            state.issue(blocker)
        if add_blocked and state.would_cycle(number, add_blocked):
            raise JhError(
                422,
                f"adding blockers {add_blocked} to #{number} would create a dependency cycle",
            )

        new_state = (data.get("state") or "").lower() or None
        if new_state not in (None, "open", "closed"):
            raise JhError(422, f"invalid state {new_state!r}")
        reason = data.get("stateReason")
        if reason is not None and reason not in STATE_REASONS:
            raise JhError(
                422,
                f'invalid argument "{reason}" for "-r, --reason" flag: valid values are {{completed|not planned|duplicate}}',
            )
        duplicate_of = data.get("duplicateOf")
        if duplicate_of not in (None, ""):
            state.issue(int(duplicate_of))
            if reason is None:
                reason = "duplicate"

        # Closing ends the work, so the in-progress status label comes off
        # with it (unless this same edit asks to add it). gh leaves labels
        # alone on close; this is the one place jh departs, because the
        # label exists only to say a session is on the issue right now.
        if (
            new_state == "closed"
            and issue["state"] == "OPEN"
            and IN_PROGRESS_LABEL in issue["labels"]
            and IN_PROGRESS_LABEL not in add_labels
            and IN_PROGRESS_LABEL not in remove_labels
        ):
            remove_labels.append(IN_PROGRESS_LABEL)

        # All validation done; emit events.
        if edited:
            self._commit(
                state, actor, {"type": "issue.edited", "number": number, **edited}
            )
        adds = [n for n in add_labels if n not in issue["labels"]]
        if adds:
            self._commit(
                state,
                actor,
                {"type": "issue.labeled", "number": number, "labels": adds},
            )
        removes = [n for n in remove_labels if n in issue["labels"]]
        if removes:
            self._commit(
                state,
                actor,
                {"type": "issue.unlabeled", "number": number, "labels": removes},
            )
        if milestone_change:
            kind, target = milestone_change
            event = {"type": kind, "number": number}
            if target is not None:
                event["milestone"] = target
            self._commit(state, actor, event)
        adds = [a for a in add_assignees if a not in issue["assignees"]]
        if adds:
            self._commit(
                state,
                actor,
                {"type": "issue.assigned", "number": number, "assignees": adds},
            )
        removes = [a for a in remove_assignees if a in issue["assignees"]]
        if removes:
            self._commit(
                state,
                actor,
                {"type": "issue.unassigned", "number": number, "assignees": removes},
            )
        adds = [n for n in add_blocked if n not in issue["blockedBy"]]
        if adds:
            self._commit(
                state,
                actor,
                {"type": "issue.blocked", "number": number, "blockedBy": adds},
            )
        removes = [n for n in remove_blocked if n in issue["blockedBy"]]
        if removes:
            self._commit(
                state,
                actor,
                {"type": "issue.unblocked", "number": number, "blockedBy": removes},
            )
        if data.get("comment"):
            self._commit(
                state,
                actor,
                {
                    "type": "issue.commented",
                    "number": number,
                    "id": state.next_comment_id,
                    "body": data["comment"],
                },
            )
        if new_state == "closed" and issue["state"] == "OPEN":
            event = {
                "type": "issue.closed",
                "number": number,
                "stateReason": reason or "completed",
            }
            if duplicate_of not in (None, ""):
                event["duplicateOf"] = int(duplicate_of)
            self._commit(state, actor, event)
        elif new_state == "open" and issue["state"] == "CLOSED":
            self._commit(state, actor, {"type": "issue.reopened", "number": number})
        return self.issue_json(state, issue)

    def issue_delete(self, repo: str, actor: str, number: int) -> dict[str, Any]:
        """Delete an issue. The number is never reused."""
        state = self.repo(repo)
        issue = state.issue(number)
        snapshot = self.issue_json(state, issue)
        self._commit(state, actor, {"type": "issue.deleted", "number": number})
        return snapshot

    # -- comments ----------------------------------------------------------

    def comment_list(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Return an issue's comments."""
        state = self.repo(repo)
        return [
            self.comment_json(state, number, c) for c in state.issue(number)["comments"]
        ]

    def comment_create(
        self, repo: str, actor: str, number: int, body: str
    ) -> dict[str, Any]:
        """Add a comment."""
        state = self.repo(repo)
        state.issue(number)
        if not (body or "").strip():
            raise JhError(422, "comment body can't be blank")
        full = self._commit(
            state,
            actor,
            {
                "type": "issue.commented",
                "number": number,
                "id": state.next_comment_id,
                "body": body,
            },
        )
        return self.comment_json(
            state, number, self._find_comment(state, full["id"])[1]
        )

    def comment_edit(
        self, repo: str, actor: str, comment_id: int, body: str
    ) -> dict[str, Any]:
        """Edit the caller's own comment."""
        state = self.repo(repo)
        number, comment = self._find_comment(state, comment_id)
        if comment["author"] != actor:
            raise JhError(
                403, f"comment {comment_id} belongs to {comment['author']}, not {actor}"
            )
        if not (body or "").strip():
            raise JhError(422, "comment body can't be blank")
        self._commit(
            state,
            actor,
            {
                "type": "issue.comment_edited",
                "number": number,
                "id": comment_id,
                "body": body,
            },
        )
        return self.comment_json(state, number, comment)

    def comment_delete(self, repo: str, actor: str, comment_id: int) -> dict[str, Any]:
        """Delete the caller's own comment."""
        state = self.repo(repo)
        number, comment = self._find_comment(state, comment_id)
        if comment["author"] != actor:
            raise JhError(
                403, f"comment {comment_id} belongs to {comment['author']}, not {actor}"
            )
        snapshot = self.comment_json(state, number, comment)
        self._commit(
            state,
            actor,
            {"type": "issue.comment_deleted", "number": number, "id": comment_id},
        )
        return snapshot

    def _find_comment(
        self, state: RepoState, comment_id: int
    ) -> tuple[int, dict[str, Any]]:
        for issue in state.issues.values():
            if issue["deleted"]:
                continue
            for comment in issue["comments"]:
                if comment["id"] == comment_id:
                    return issue["number"], comment
        raise JhError(404, f"comment {comment_id} not found")

    # -- labels ------------------------------------------------------------

    def label_list(
        self, repo: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """List labels, oldest first (gh's default sort is `created`)."""
        params = params or {}
        state = self.repo(repo)
        search = (params.get("search") or "").lower()
        limit = int(params.get("limit") or 30)
        out = []
        for label in sorted(
            state.labels.values(), key=lambda l: (l["createdAt"], l["name"])
        ):
            if (
                search
                and search not in label["name"].lower()
                and search not in (label["description"] or "").lower()
            ):
                continue
            out.append(self.label_json(state, label))
            if len(out) >= limit:
                break
        return out

    def label_create(
        self, repo: str, actor: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a label, or update it when `force` is set and it exists."""
        state = self.repo(repo)
        name = (data.get("name") or "").strip()
        if not name:
            raise JhError(422, "label name can't be blank")
        color = _check_color(data.get("color")) or "%06x" % random.randrange(
            0, 0xFFFFFF
        )
        description = data.get("description") or ""
        if name in state.labels:
            if not data.get("force"):
                raise JhError(
                    422,
                    f'label with name "{name}" already exists; use `--force` to update its color and description',
                )
            event: dict[str, Any] = {
                "type": "label.edited",
                "name": name,
                "description": description,
            }
            if data.get("color"):
                event["color"] = color
            self._commit(state, actor, event)
        else:
            self._commit(
                state,
                actor,
                {
                    "type": "label.created",
                    "name": name,
                    "color": color,
                    "description": description,
                },
            )
        return self.label_json(state, state.label(name))

    def label_edit(
        self, repo: str, actor: str, name: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Rename or recolour a label; renames propagate to issues."""
        state = self.repo(repo)
        state.label(name)
        event: dict[str, Any] = {"type": "label.edited", "name": name}
        new_name = (data.get("name") or "").strip()
        if new_name and new_name != name:
            if new_name in state.labels:
                raise JhError(422, f'label with name "{new_name}" already exists')
            event["newName"] = new_name
        if data.get("color"):
            event["color"] = _check_color(data["color"])
        if data.get("description") is not None:
            event["description"] = data["description"]
        if len(event) == 2:
            raise JhError(
                422, "specify at least one of `--color`, `--description`, or `--name`"
            )
        self._commit(state, actor, event)
        return self.label_json(state, state.label(new_name or name))

    def label_delete(self, repo: str, actor: str, name: str) -> dict[str, Any]:
        """Delete a label and strip it from every issue."""
        state = self.repo(repo)
        snapshot = self.label_json(state, state.label(name))
        self._commit(state, actor, {"type": "label.deleted", "name": name})
        return snapshot

    # -- milestones --------------------------------------------------------

    def milestone_list(
        self, repo: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """List milestones (`state` open|closed|all, default open)."""
        params = params or {}
        state = self.repo(repo)
        want = (params.get("state") or "open").lower()
        if want not in ("open", "closed", "all"):
            raise JhError(
                422,
                f'invalid argument "{want}" for "-s, --state" flag: valid values are {{open|closed|all}}',
            )
        out = []
        for milestone in sorted(state.milestones.values(), key=lambda m: m["number"]):
            if want != "all" and milestone["state"] != want.upper():
                continue
            out.append(self.milestone_json(state, milestone))
        return out

    def milestone_get(self, repo: str, ref: int | str) -> dict[str, Any]:
        """Return one milestone by number or title."""
        state = self.repo(repo)
        return self.milestone_json(state, state.milestone(ref))

    def milestone_create(
        self, repo: str, actor: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a milestone."""
        state = self.repo(repo)
        title = (data.get("title") or "").strip()
        if not title:
            raise JhError(422, "milestone title can't be blank")
        if any(m["title"] == title for m in state.milestones.values()):
            raise JhError(422, f"milestone {title!r} already exists")
        event: dict[str, Any] = {
            "type": "milestone.created",
            "number": state.next_milestone,
            "title": title,
        }
        if data.get("description"):
            event["description"] = data["description"]
        if data.get("dueOn"):
            event["dueOn"] = _check_due_on(data["dueOn"])
        full = self._commit(state, actor, event)
        return self.milestone_json(state, state.milestones[full["number"]])

    def milestone_edit(
        self, repo: str, actor: str, ref: int | str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Edit a milestone's title, description, due date or state."""
        state = self.repo(repo)
        milestone = state.milestone(ref)
        new_state = (data.get("state") or "").lower() or None
        if new_state == "closed":
            if milestone["state"] == "OPEN":
                self._commit(
                    state,
                    actor,
                    {"type": "milestone.closed", "number": milestone["number"]},
                )
            return self.milestone_json(state, milestone)
        event: dict[str, Any] = {
            "type": "milestone.edited",
            "number": milestone["number"],
        }
        if data.get("title"):
            if any(
                m["title"] == data["title"] and m is not milestone
                for m in state.milestones.values()
            ):
                raise JhError(422, f"milestone {data['title']!r} already exists")
            event["title"] = data["title"]
        if data.get("description") is not None:
            event["description"] = data["description"]
        if "dueOn" in data:
            event["dueOn"] = _check_due_on(data["dueOn"]) if data["dueOn"] else None
        if new_state == "open":
            event["state"] = "OPEN"
        if len(event) == 2:
            raise JhError(422, "nothing to change")
        self._commit(state, actor, event)
        return self.milestone_json(state, milestone)

    # -- events ------------------------------------------------------------

    def issue_history(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Every revision of an issue's title and body, oldest first.

        Revision 1 is the creation; each later `issue.edited` event that
        changed the title or body adds one, so the log itself is the history.

        Returns:
            `[{"rev", "seq", "ts", "actor", "title", "body"}, ...]`.
        """
        state = self.repo(repo)
        state.issue(number)
        out: list[dict[str, Any]] = []
        title = body = ""
        for event in state.events:
            if event.get("number") != number:
                continue
            if event["type"] == "issue.created":
                title, body = event["title"], event.get("body", "")
            elif event["type"] == "issue.edited" and (
                "title" in event or "body" in event
            ):
                title = event.get("title", title)
                body = event.get("body", body)
            else:
                continue
            out.append(
                {
                    "rev": len(out) + 1,
                    "seq": event["seq"],
                    "ts": event["ts"],
                    "actor": event["actor"],
                    "title": title,
                    "body": body,
                }
            )
        return out

    def events_since(self, repo: str, since: int = 0) -> list[dict[str, Any]]:
        """Return the raw events with `seq > since`."""
        state = self.repo(repo)
        return [e for e in state.events if e["seq"] > since]

    # -- JSON rendering ----------------------------------------------------

    def issue_url(self, repo: str, number: int) -> str:
        """Return the issue URL (its reading page on the server; JSON for API clients)."""
        return f"{self.base_url}/{repo}/issues/{number}"

    def issue_json(self, state: RepoState, issue: dict[str, Any]) -> dict[str, Any]:
        """Render an issue with every gh field plus `blockedBy`/`blocking`."""
        repo = state.name
        milestone = None
        if issue["milestone"] is not None and issue["milestone"] in state.milestones:
            m = state.milestones[issue["milestone"]]
            milestone = {
                "number": m["number"],
                "title": m["title"],
                "description": m["description"],
                "dueOn": m["dueOn"],
            }
        return {
            "assignees": [{"login": a, "name": ""} for a in issue["assignees"]],
            "author": {"login": issue["author"], "name": ""},
            "body": issue["body"],
            "closed": issue["state"] == "CLOSED",
            "closedAt": issue["closedAt"],
            "closedByPullRequestsReferences": [],
            "comments": [
                self.comment_json(state, issue["number"], c) for c in issue["comments"]
            ],
            "createdAt": issue["createdAt"],
            "id": f"jh-{repo}-{issue['number']}",
            "isPinned": False,
            "labels": [self._label_ref(state, n) for n in issue["labels"]],
            "milestone": milestone,
            "number": issue["number"],
            "projectCards": [],
            "projectItems": [],
            "reactionGroups": [],
            "state": issue["state"],
            "stateReason": (issue["stateReason"] or "").upper().replace(" ", "_")
            or None
            if issue["state"] == "CLOSED"
            else None,
            "title": issue["title"],
            "updatedAt": issue["updatedAt"],
            "url": self.issue_url(repo, issue["number"]),
            "blockedBy": [self._issue_ref(state, n) for n in issue["blockedBy"]],
            "blocking": [
                self._issue_ref(state, n) for n in state.blocking(issue["number"])
            ],
        }

    def _issue_ref(self, state: RepoState, number: int) -> dict[str, Any]:
        other = state.issues.get(number)
        if other is None:
            return {"number": number, "title": "", "state": "UNKNOWN"}
        return {"number": number, "title": other["title"], "state": other["state"]}

    def _label_ref(self, state: RepoState, name: str) -> dict[str, Any]:
        label = state.labels.get(name)
        return {
            "id": f"jh-{state.name}-label-{name}",
            "name": name,
            "color": label["color"] if label else "ededed",
            "description": label["description"] if label else "",
        }

    def comment_json(
        self, state: RepoState, number: int, comment: dict[str, Any]
    ) -> dict[str, Any]:
        """Render a comment with gh's shape."""
        return {
            "id": f"jh-{state.name}-comment-{comment['id']}",
            "databaseId": comment["id"],
            "author": {"login": comment["author"]},
            "authorAssociation": "NONE",
            "body": comment["body"],
            "createdAt": comment["createdAt"],
            "updatedAt": comment["updatedAt"],
            "includesCreatedEdit": comment["updatedAt"] != comment["createdAt"],
            "isMinimized": False,
            "minimizedReason": "",
            "reactionGroups": [],
            "url": f"{self.issue_url(state.name, number)}#issuecomment-{comment['id']}",
            "viewerDidAuthor": False,
        }

    def label_json(self, state: RepoState, label: dict[str, Any]) -> dict[str, Any]:
        """Render a label with gh's `gh label list --json` fields."""
        return {
            "color": label["color"],
            "createdAt": label["createdAt"],
            "description": label["description"],
            "id": f"jh-{state.name}-label-{label['name']}",
            "isDefault": False,
            "name": label["name"],
            "updatedAt": label["updatedAt"],
            "url": f"{self.base_url}/{state.name}/labels/{label['name']}",
        }

    def milestone_json(
        self, state: RepoState, milestone: dict[str, Any]
    ) -> dict[str, Any]:
        """Render a milestone with GitHub's milestone field names."""
        return {
            "closedAt": milestone["closedAt"],
            "createdAt": milestone["createdAt"],
            "description": milestone["description"],
            "dueOn": milestone["dueOn"],
            "id": f"jh-{state.name}-milestone-{milestone['number']}",
            "number": milestone["number"],
            "state": milestone["state"],
            "title": milestone["title"],
            "updatedAt": milestone["updatedAt"],
            "url": f"{self.base_url}/{state.name}/milestones/{milestone['number']}",
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def select_fields(obj: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Return only the requested fields of a rendered object."""
    return {f: obj.get(f) for f in fields}


def parse_fields(spec: str, allowed: list[str]) -> list[str]:
    """Parse a `--json a,b,c` field spec against the allowed list.

    Raises:
        JhError: With gh's wording when a field is unknown.
    """
    fields = [f.strip() for f in spec.split(",") if f.strip()]
    for field in fields:
        if field not in allowed:
            listing = "\n".join(f"  {f}" for f in sorted(allowed))
            raise JhError(
                400, f'Unknown JSON field: "{field}"\nAvailable fields:\n{listing}'
            )
    return fields


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (int, float)):
        return [str(value)]
    out: list[str] = []
    for item in value:
        out.extend(_as_list(item))
    return out


def _as_numbers(value: Any) -> list[int]:
    numbers = []
    for item in _as_list(value):
        text = str(item).lstrip("#")
        if not text.isdigit():
            raise JhError(422, f"invalid issue number: {item!r}")
        numbers.append(int(text))
    return sorted(set(numbers))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").lower() in ("1", "true", "yes")


def _check_color(color: Any) -> str | None:
    if color in (None, ""):
        return None
    text = str(color).lstrip("#")
    if not _COLOR.match(text):
        raise JhError(
            422,
            f"invalid color {color!r}: the label color needs to be 6 character hex value",
        )
    return text.lower()


def _check_due_on(value: str) -> str:
    text = str(value)
    if _DATE.match(text):
        return f"{text}T00:00:00Z"
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", text):
        return text
    raise JhError(422, f"invalid --due-on {value!r}: expected YYYY-MM-DD")


def _parse_search(query: str) -> dict[str, Any]:
    """Parse the supported subset of GitHub's issue search syntax.

    Supported qualifiers: `is:open|closed`, `state:`, `label:NAME`,
    `milestone:TITLE`, `no:milestone`, `no:label`, `assignee:LOGIN`,
    `author:LOGIN`; remaining words are matched against title and body.
    """
    out: dict[str, Any] = {"labels": [], "text": [], "no": []}
    for token in _split_search(query):
        if ":" in token and not token.startswith(":"):
            key, _, value = token.partition(":")
            key = key.lower()
            if key in ("is", "state"):
                if value.lower() not in ("open", "closed", "issue"):
                    raise JhError(422, f"unsupported search qualifier {token!r}")
                if value.lower() != "issue":
                    out["state"] = value.lower()
            elif key == "label":
                out["labels"].append(value)
            elif key == "milestone":
                out["milestone"] = value
            elif key == "no":
                if value.lower() not in ("milestone", "label", "assignee"):
                    raise JhError(422, f"unsupported search qualifier {token!r}")
                out["no"].append(value.lower())
            elif key in ("assignee", "author"):
                out[key] = value
            else:
                raise JhError(
                    422,
                    f"unsupported search qualifier {token!r}; jh supports "
                    + ", ".join(f"{q}:" for q in SEARCH_QUALIFIERS)
                    + " and free text",
                )
        else:
            out["text"].append(token.lower())
    return out


def _split_search(query: str) -> list[str]:
    tokens = []
    for match in re.finditer(r'(\S+?:"[^"]*"|"[^"]*"|\S+)', query):
        tokens.append(match.group(0).replace('"', ""))
    return tokens


def _search_matches(
    state: RepoState, issue: dict[str, Any], search: dict[str, Any]
) -> bool:
    for name in search["labels"]:
        if name not in issue["labels"]:
            return False
    if "milestone" in search:
        try:
            if issue["milestone"] != state.milestone(search["milestone"])["number"]:
                return False
        except JhError:
            return False
    for missing in search["no"]:
        if missing == "milestone" and issue["milestone"] is not None:
            return False
        if missing == "label" and issue["labels"]:
            return False
        if missing == "assignee" and issue["assignees"]:
            return False
    if "assignee" in search and search["assignee"] not in issue["assignees"]:
        return False
    if "author" in search and issue["author"] != search["author"]:
        return False
    haystack = (issue["title"] + "\n" + issue["body"]).lower()
    return all(word in haystack for word in search["text"])
