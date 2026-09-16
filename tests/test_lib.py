"""Store tests: replay, dependencies, labels, deletion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jh.jh_lib import EVENT_TYPES, JhError, Store


def seed(store: Store) -> None:
    store.repo_create("demo", "claude")
    store.label_create("demo", "claude", {"name": "bug", "color": "d73a4a"})
    store.label_create("demo", "claude", {"name": "in-progress", "color": "fbca04"})
    store.milestone_create(
        "demo", "claude", {"title": "Increment 1", "dueOn": "2026-10-01"}
    )
    store.issue_create(
        "demo",
        "claude",
        {"title": "Router", "body": "a", "labels": ["bug"], "milestone": "Increment 1"},
    )
    store.issue_create(
        "demo", "claude", {"title": "Latency", "body": "b", "blockedBy": [1]}
    )
    store.issue_create(
        "demo", "claude", {"title": "Write-up", "body": "c", "blockedBy": [2]}
    )
    store.comment_create("demo", "claude", 1, "first")


def snapshot(store: Store) -> dict:
    return {
        "issues": store.issue_list("demo", {"state": "all"}),
        "labels": store.label_list("demo"),
        "milestones": store.milestone_list("demo", {"state": "all"}),
        "events": store.events_since("demo"),
    }


def test_replay_reproduces_state(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    store.issue_edit(
        "demo", "claude", 1, {"addLabels": ["in-progress"], "title": "Router v2"}
    )
    store.issue_edit(
        "demo",
        "claude",
        1,
        {"state": "closed", "stateReason": "not planned", "comment": "bye"},
    )
    store.label_edit("demo", "claude", "bug", {"name": "defect"})
    store.milestone_edit("demo", "claude", 1, {"state": "closed"})
    before = snapshot(store)
    again = Store(tmp_path, base_url="http://x")
    assert snapshot(again) == before
    assert again.repos["demo"].next_number == 4
    assert again.repos["demo"].next_comment_id == 3


def test_log_is_one_json_object_per_line_with_seq_ts_actor(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    lines = (tmp_path / "demo" / "events.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all({"seq", "ts", "actor", "type"} <= set(e) for e in events)
    assert all(e["type"] in EVENT_TYPES for e in events)
    assert events[0]["type"] == "repo.created"
    created = [e for e in events if e["type"] == "issue.created"]
    assert created[1]["blockedBy"] == [1]


def test_ready_flips_when_blocker_closes_and_back_when_reopened(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    ready = lambda: [i["number"] for i in store.issue_list("demo", {"ready": "1"})]  # noqa: E731
    assert ready() == [1]
    store.issue_edit("demo", "claude", 1, {"state": "closed"})
    assert ready() == [2]
    store.issue_edit("demo", "claude", 1, {"state": "open"})
    assert ready() == [1]


def test_a_draft_is_never_ready(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    ready = lambda: [i["number"] for i in store.issue_list("demo", {"ready": "1"})]  # noqa: E731
    assert ready() == [1]
    store.label_create("demo", "claude", {"name": "draft", "color": "6e7781"})
    store.issue_edit("demo", "claude", 1, {"addLabels": ["draft"]})
    assert ready() == []
    store.issue_edit("demo", "claude", 1, {"removeLabels": ["draft"]})
    assert ready() == [1]


def test_cycles_are_rejected(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    with pytest.raises(JhError) as err:
        store.issue_edit("demo", "claude", 1, {"addBlockedBy": [3]})
    assert "cycle" in err.value.message
    with pytest.raises(JhError):
        store.issue_edit("demo", "claude", 1, {"addBlockedBy": [1]})
    # Nothing was written.
    assert store.repos["demo"].issues[1]["blockedBy"] == []


def test_blocked_by_and_blocking_fields(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    two = store.issue_get("demo", 2)
    assert two["blockedBy"] == [{"number": 1, "title": "Router", "state": "OPEN"}]
    assert two["blocking"] == [{"number": 3, "title": "Write-up", "state": "OPEN"}]
    assert [i["number"] for i in store.issue_list("demo", {"blocking": 3})] == [2]
    assert [i["number"] for i in store.issue_list("demo", {"blocked_by": 1})] == [2]


def test_label_delete_and_rename_propagate_to_issues(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    store.label_edit("demo", "claude", "bug", {"name": "defect", "color": "#00FF00"})
    assert [l["name"] for l in store.issue_get("demo", 1)["labels"]] == ["defect"]
    assert store.repos["demo"].labels["defect"]["color"] == "00ff00"
    store.label_delete("demo", "claude", "defect")
    assert store.issue_get("demo", 1)["labels"] == []
    with pytest.raises(JhError):
        store.issue_create(
            "demo", "claude", {"title": "x", "body": "", "labels": ["defect"]}
        )


def test_deleted_numbers_are_never_reused(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    store.issue_delete("demo", "claude", 3)
    with pytest.raises(JhError):
        store.issue_get("demo", 3)
    new = store.issue_create("demo", "claude", {"title": "next", "body": ""})
    assert new["number"] == 4
    again = Store(tmp_path, base_url="http://x")
    assert again.repos["demo"].next_number == 5
    assert again.issue_get("demo", 2)["blocking"] == []


def test_close_records_state_reason_and_duplicate(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    closed = store.issue_edit(
        "demo", "claude", 3, {"state": "closed", "duplicateOf": 2}
    )
    assert (
        closed["state"] == "CLOSED"
        and closed["stateReason"] == "DUPLICATE"
        and closed["closed"] is True
    )
    closed = store.issue_edit(
        "demo", "claude", 2, {"state": "closed", "stateReason": "not planned"}
    )
    assert closed["stateReason"] == "NOT_PLANNED"
    with pytest.raises(JhError):
        store.issue_edit(
            "demo", "claude", 1, {"state": "closed", "stateReason": "wontfix"}
        )


def test_close_drops_the_in_progress_label(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    store.issue_edit("demo", "claude", 2, {"addLabels": ["in-progress", "bug"]})
    closed = store.issue_edit("demo", "claude", 2, {"state": "closed"})
    assert [l["name"] for l in closed["labels"]] == ["bug"]
    kinds = [e["type"] for e in store.events_since("demo")][-2:]
    assert kinds == ["issue.unlabeled", "issue.closed"]
    # Reopening does not bring it back: nobody is on the issue.
    reopened = store.issue_edit("demo", "claude", 2, {"state": "open"})
    assert [l["name"] for l in reopened["labels"]] == ["bug"]
    # Closing an issue that never had the label emits no unlabel event.
    before = len(store.events_since("demo"))
    store.issue_edit("demo", "claude", 3, {"state": "closed"})
    assert len(store.events_since("demo")) == before + 1


def test_search_qualifiers(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    numbers = lambda q: [
        i["number"] for i in store.issue_list("demo", {"search": q, "state": "all"})
    ]  # noqa: E731
    assert numbers("no:milestone") == [3, 2]
    assert numbers('milestone:"Increment 1"') == [1]
    assert numbers("label:bug") == [1]
    assert numbers("no:label") == [3, 2]
    assert numbers("write") == [3]
    assert numbers("is:open router") == [1]
    with pytest.raises(JhError):
        numbers("sort:created-asc")


def test_comments_only_editable_by_author(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    with pytest.raises(JhError) as err:
        store.comment_edit("demo", "dpage", 1, "hijack")
    assert err.value.status == 403
    store.comment_edit("demo", "claude", 1, "edited")
    comment = store.issue_get("demo", 1)["comments"][0]
    assert comment["body"] == "edited" and comment["author"]["login"] == "claude"
    store.comment_delete("demo", "claude", 1)
    assert store.issue_get("demo", 1)["comments"] == []


def test_partial_trailing_line_is_ignored(tmp_path: Path) -> None:
    store = Store(tmp_path, base_url="http://x")
    seed(store)
    log = tmp_path / "demo" / "events.jsonl"
    with log.open("a") as handle:
        handle.write('{"seq": 99, "ts": "2026-')
    again = Store(tmp_path, base_url="http://x")
    assert again.repos["demo"].seq == store.repos["demo"].seq
