"""Server tests: REST subset, concurrency, replay across restart."""

from __future__ import annotations

import json
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


def test_board_and_browser_redirect(running: Any) -> None:
    base = running.url
    seed(base)
    status, page, headers = call(base, "GET", "/demo/board", accept="text/html")
    assert (
        status == 200
        and "<title>demo board</title>" in page
        and '"repo": "demo"' in page
    )
    status, data, _ = call(base, "GET", "/demo/board?format=json")
    assert (
        status == 200
        and data["repo"] == "demo"
        and len(data["issues"]) == 2
        and data["seq"] > 0
    )
    status, _, headers = call(
        base, "GET", "/demo/issues/2", accept="text/html,application/xhtml+xml"
    )
    assert status == 302 and headers["Location"] == "/demo/board#issue-2"
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
