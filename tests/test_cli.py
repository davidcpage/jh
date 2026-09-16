"""CLI tests: gh parity from captured fixtures, output shapes, jq, board snapshot."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Callable

import pytest

from jh import jh_cli
from jh.jh_cli import DROPPED_FLAGS, EXTENSION_FLAGS, build_parser, simple_jq
from jh.jh_lib import ISSUE_FIELDS_EXT, ISSUE_FIELDS_GH, LABEL_FIELDS

FIXTURES = Path(__file__).parent / "fixtures" / "gh-2.88.1"

# jh subcommand -> gh help fixture. Everything gh 2.88.1 offers that jh mirrors.
PARITY = {
    ("issue", "create"): "issue_create.txt",
    ("issue", "list"): "issue_list.txt",
    ("issue", "view"): "issue_view.txt",
    ("issue", "edit"): "issue_edit.txt",
    ("issue", "close"): "issue_close.txt",
    ("issue", "reopen"): "issue_reopen.txt",
    ("issue", "comment"): "issue_comment.txt",
    ("issue", "delete"): "issue_delete.txt",
    ("label", "list"): "label_list.txt",
    ("label", "create"): "label_create.txt",
    ("label", "edit"): "label_edit.txt",
    ("label", "delete"): "label_delete.txt",
}


def gh_flags(fixture: str) -> set[str]:
    """Return every option string in the FLAGS section of a gh help text."""
    text = (FIXTURES / fixture).read_text()
    section = text.split("\nFLAGS\n", 1)[1].split("\n\n", 1)[0]
    flags: set[str] = set()
    for line in section.splitlines():
        match = re.match(r"\s+(?:(-\w), )?(--[\w-]+)", line)
        assert match, line
        if match.group(1):
            flags.add(match.group(1))
        flags.add(match.group(2))
    return flags


def gh_json_fields(fixture: str) -> list[str]:
    text = (FIXTURES / fixture).read_text()
    return [line.strip() for line in text.splitlines()[1:] if line.strip()]


def jh_flags(command: str, subcommand: str) -> set[str]:
    parser: Any = build_parser()
    for name in (command, subcommand):
        parser = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices[name]
    flags: set[str] = set()
    for action in parser._actions:
        if action.option_strings in (["-h", "--help"], ["-R", "--repo"]):
            continue
        flags.update(action.option_strings)
    return flags


@pytest.mark.parametrize("command,fixture", PARITY.items(), ids=[" ".join(k) for k in PARITY])
def test_grammar_parity_with_gh(command: tuple[str, str], fixture: str) -> None:
    expected = gh_flags(fixture)
    extensions = EXTENSION_FLAGS.get(" ".join(command), set())
    actual = jh_flags(*command) - extensions
    assert actual == expected, f"jh-only: {sorted(actual - expected)}; gh-only: {sorted(expected - actual)}"


@pytest.mark.parametrize("command", DROPPED_FLAGS)
def test_dropped_flags_are_in_gh(command: str) -> None:
    fixture = PARITY[tuple(command.split())]
    dropped = {f for group in DROPPED_FLAGS[command] for f in group}
    assert dropped <= gh_flags(fixture)


def test_issue_field_parity() -> None:
    assert ISSUE_FIELDS_GH == gh_json_fields("issue_fields.txt")
    assert ISSUE_FIELDS_EXT == ["blockedBy", "blocking"]


def test_label_field_parity() -> None:
    assert LABEL_FIELDS == gh_json_fields("label_fields.txt")


# ---- behaviour ------------------------------------------------------------


def seed(cli: Callable[..., Any]) -> None:
    assert cli("label", "create", "component:noc", "-c", "0e8a16", "-d", "NoC").code == 0
    assert cli("label", "create", "in-progress", "-c", "fbca04").code == 0
    assert cli("milestone", "create", "-t", "Increment 1", "--due-on", "2026-10-01").code == 0
    r = cli("issue", "create", "-t", "Router", "-b", "Port it.", "-l", "component:noc", "-m", "Increment 1")
    assert r.code == 0 and r.out.strip().endswith("/demo/issues/1"), r
    r = cli("issue", "create", "-t", "Latency", "-b", "Measure.", "--blocked-by", "1")
    assert r.code == 0 and r.out.strip().endswith("/demo/issues/2"), r
    r = cli("issue", "create", "-t", "Write-up", "-F", "-", "--blocked-by", "2", stdin="From stdin.")
    assert r.code == 0 and r.out.strip().endswith("/demo/issues/3"), r


def test_create_requires_title_and_body(cli: Callable[..., Any]) -> None:
    r = cli("issue", "create", "-t", "only title")
    assert r.code == 1 and "must provide `--title` and `--body`" in r.err
    r = cli("issue", "create", "-t", "x", "-b", "y", "-l", "missing")
    assert r.code == 1 and "'missing' not found" in r.err


def test_dropped_flags_fail_naming_supported_set(cli: Callable[..., Any]) -> None:
    r = cli("issue", "list", "--web")
    assert r.code == 1 and "--web is not supported by `jh issue list`" in r.err and "--ready" in r.err and "--state" in r.err
    r = cli("issue", "create", "-t", "x", "-b", "y", "-p", "Roadmap")
    assert r.code == 1 and "-p is not supported" in r.err
    r = cli("issue", "pin", "1")
    assert r.code == 1 and "`jh issue pin` is not supported" in r.err and "create, list, view" in r.err
    r = cli("label", "list", "--sort", "name")
    assert r.code == 1 and "--sort is not supported by `jh label list`" in r.err


def test_list_and_view_piped_output(cli: Callable[..., Any]) -> None:
    seed(cli)
    r = cli("issue", "list")
    rows = [line.split("\t") for line in r.out.splitlines()]
    assert [row[:3] for row in rows] == [["3", "OPEN", "Write-up"], ["2", "OPEN", "Latency"], ["1", "OPEN", "Router"]]
    assert rows[2][3] == "component:noc" and re.match(r"\d{4}-\d{2}-\d{2}T", rows[2][4])

    r = cli("issue", "list", "--ready")
    assert [line.split("\t")[0] for line in r.out.splitlines()] == ["1"]
    r = cli("issue", "list", "-l", "component:noc", "-m", "Increment 1", "-s", "all")
    assert [line.split("\t")[0] for line in r.out.splitlines()] == ["1"]
    r = cli("issue", "list", "-S", "no:milestone")
    assert [line.split("\t")[0] for line in r.out.splitlines()] == ["3", "2"]
    r = cli("issue", "list", "-s", "weird")
    assert r.code == 1 and 'invalid argument "weird" for "-s, --state" flag' in r.err

    cli("issue", "comment", "1", "-b", "note one")
    r = cli("issue", "view", "1", "-c")
    assert r.out.startswith("title:\tRouter\nstate:\tOPEN\nauthor:\tclaude\nlabels:\tcomponent:noc\ncomments:\t1\n")
    assert "milestone:\tIncrement 1\nnumber:\t1\n--\nPort it.\n" in r.out
    assert "author:\tclaude\nassociation:\tnone\nedited:\tfalse\nstatus:\tnone\n--\nnote one\n--\n" in r.out
    r = cli("issue", "view", "3")
    assert "--\nFrom stdin.\n" in r.out and "blocked by:\t#2" in r.out
    r = cli("issue", "view", "http://127.0.0.1:7411/demo/issues/2")
    assert r.code == 0 and "title:\tLatency" in r.out
    r = cli("issue", "view", "99")
    assert r.code == 1 and "Could not resolve to an issue with the number of 99" in r.err


def test_json_and_jq(cli: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    seed(cli)
    r = cli("issue", "list", "--json")
    assert r.code == 1 and r.err.startswith("Specify one or more comma-separated fields for `--json`:\n  assignees\n")
    r = cli("issue", "list", "--json", "nope")
    assert r.code == 1 and r.err.startswith('Unknown JSON field: "nope"\nAvailable fields:\n')
    r = cli("issue", "list", "-q", ".x")
    assert r.code == 1 and "cannot use `--jq` without specifying `--json`" in r.err

    r = cli("issue", "list", "--json", "number,title,labels", "-s", "all")
    data = json.loads(r.out)
    assert [d["number"] for d in data] == [3, 2, 1]
    assert set(data[0]) == {"number", "title", "labels"}
    assert data[2]["labels"] == [{"id": "jh-demo-label-component:noc", "name": "component:noc", "color": "0e8a16", "description": "NoC"}]

    r = cli("issue", "view", "3", "--json", "number,title,state,labels,blockedBy", "-q", ".blockedBy[].number")
    assert r.code == 0 and r.out == "2\n"
    r = cli("issue", "view", "1", "--json", "milestone,author,url,id,isPinned,projectCards,reactionGroups,stateReason,closed")
    one = json.loads(r.out)
    assert one["milestone"] == {"number": 1, "title": "Increment 1", "description": "", "dueOn": "2026-10-01T00:00:00Z"}
    assert one["author"]["login"] == "claude" and one["id"] == "jh-demo-1" and one["isPinned"] is False
    assert one["projectCards"] == [] and one["reactionGroups"] == [] and one["stateReason"] is None and one["closed"] is False

    # Force the built-in jq subset.
    monkeypatch.setattr(jh_cli.shutil, "which", lambda name: None)
    r = cli("issue", "list", "--json", "number,title", "-s", "all", "-q", ".[].number")
    assert r.out == "3\n2\n1\n"
    r = cli("issue", "view", "3", "--json", "blockedBy", "-q", ".blockedBy[].title")
    assert r.out == "Latency\n"
    r = cli("issue", "list", "--json", "number", "-q", "map(.number)")
    assert r.code == 1 and "install jq" in r.err


def test_simple_jq_subset() -> None:
    data = {"a": [{"b": 1, "c": "x"}, {"b": 2, "c": "y"}], "n": None}
    assert simple_jq(".a[].b", data) == [1, 2]
    assert simple_jq(".a[0].c", data) == ["x"]
    assert simple_jq(".a[-1].c", data) == ["y"]
    assert simple_jq(".a | length", data) == [2]
    assert simple_jq(".n.x", data) == [None]
    assert simple_jq(".", 5) == [5]
    assert simple_jq("keys", {"z": 1, "a": 2}) == [["a", "z"]]
    assert simple_jq(".[] | .b", data["a"]) == [1, 2]


def test_edit_close_reopen_comment_delete(cli: Callable[..., Any]) -> None:
    seed(cli)
    r = cli("issue", "edit", "1", "2", "--add-label", "in-progress", "--add-assignee", "@me")
    assert r.code == 0 and r.out.count("/demo/issues/") == 2
    one = json.loads(cli("issue", "view", "1", "--json", "labels,assignees").out)
    assert [l["name"] for l in one["labels"]] == ["component:noc", "in-progress"]
    assert one["assignees"] == [{"login": "claude", "name": ""}]
    r = cli("issue", "edit", "1", "--remove-label", "in-progress", "--remove-milestone", "-t", "Router v2")
    one = json.loads(cli("issue", "view", "1", "--json", "labels,milestone,title").out)
    assert [l["name"] for l in one["labels"]] == ["component:noc"] and one["milestone"] is None and one["title"] == "Router v2"
    r = cli("issue", "edit", "1", "--add-blocked-by", "3")
    assert r.code == 1 and "cycle" in r.err and "failed to update 1 issue" in r.err
    r = cli("issue", "edit", "1", "--add-label", "nope")
    assert r.code == 1 and "'nope' not found" in r.err

    r = cli("issue", "close", "1", "-r", "not planned", "-c", "Superseded.")
    assert r.code == 0 and r.err.strip() == "✓ Closed issue demo#1 (Router v2)"
    r = cli("issue", "close", "1")
    assert r.code == 0 and "is already closed" in r.err
    one = json.loads(cli("issue", "view", "1", "--json", "state,stateReason,closed,closedAt,comments").out)
    assert one["state"] == "CLOSED" and one["stateReason"] == "NOT_PLANNED" and one["closed"] and one["closedAt"]
    assert one["comments"][-1]["body"] == "Superseded."
    r = cli("issue", "close", "2", "-r", "bogus")
    assert r.code == 1 and 'invalid argument "bogus" for "-r, --reason" flag' in r.err
    r = cli("issue", "close", "3", "--duplicate-of", "2")
    assert json.loads(cli("issue", "view", "3", "--json", "stateReason").out)["stateReason"] == "DUPLICATE"

    assert [l.split("\t")[0] for l in cli("issue", "list", "--ready").out.splitlines()] == ["2"]
    r = cli("issue", "reopen", "1", "-c", "Back on.")
    assert r.code == 0 and r.err.strip() == "✓ Reopened issue demo#1 (Router v2)"
    assert [l.split("\t")[0] for l in cli("issue", "list", "--ready").out.splitlines()] == ["1"]

    r = cli("issue", "comment", "2", "-b", "mine")
    assert r.code == 0 and r.out.strip().endswith("/demo/issues/2#issuecomment-3")
    r = cli("issue", "comment", "2", "-b", "theirs", actor="dpage")
    assert r.code == 0
    r = cli("issue", "comment", "2", "--edit-last", "-b", "mine, edited")
    assert r.code == 0
    comments = json.loads(cli("issue", "view", "2", "--json", "comments").out)["comments"]
    assert [(c["author"]["login"], c["body"]) for c in comments] == [("claude", "mine, edited"), ("dpage", "theirs")]
    r = cli("issue", "comment", "2", "--delete-last")
    assert r.code == 1 and "--yes required" in r.err
    r = cli("issue", "comment", "2", "--delete-last", "--yes")
    assert r.code == 0
    comments = json.loads(cli("issue", "view", "2", "--json", "comments").out)["comments"]
    assert [c["author"]["login"] for c in comments] == ["dpage"]
    r = cli("issue", "comment", "2")
    assert r.code == 1 and "must provide `--body`" in r.err

    r = cli("issue", "delete", "3")
    assert r.code == 1 and "--yes required" in r.err
    r = cli("issue", "delete", "3", "--yes")
    assert r.code == 0 and r.err.strip() == "✓ Deleted issue demo#3 (Write-up)."
    assert cli("issue", "view", "3").code == 1
    r = cli("issue", "create", "-t", "Fourth", "-b", "")
    assert r.out.strip().endswith("/demo/issues/4")


def test_labels_and_milestones(cli: Callable[..., Any]) -> None:
    seed(cli)
    r = cli("label", "list")
    assert r.out == "component:noc\tNoC\t#0e8a16\nin-progress\t\t#fbca04\n"
    r = cli("label", "list", "--json", "name,color,isDefault", "-S", "noc")
    assert json.loads(r.out) == [{"name": "component:noc", "color": "0e8a16", "isDefault": False}]
    r = cli("label", "create", "component:noc")
    assert r.code == 1 and "already exists" in r.err
    r = cli("label", "create", "component:noc", "-f", "-d", "renamed desc")
    assert r.code == 0
    r = cli("label", "create", "bad", "-c", "12345")
    assert r.code == 1 and "6 character hex" in r.err
    r = cli("label", "edit", "component:noc", "-n", "noc", "-c", "#ABCDEF")
    assert r.code == 0
    assert json.loads(cli("issue", "view", "1", "--json", "labels").out)["labels"][0]["name"] == "noc"
    assert cli("label", "delete", "noc").code == 1
    assert cli("label", "delete", "noc", "--yes").code == 0
    assert json.loads(cli("issue", "view", "1", "--json", "labels").out)["labels"] == []

    r = cli("milestone", "list")
    assert r.out == "1\tIncrement 1\t2026-10-01\tOPEN\t\n"
    r = cli("milestone", "list", "--json", "number,title,dueOn,state")
    assert json.loads(r.out) == [{"number": 1, "title": "Increment 1", "dueOn": "2026-10-01T00:00:00Z", "state": "OPEN"}]
    r = cli("milestone", "edit", "1", "-d", "First increment", "--due-on", "2026-11-01")
    assert r.code == 0
    r = cli("milestone", "close", "Increment 1")
    assert r.code == 0 and cli("milestone", "list").out == ""
    assert cli("milestone", "list", "-s", "closed").out.startswith("1\tIncrement 1\t2026-11-01\tCLOSED\tFirst increment")
    r = cli("milestone", "edit", "1", "--reopen")
    assert r.code == 0 and cli("milestone", "list").out.startswith("1\t")
    r = cli("milestone", "create", "-t", "Increment 1")
    assert r.code == 1 and "already exists" in r.err
    r = cli("issue", "create", "-t", "x", "-b", "y", "-m", "Nope")
    assert r.code == 1 and "no milestone found" in r.err


def test_repo_resolution(cli: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    seed(cli)
    monkeypatch.delenv("JH_REPO")
    r = cli("issue", "list")
    assert r.code == 1 and "no repository specified" in r.err and "Known repos: demo" in r.err
    r = cli("issue", "list", "-R", "demo")
    assert r.code == 0 and len(r.out.splitlines()) == 3
    r = cli("issue", "list", "-R", "other")
    assert r.code == 1 and "repo 'other' not found; known repos: demo" in r.err
    r = cli("repo", "list")
    assert r.out.startswith("demo\t3 open issues\t")
    assert cli("repo", "create", "demo").code == 1


def test_unreachable_server(cli: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JH_SERVER", "http://127.0.0.1:1")
    r = cli("issue", "list")
    assert r.code == 1 and "could not reach jh-server at http://127.0.0.1:1" in r.err and "ssh -L 7411" in r.err


def test_board_url_and_snapshot(cli: Callable[..., Any], tmp_path: Path, running: Any) -> None:
    seed(cli)
    r = cli("board")
    assert r.code == 0 and r.out.strip() == f"{running.url}/demo/board"
    target = tmp_path / "board.html"
    r = cli("board", "--snapshot", str(target))
    assert r.code == 0 and target.exists()
    page = target.read_text()
    assert "<title>demo board (snapshot)</title>" in page
    assert "var LIVE = false;" in page
    assert '"title": "Write-up"' in page and '"Increment 1"' in page
    assert "cdnjs.cloudflare.com/ajax/libs/mermaid" in page
    # Bodies and comments render as markdown in the browser; both libraries are
    # loaded from the one CDN host and the page degrades to plain text without them.
    assert "cdnjs.cloudflare.com/ajax/libs/marked" in page
    assert "window.marked.parse(" in page and 'class="body plain"' in page
    assert "https://" not in page.replace("https://cdnjs.cloudflare.com", "")


def test_tty_layouts(cli: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    seed(cli)
    cli("issue", "comment", "1", "-b", "note")
    monkeypatch.setattr(jh_cli, "is_tty", lambda: True)
    monkeypatch.setattr(jh_cli.shutil, "get_terminal_size", lambda fallback=None: type("S", (), {"columns": 100})())
    r = cli("issue", "list")
    lines = r.out.splitlines()
    assert lines[1] == "Showing 3 of 3 open issues in demo"
    assert lines[3].split() == ["ID", "TITLE", "LABELS", "UPDATED"]
    assert lines[4].startswith("#3") and lines[6].startswith("#1") and "less than a minute ago" in lines[6]
    r = cli("issue", "view", "1")
    lines = r.out.splitlines()
    assert lines[0] == "Router demo#1"
    assert lines[1].startswith("Open • claude opened less than a minute ago • 1 comment")
    assert lines[2] == "Labels: component:noc" and lines[3] == "Milestone: Increment 1"
    assert "  Port it." in lines and lines[-1].startswith("View this issue on jh: ")
    r = cli("issue", "list", "-s", "closed")
    assert r.code == 1 and "no closed issues match your search in demo" in r.err
    r = cli("label", "list")
    assert "Showing 2 of 2 labels in demo" in r.out and "#0e8a16" in r.out
