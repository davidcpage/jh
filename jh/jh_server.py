"""jh-server: single-threaded HTTP front end over the jh event log.

REST subset with GitHub's paths and gh's field names:

    GET  /repos                         POST /repos
    GET  /:repo/issues                  POST /:repo/issues
    GET  /:repo/issues/:n               PATCH /:repo/issues/:n    DELETE /:repo/issues/:n
    GET  /:repo/issues/:n/comments      POST /:repo/issues/:n/comments
    PATCH /:repo/issues/comments/:id    DELETE /:repo/issues/comments/:id
    GET  /:repo/labels                  POST /:repo/labels
    GET  /:repo/labels/:name            PATCH /:repo/labels/:name DELETE /:repo/labels/:name
    GET  /:repo/milestones              POST /:repo/milestones
    GET  /:repo/milestones/:n           PATCH /:repo/milestones/:n
    GET  /:repo/events?since=SEQ
    GET  /:repo/board                   (HTML; `?format=json` for the board data)
    GET  /:repo/docs[/path]             (docs viewer over `--docs REPO=DIR`; see docs.py)

Errors are `{"message": ...}` with a 4xx status. The actor comes from the
`X-JH-Actor` request header. A browser hitting `/:repo/issues/:n` is
redirected to the board scrolled to that issue.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from jh import board, docs
from jh.jh_lib import DEFAULT_HOST, DEFAULT_PORT, JhError, Store, default_home

Handler = Callable[
    ["JhRequestHandler", dict[str, str], dict[str, Any], dict[str, Any]], Any
]

_ROUTES: list[tuple[str, re.Pattern[str], str]] = []


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    """Register a handler for `method` and a path regex with named groups."""

    def wrap(fn: Handler) -> Handler:
        _ROUTES.append((method, re.compile("^" + pattern + "$"), fn.__name__))
        return fn

    return wrap


class JhRequestHandler(BaseHTTPRequestHandler):
    """Dispatches one request to a route handler on the shared store."""

    server: "JhServer"
    protocol_version = "HTTP/1.1"
    # One request per connection: the server is single-threaded, so a browser
    # holding an idle keep-alive socket must not block the next client.
    timeout = 10

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102 - quiet unless verbose
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- plumbing ----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = unquote(parts.path.rstrip("/") or "/")
        query = {
            k: v[-1] if len(v) == 1 else v
            for k, v in parse_qs(parts.query, keep_blank_values=True).items()
        }
        body: dict[str, Any] = {}
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            if raw:
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise JhError(400, "request body must be a JSON object")
            for route_method, pattern, name in _ROUTES:
                match = pattern.match(path)
                if match and route_method == method:
                    result = getattr(self, name)(match.groupdict(), query, body)
                    if result is not None:
                        self._send_json(200, result)
                    return
            if any(p.match(path) for _, p, _ in _ROUTES):
                raise JhError(405, f"{method} not allowed on {path}")
            raise JhError(404, f"no route for {path}")
        except JhError as err:
            self._send_json(err.status, {"message": err.message})
        except json.JSONDecodeError as err:
            self._send_json(400, {"message": f"invalid JSON body: {err}"})
        except Exception as err:  # noqa: BLE001 - report, keep serving
            self._send_json(
                500, {"message": f"internal error: {type(err).__name__}: {err}"}
            )

    def do_GET(self) -> None:  # noqa: N802
        """Dispatch a GET request."""
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        """Dispatch a POST request."""
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        """Dispatch a PATCH request."""
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        """Dispatch a DELETE request."""
        self._dispatch("DELETE")

    def _send(
        self,
        status: int,
        content_type: str,
        payload: bytes,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, obj: Any) -> None:
        self._send(
            status,
            "application/json; charset=utf-8",
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        )

    def _send_html(self, text: str) -> None:
        self._send(200, "text/html; charset=utf-8", text.encode("utf-8"))

    def _wants_html(self) -> bool:
        accept = self.headers.get("Accept", "")
        return "text/html" in accept and "application/json" not in accept.split(";")[0]

    @property
    def store(self) -> Store:
        """The shared store."""
        return self.server.store

    @property
    def actor(self) -> str:
        """Actor from the `X-JH-Actor` header, else `anonymous`."""
        return self.headers.get("X-JH-Actor") or "anonymous"

    # -- repos -------------------------------------------------------------

    @route("GET", "/")
    def index(self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]) -> Any:
        """Landing page linking to each repo's board, or the repo list as JSON."""
        if not self._wants_html():
            return self.store.repo_list()
        items = "".join(
            f'<li><a href="/{html.escape(r["name"])}/board">{html.escape(r["name"])}</a> '
            f"({r['issues']} open issues)</li>"
            for r in self.store.repo_list()
        )
        self._send_html(
            "<!doctype html><meta charset=utf-8><title>jh</title>"
            "<body style='font-family:system-ui;padding:16px'><h1>jh</h1>"
            f"<ul>{items or '<li>no repos yet: <code>jh repo create NAME</code></li>'}</ul></body>"
        )
        return None

    @route("GET", "/repos")
    def repos_list(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """List repos."""
        return self.store.repo_list()

    @route("POST", "/repos")
    def repos_create(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Create a repo; body `{"name": ...}`."""
        return self.store.repo_create(b.get("name", ""), self.actor)

    # -- issues ------------------------------------------------------------

    @route("GET", r"/(?P<repo>[^/]+)/issues")
    def issues_list(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """List issues; query keys mirror the command line."""
        params = dict(q)
        if "labels" in params and isinstance(params["labels"], str):
            params["labels"] = [s for s in params["labels"].split(",") if s]
        return self.store.issue_list(m["repo"], params)

    @route("POST", r"/(?P<repo>[^/]+)/issues")
    def issues_create(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Create an issue."""
        result = self.store.issue_create(m["repo"], self.actor, b)
        self._send_json(201, result)
        return None

    @route("GET", r"/(?P<repo>[^/]+)/issues/(?P<n>\d+)")
    def issue_get(self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]) -> Any:
        """Read an issue; browsers are redirected to the board deep link."""
        issue = self.store.issue_get(m["repo"], int(m["n"]))
        if self._wants_html():
            self._send(
                302,
                "text/plain",
                b"",
                {"Location": f"/{m['repo']}/board#issue-{m['n']}"},
            )
            return None
        return issue

    @route("PATCH", r"/(?P<repo>[^/]+)/issues/(?P<n>\d+)")
    def issue_edit(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Edit an issue, including state, labels, milestone and blockedBy."""
        return self.store.issue_edit(m["repo"], self.actor, int(m["n"]), b)

    @route("DELETE", r"/(?P<repo>[^/]+)/issues/(?P<n>\d+)")
    def issue_delete(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Delete an issue."""
        return self.store.issue_delete(m["repo"], self.actor, int(m["n"]))

    # -- comments ----------------------------------------------------------

    @route("GET", r"/(?P<repo>[^/]+)/issues/(?P<n>\d+)/comments")
    def comments_list(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """List an issue's comments."""
        return self.store.comment_list(m["repo"], int(m["n"]))

    @route("POST", r"/(?P<repo>[^/]+)/issues/(?P<n>\d+)/comments")
    def comments_create(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Add a comment; body `{"body": ...}`."""
        result = self.store.comment_create(
            m["repo"], self.actor, int(m["n"]), b.get("body", "")
        )
        self._send_json(201, result)
        return None

    @route("PATCH", r"/(?P<repo>[^/]+)/issues/comments/(?P<id>\d+)")
    def comment_edit(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Edit the caller's own comment."""
        return self.store.comment_edit(
            m["repo"], self.actor, int(m["id"]), b.get("body", "")
        )

    @route("DELETE", r"/(?P<repo>[^/]+)/issues/comments/(?P<id>\d+)")
    def comment_delete(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Delete the caller's own comment."""
        return self.store.comment_delete(m["repo"], self.actor, int(m["id"]))

    # -- labels ------------------------------------------------------------

    @route("GET", r"/(?P<repo>[^/]+)/labels")
    def labels_list(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """List labels (`search`, `limit`)."""
        return self.store.label_list(m["repo"], q)

    @route("POST", r"/(?P<repo>[^/]+)/labels")
    def labels_create(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Create a label (`force` updates an existing one)."""
        result = self.store.label_create(m["repo"], self.actor, b)
        self._send_json(201, result)
        return None

    @route("GET", r"/(?P<repo>[^/]+)/labels/(?P<name>[^/]+)")
    def label_get(self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]) -> Any:
        """Read one label."""
        state = self.store.repo(m["repo"])
        return self.store.label_json(state, state.label(m["name"]))

    @route("PATCH", r"/(?P<repo>[^/]+)/labels/(?P<name>[^/]+)")
    def label_edit(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Edit a label (`name`, `color`, `description`)."""
        return self.store.label_edit(m["repo"], self.actor, m["name"], b)

    @route("DELETE", r"/(?P<repo>[^/]+)/labels/(?P<name>[^/]+)")
    def label_delete(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Delete a label."""
        return self.store.label_delete(m["repo"], self.actor, m["name"])

    # -- milestones --------------------------------------------------------

    @route("GET", r"/(?P<repo>[^/]+)/milestones")
    def milestones_list(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """List milestones (`state`)."""
        return self.store.milestone_list(m["repo"], q)

    @route("POST", r"/(?P<repo>[^/]+)/milestones")
    def milestones_create(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Create a milestone."""
        result = self.store.milestone_create(m["repo"], self.actor, b)
        self._send_json(201, result)
        return None

    @route("GET", r"/(?P<repo>[^/]+)/milestones/(?P<ref>[^/]+)")
    def milestone_get(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Read a milestone by number or title."""
        return self.store.milestone_get(m["repo"], m["ref"])

    @route("PATCH", r"/(?P<repo>[^/]+)/milestones/(?P<ref>[^/]+)")
    def milestone_edit(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """Edit or close a milestone."""
        return self.store.milestone_edit(m["repo"], self.actor, m["ref"], b)

    # -- events and board --------------------------------------------------

    @route("GET", r"/(?P<repo>[^/]+)/events")
    def events(self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]) -> Any:
        """Raw log from a sequence number."""
        since = q.get("since") or "0"
        if not str(since).isdigit():
            raise JhError(400, "since must be an integer")
        return self.store.events_since(m["repo"], int(since))

    @route("GET", r"/(?P<repo>[^/]+)/board")
    def board_page(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """The live board, or its data as JSON with `?format=json`."""
        docs_root = self.server.docs.get(m["repo"])
        if q.get("format") == "json":
            return board.board_data(self.store, m["repo"], docs_root)
        self.store.repo(m["repo"])
        self._send_html(
            board.render_board(self.store, m["repo"], live=True, docs_root=docs_root)
        )
        return None

    # -- docs --------------------------------------------------------------

    def _docs_root(self, repo: str) -> Path:
        root = self.server.docs.get(repo)
        if root is None:
            raise JhError(
                404,
                f"no docs configured for {repo}: start jh-server with --docs {repo}=DIR",
            )
        return root

    @route("GET", r"/(?P<repo>[^/]+)/docs")
    def docs_index(
        self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]
    ) -> Any:
        """The docs root: redirect to the index document, or the tree as JSON."""
        root = self._docs_root(m["repo"])
        if q.get("format") == "json":
            return {"repo": m["repo"], "root": str(root), "files": docs.tree(root)}
        first = docs.index_path(root)
        if first is None:
            raise JhError(404, f"no markdown files under {root}")
        self._send(302, "text/plain", b"", {"Location": f"/{m['repo']}/docs/{first}"})
        return None

    @route("GET", r"/(?P<repo>[^/]+)/docs/(?P<path>.+)")
    def docs_file(self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]) -> Any:
        """A document (viewer page, `?format=raw` for the markdown) or a static file."""
        root = self._docs_root(m["repo"])
        rel = m["path"]
        if rel.endswith("/"):
            rel = rel.rstrip("/")
        if (root / rel).is_dir():
            first = docs.index_path(root / rel)
            if first is None:
                raise JhError(404, f"no markdown files under {rel}")
            self._send(
                302, "text/plain", b"", {"Location": f"/{m['repo']}/docs/{rel}/{first}"}
            )
            return None
        docs.serve(self, m["repo"], root, rel, q)
        return None

    @route("GET", r"/(?P<repo>[^/]+)")
    def repo_page(self, m: dict[str, str], q: dict[str, Any], b: dict[str, Any]) -> Any:
        """A repo: JSON summary, or redirect to the board for browsers."""
        self.store.repo(m["repo"])
        if self._wants_html():
            self._send(302, "text/plain", b"", {"Location": f"/{m['repo']}/board"})
            return None
        return next(r for r in self.store.repo_list() if r["name"] == m["repo"])


class JhServer(HTTPServer):
    """`HTTPServer` carrying the store. Single-threaded by design.

    Attributes:
        store: The shared store.
        verbose: Whether to log each request to stderr.
        docs: Repo name to docs root directory, from `--docs REPO=DIR`.
    """

    allow_reuse_address = True
    request_queue_size = 64

    def __init__(
        self,
        host: str,
        port: int,
        home: Path | None,
        verbose: bool = False,
        docs: dict[str, Path] | None = None,
    ) -> None:
        """Bind to `host:port` (port 0 picks a free one) over the log at `home`."""
        super().__init__((host, port), JhRequestHandler)
        actual_port = self.server_address[1]
        self.store = Store(home, base_url=f"http://{host}:{actual_port}")
        self.verbose = verbose
        self.docs = {k: Path(v).resolve() for k, v in (docs or {}).items()}

    @property
    def url(self) -> str:
        """The server's base URL."""
        return self.store.base_url


def main(argv: list[str] | None = None) -> int:
    """Run the server until interrupted."""
    parser = argparse.ArgumentParser(
        prog="jh-server", description="Local issue tracker server for jh."
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="log base directory (default JH_HOME or ~/.local/share/jh)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log requests to stderr"
    )
    parser.add_argument(
        "--docs",
        action="append",
        default=[],
        metavar="REPO=DIR",
        help="serve the markdown under DIR at /REPO/docs (repeatable)",
    )
    args = parser.parse_args(argv)
    home = args.home or default_home()
    server = JhServer(
        args.host,
        args.port,
        home,
        verbose=args.verbose,
        docs=docs.parse_docs_args(args.docs),
    )
    served = "".join(
        f"; docs /{repo}/docs -> {root}" for repo, root in sorted(server.docs.items())
    )
    print(
        f"jh-server listening on {server.url}; log dir {home}; repos: {', '.join(sorted(server.store.repos)) or '(none)'}{served}",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
