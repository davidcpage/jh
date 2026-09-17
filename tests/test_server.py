"""Server tests: REST subset, concurrency, replay across restart."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any
from urllib import error, request

import pytest


def call(
    base: str,
    method: str,
    path: str,
    body: dict | None = None,
    actor: str = "claude",
    accept: str = "application/json",
) -> tuple[int, Any, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(base + path, data=data, method=method)
    req.add_header("Accept", accept)
    req.add_header("X-JH-Actor", actor)
    if data is not None:
        req.add_header("Content-Type", "application/json")

    class NoRedirect(request.HTTPRedirectHandler):
        def redirect_request(self, *a: Any, **k: Any) -> None:
            return None

    opener = request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=10) as resp:
            raw = resp.read()
            return (
                resp.status,
                (
                    json.loads(raw)
                    if resp.headers.get("Content-Type", "").startswith(
                        "application/json"
                    )
                    else raw.decode()
                ),
                dict(resp.headers),
            )
    except error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw), dict(err.headers)
        except json.JSONDecodeError:
            return err.code, raw.decode(), dict(err.headers)


def seed(base: str) -> None:
    assert call(base, "POST", "/repos", {"name": "demo"})[0] == 200
    assert (
        call(base, "POST", "/demo/labels", {"name": "bug", "color": "d73a4a"})[0]
        == 201
    )
    assert call(base, "POST", "/demo/milestones", {"title": "Increment 1"})[0] == 201
    assert (
        call(
            base,
            "POST",
            "/demo/issues",
            {
                "title": "Router",
                "body": "a",
                "labels": ["bug"],
                "milestone": "Increment 1",
            },
        )[0]
        == 201
    )
    assert (
        call(
            base,
            "POST",
            "/demo/issues",
            {"title": "Latency", "body": "b", "blockedBy": [1]},
        )[0]
        == 201
    )


def test_rest_surface(running: Any) -> None:
    base = running.url
    seed(base)
    status, repos, _ = call(base, "GET", "/repos")
    assert status == 200 and [r["name"] for r in repos] == ["demo"]

    status, issues, _ = call(base, "GET", "/demo/issues?state=all")
    assert status == 200 and [i["number"] for i in issues] == [2, 1]
    status, ready, _ = call(base, "GET", "/demo/issues?ready=1")
    assert [i["number"] for i in ready] == [1]
    status, by_label, _ = call(base, "GET", "/demo/issues?labels=bug")
    assert [i["number"] for i in by_label] == [1]

    status, one, _ = call(base, "GET", "/demo/issues/1")
    assert (
        status == 200
        and one["url"] == f"{base}/demo/issues/1"
        and one["id"] == "jh-demo-1"
    )
    assert (
        one["milestone"]["title"] == "Increment 1"
        and one["labels"][0]["color"] == "d73a4a"
    )

    status, edited, _ = call(
        base, "PATCH", "/demo/issues/1", {"state": "closed", "comment": "done"}
    )
    assert (
        status == 200
        and edited["state"] == "CLOSED"
        and edited["stateReason"] == "COMPLETED"
    )
    assert len(edited["comments"]) == 1
    status, ready, _ = call(base, "GET", "/demo/issues?ready=1")
    assert [i["number"] for i in ready] == [2]

    status, comment, _ = call(
        base, "POST", "/demo/issues/2/comments", {"body": "hi"}
    )
    assert status == 201
    status, _, _ = call(
        base,
        "PATCH",
        f"/demo/issues/comments/{comment['databaseId']}",
        {"body": "hi2"},
        actor="dpage",
    )
    assert status == 403
    status, comments, _ = call(base, "GET", "/demo/issues/2/comments")
    assert [c["body"] for c in comments] == ["hi"]
    status, _, _ = call(
        base, "DELETE", f"/demo/issues/comments/{comment['databaseId']}"
    )
    assert status == 200

    status, labels, _ = call(base, "GET", "/demo/labels")
    assert [label["name"] for label in labels] == ["bug"]
    status, _, _ = call(base, "PATCH", "/demo/labels/bug", {"name": "defect"})
    assert status == 200
    assert call(base, "GET", "/demo/issues/1")[1]["labels"][0]["name"] == "defect"

    status, ms, _ = call(base, "GET", "/demo/milestones")
    assert [m["title"] for m in ms] == ["Increment 1"]
    status, closed, _ = call(
        base, "PATCH", "/demo/milestones/1", {"state": "closed"}
    )
    assert closed["state"] == "CLOSED"

    status, events, _ = call(base, "GET", "/demo/events?since=0")
    assert (
        status == 200
        and events[0]["type"] == "repo.created"
        and events[-1]["type"] == "milestone.closed"
    )
    seq = events[-1]["seq"]
    assert call(base, "GET", f"/demo/events?since={seq}")[1] == []

    status, _, _ = call(base, "DELETE", "/demo/issues/2")
    assert status == 200
    assert call(base, "GET", "/demo/issues/2")[0] == 404
    assert call(base, "GET", "/nope/issues")[0] == 404
    assert "known repos: demo" in call(base, "GET", "/nope/issues")[1]["message"]
    assert call(base, "PATCH", "/demo/labels", {})[0] == 405


def test_board_reading_page_and_browser_redirect(running: Any) -> None:
    base = running.url
    seed(base)
    status, page, headers = call(base, "GET", "/demo/board", accept="text/html")
    assert (
        status == 200
        and "<title>demo board</title>" in page
        and '"repo": "demo"' in page
    )
    assert "issuePage(issue.number)" in page
    assert ".card .body th, .card .body td { white-space: nowrap; }" in page
    status, data, _ = call(base, "GET", "/demo/board?format=json")
    assert (
        status == 200
        and data["repo"] == "demo"
        and len(data["issues"]) == 2
        and data["seq"] > 0
    )
    status, page, headers = call(
        base, "GET", "/demo/issues/2", accept="text/html,application/xhtml+xml"
    )
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "<title>#2 Latency · demo</title>" in page
    assert '"number": 2' in page and '"blockedBy": [{"number": 1' in page
    assert "cdnjs.cloudflare.com/ajax/libs/marked/" in page
    # The board's card titles link to the reading page.
    assert "/issues/\" + issue.number" in page or "/issues/" in page
    assert call(base, "GET", "/demo/issues/9", accept="text/html")[0] == 404
    status, _, headers = call(base, "GET", "/demo", accept="text/html")
    assert status == 302 and headers["Location"] == "/demo/board"
    status, index, _ = call(base, "GET", "/", accept="text/html")
    assert status == 200 and "/demo/board" in index


def test_docs_viewer_serves_markdown_and_assets(tmp_path: Path) -> None:
    from conftest import RunningServer

    root = tmp_path / "docs"
    (root / "increments" / "fig").mkdir(parents=True)
    (root / "index.md").write_text(
        "# Design\n\nSee [the plan](plan.md) and ![fig](increments/fig/a.svg).\n"
    )
    (root / "plan.md").write_text("# Plan\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    (root / "increments" / "00.md").write_text(
        "# Increment 0\n\n```mermaid\ngraph LR; a-->b\n```\n"
    )
    (root / "increments" / "fig" / "a.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'/>"
    )
    (root / ".hidden.md").write_text("# no\n")
    srv = RunningServer(tmp_path / "home", docs={"demo": root})
    try:
        base = srv.url
        status, _, headers = call(base, "GET", "/demo/docs", accept="text/html")
        assert status == 302 and headers["Location"] == "/demo/docs/index.md"
        status, _, headers = call(
            base, "GET", "/demo/docs/increments/", accept="text/html"
        )
        assert status == 302 and headers["Location"] == "/demo/docs/increments/00.md"

        status, page, headers = call(
            base, "GET", "/demo/docs/index.md", accept="text/html"
        )
        assert status == 200 and headers["Content-Type"].startswith("text/html")
        assert "<title>Design · demo docs</title>" in page
        assert '"path": "increments/00.md"' in page and '"title": "Increment 0"' in page
        assert ".hidden.md" not in page
        assert "cdnjs.cloudflare.com/ajax/libs/marked/" in page

        # The board learns the docs tree so issue text can link into it.
        call(base, "POST", "/repos", {"name": "demo"})
        status, data, _ = call(base, "GET", "/demo/board?format=json")
        assert status == 200 and data["docs"]["base"] == root.name
        assert "increments/00.md" in data["docs"]["files"]
        assert ".hidden.md" not in data["docs"]["files"]

        status, raw, headers = call(base, "GET", "/demo/docs/plan.md?format=raw")
        assert (
            status == 200
            and headers["Content-Type"].startswith("text/markdown")
            and raw.startswith("# Plan")
        )

        status, tree, _ = call(base, "GET", "/demo/docs?format=json")
        assert status == 200 and [f["path"] for f in tree["files"]] == [
            "index.md",
            "plan.md",
            "increments/00.md",
        ]
        assert tree["files"][2]["dir"] == "increments"

        status, svg, headers = call(base, "GET", "/demo/docs/increments/fig/a.svg")
        assert (
            status == 200
            and headers["Content-Type"] == "image/svg+xml"
            and svg.startswith("<svg")
        )

        for bad in (
            "/demo/docs/missing.md",
            "/demo/docs/../../etc/passwd",
            "/demo/docs/increments/fig/../../../home",
        ):
            status, body, _ = call(base, "GET", bad)
            assert status == 404, (bad, status, body)
        status, body, _ = call(base, "GET", "/other/docs/index.md")
        assert status == 404 and "--docs other=DIR" in body["message"]
    finally:
        srv.stop()


def test_twenty_parallel_creates_get_consecutive_numbers(running: Any) -> None:
    base = running.url
    call(base, "POST", "/repos", {"name": "demo"})
    results: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        status, issue, _ = call(
            base, "POST", "/demo/issues", {"title": f"issue {i}", "body": ""}
        )
        with lock:
            if status == 201:
                results.append(issue["number"])
            else:
                errors.append(str(issue))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert sorted(results) == list(range(1, 21))
    events = call(base, "GET", "/demo/events?since=1")[1]
    assert [e["seq"] for e in events] == list(range(2, 22))


def test_replay_after_restart_gives_identical_gets(running: Any) -> None:
    base = running.url
    seed(base)
    call(
        base,
        "PATCH",
        "/demo/issues/1",
        {"addLabels": ["bug"], "title": "Router v2", "comment": "note"},
    )
    call(
        base,
        "PATCH",
        "/demo/issues/2",
        {"state": "closed", "stateReason": "not planned"},
    )
    call(
        base,
        "POST",
        "/demo/issues",
        {"title": "Third", "body": "", "blockedBy": [1, 2]},
    )
    call(base, "PATCH", "/demo/labels/bug", {"color": "00ff00", "description": "d"})
    paths = [
        "/repos",
        "/demo/issues?state=all",
        "/demo/issues/3",
        "/demo/labels",
        "/demo/milestones?state=all",
        "/demo/events",
        "/demo/board?format=json",
    ]
    before = {p: call(base, "GET", p)[1] for p in paths}
    running.restart()
    base2 = running.url
    after = {p: call(base2, "GET", p)[1] for p in paths}

    def scrub(obj: Any) -> Any:
        text = json.dumps(obj).replace(base2, base)
        return json.loads(text)

    for path in paths:
        assert scrub(after[path]) == before[path], path


@pytest.mark.parametrize("bad", [b"[1,2]", b"{not json"])
def test_bad_bodies_are_400(running: Any, bad: bytes) -> None:
    req = request.Request(running.url + "/repos", data=bad, method="POST")
    req.add_header("Content-Type", "application/json")
    with pytest.raises(error.HTTPError) as err:
        request.urlopen(req, timeout=5)
    assert err.value.code == 400


def test_book_lists_concepts_by_milestone_with_state(running: Any) -> None:
    base = running.url
    seed(base)
    call(base, "POST", "/demo/labels", {"name": "kind:concept", "color": "0075ca"})
    call(base, "POST", "/demo/milestones", {"title": "0 · tracer"})
    for title, ms in (("Later concept", "Increment 1"), ("Early concept", "0 · tracer"),
                      ("Orphan concept", None), ("Open concept", "0 · tracer")):
        body = {"title": title, "body": "", "labels": ["kind:concept"]}
        if ms:
            body["milestone"] = ms
        assert call(base, "POST", "/demo/issues", body)[0] == 201
    # 3..5 close; 6 stays open; 1 closes without the label.
    for n in (3, 4, 5, 1):
        call(base, "PATCH", f"/demo/issues/{n}", {"state": "closed"})

    call(base, "POST", "/demo/labels", {"name": "draft", "color": "888888"})
    call(base, "POST", "/demo/issues", {"title": "Draft concept", "body": "",
                                        "labels": ["kind:concept", "draft"], "milestone": "0 · tracer"})
    call(base, "POST", "/demo/issues", {"title": "Blocked concept", "body": "",
                                        "labels": ["kind:concept"], "blockedBy": [6]})
    call(base, "POST", "/demo/labels", {"name": "kind:docs", "color": "0075ca"})
    call(base, "POST", "/demo/issues", {"title": "Glossary", "body": "",
                                        "labels": ["kind:docs"], "milestone": "Increment 1"})

    status, book, _ = call(base, "GET", "/demo/book?format=json")
    assert status == 200
    assert [(g["title"], [(i["number"], i["column"], i["kind"]) for i in g["issues"]]) for g in book["groups"]] == [
        ("0 · tracer", [(4, "closed", "concept"), (6, "ready", "concept"), (7, "draft", "concept")]),
        ("Increment 1", [(3, "closed", "concept"), (9, "ready", "docs")]),
        ("No milestone", [(5, "closed", "concept"), (8, "blocked", "concept")]),
    ]

    status, page, headers = call(base, "GET", "/demo/book", accept="text/html")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "<title>demo book</title>" in page and "3 of 7 chapters finished" in page
    assert page.index("0 · tracer") < page.index("Increment 1") < page.index("No milestone")
    assert '<li class="concept"><a href="/demo/issues/4" title="#4">Early concept</a></li>' in page
    assert '<li class="draft concept"><a href="/demo/issues/7" title="#7">Draft concept</a> <span class="tag draft">draft</span>' in page
    assert ('<li class="ready docs"><a href="/demo/issues/9" title="#9">Glossary</a>'
            ' <span class="tag kind">docs</span> <span class="tag ready">ready</span>') in page
    assert '<span class="tag blocked">blocked</span>' in page and '<span class="tag ready">ready</span>' in page
    assert "Router" not in page  # not a concept
    assert 'id="finished"' in page
    assert 'href="/demo/docs/"' not in page and 'href="/demo/board"' in page
    assert call(base, "GET", "/nope/book")[0] == 404


def test_issue_history_is_title_and_body_revisions(running: Any) -> None:
    base = running.url
    seed(base)
    call(base, "PATCH", "/demo/issues/1", {"body": "a b c"}, actor="alice")
    call(base, "PATCH", "/demo/issues/1", {"addLabels": ["bug"]})  # not a revision
    call(base, "PATCH", "/demo/issues/1", {"title": "Router v2"}, actor="bob")
    status, hist, _ = call(base, "GET", "/demo/issues/1/history")
    assert status == 200
    assert [(h["rev"], h["actor"], h["title"], h["body"]) for h in hist] == [
        (1, "claude", "Router", "a"),
        (2, "alice", "Router", "a b c"),
        (3, "bob", "Router v2", "a b c"),
    ]
    assert [h["seq"] for h in hist] == sorted(h["seq"] for h in hist)
    assert call(base, "GET", "/demo/issues/9/history")[0] == 404
    # The reading page carries the same revisions for its #diff=A..B view.
    status, page, _ = call(base, "GET", "/demo/issues/1", accept="text/html")
    assert status == 200 and '"rev": 3' in page and "#diff=" in page and "function markup(" in page


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_diff_markup_keeps_markdown_structure(tmp_path: Path) -> None:
    """The word diff marks changed words without breaking block syntax."""
    from jh.board import DIFF_JS

    script = tmp_path / "diff.js"
    script.write_text(
        "var esc = function (s) { return String(s); };\n" + DIFF_JS + """
var show = function (s) { return s.replace(/\uE000/g, "{+").replace(/\uE001/g, "+}").replace(/\uE002/g, "[-").replace(/\uE003/g, "-]"); };
var cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(JSON.stringify(cases.map(function (c) { return show(markup(c[0], c[1])); })));
"""
    )
    cases = [
        ["the quick fox", "the slow fox"],
        ["# Title\n\npara\n", "# New Title\n\npara\n\nextra\n"],
        ["- a\n- b\n", "- a\n- b\n- c\n"],
        ["| a | b |\n|---|---|\n| 1 | 2 |\n", "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"],
        ["same", "same"],
        ["", "new"],
        ["gone", ""],
    ]
    out = subprocess.run(
        ["node", str(script)], input=json.dumps(cases), capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout) == [
        "the [-quick-]{+slow+} fox",
        "# {+New+} Title\n\npara\n\n{+extra+}\n",
        "- a\n- b\n- {+c+}\n",
        "| a | b |\n|---|---|\n| 1 | 2 |\n| {+3 | 4 +}|\n",
        "same",
        "{+new+}",
        "[-gone-]",
    ]


def test_glossary_is_parsed_from_the_labelled_issue(running: Any) -> None:
    base = running.url
    seed(base)
    assert call(base, "GET", "/demo/board?format=json")[1]["glossary"] is None
    assert call(base, "POST", "/demo/labels", {"name": "glossary", "color": "ededed"})[0] == 201
    body = (
        "Intro line, not an entry.\n\n## Build\n\n"
        "**Crate.** Rust's unit of compilation. (#1)\n\n"
        "**.bzl file.** A Starlark source file that defines rules. (#1)\n\n"
        "**Perfetto.** The open-source trace viewer.\n"
    )
    status, issue, _ = call(
        base, "POST", "/demo/issues", {"title": "Glossary", "body": body, "labels": ["glossary"]}
    )
    assert status == 201
    status, data, _ = call(base, "GET", "/demo/board?format=json")
    assert status == 200 and data["glossary"] == {
        "source": issue["number"],
        "entries": [
            {"term": "Crate", "definition": "Rust's unit of compilation.", "issue": 1},
            {"term": ".bzl file", "definition": "A Starlark source file that defines rules.", "issue": 1},
            {"term": "Perfetto", "definition": "The open-source trace viewer.", "issue": None},
        ],
    }
    # Both pages carry the data and the pass that applies it.
    status, page, _ = call(base, "GET", "/demo/issues/1", accept="text/html")
    assert status == 200 and '"glossary": {"source": ' in page and "applyGlossary(root, issue.number)" in page
    status, page, _ = call(base, "GET", "/demo/board", accept="text/html")
    assert status == 200 and "abbr.gloss" in page
    # Closing the glossary issue withdraws it.
    assert call(base, "PATCH", f"/demo/issues/{issue['number']}", {"state": "closed"})[0] == 200
    assert call(base, "GET", "/demo/board?format=json")[1]["glossary"] is None
