"""jh: `gh issue` grammar over a local jh-server.

The parser mirrors gh 2.88.1 flag for flag. Flags gh has that mean nothing
locally are still parsed, and fail with a message naming the supported set.
Human output follows gh's layout (a table on a terminal, tab-separated when
piped); `--json` and `--jq` behave as in gh.

`--jq` runs the `jq` binary when one is on PATH. Otherwise a small built-in
subset is used: dot paths (`.a`, `.a.b`, `.[]`, `.[0]`, `.a[].b`), pipes
between them, and `length` / `keys`. Anything else needs real `jq`.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, NoReturn
from urllib import error, parse, request

from jh import board
from jh.jh_lib import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ISSUE_FIELDS,
    LABEL_FIELDS,
    MILESTONE_FIELDS,
    STATE_REASONS,
    JhError,
    parse_fields,
    select_fields,
)

# Flags that are jh extensions (GitHub concepts gh's CLI lacks), per command.
EXTENSION_FLAGS: dict[str, set[str]] = {
    "issue create": {"--blocked-by"},
    "issue list": {"--ready", "--blocked-by", "--blocking"},
    "issue edit": {"--add-blocked-by", "--remove-blocked-by"},
}

# gh flags that are accepted by the parser but rejected with a message.
DROPPED_FLAGS: dict[str, list[tuple[str, ...]]] = {
    "issue create": [
        ("-p", "--project"),
        ("-T", "--template"),
        ("--recover",),
        ("-e", "--editor"),
        ("-w", "--web"),
    ],
    "issue list": [("--app",), ("--mention",), ("-t", "--template"), ("-w", "--web")],
    "issue view": [("-t", "--template"), ("-w", "--web")],
    "issue edit": [("--add-project",), ("--remove-project",)],
    "issue comment": [("--create-if-none",), ("-e", "--editor"), ("-w", "--web")],
    "label list": [("--order",), ("--sort",), ("-t", "--template"), ("-w", "--web")],
}

# Whole gh subcommands with no local meaning.
DROPPED_ISSUE_COMMANDS = (
    "status",
    "develop",
    "lock",
    "unlock",
    "pin",
    "unpin",
    "transfer",
)

_NUMBER_RE = re.compile(r"(?:.*/issues/)?#?(\d+)$")


class CliError(Exception):
    """A user-facing command-line error (exit status 1)."""


class Parser(argparse.ArgumentParser):
    """argparse parser that raises `CliError` instead of exiting."""

    def error(self, message: str) -> NoReturn:  # noqa: D102
        raise CliError(f"{message}\n\n{self.format_usage().rstrip()}")


class _Dropped(argparse.Action):
    """Records the use of a gh flag that jh does not support."""

    def __init__(self, option_strings: list[str], dest: str, **kwargs: Any) -> None:
        kwargs.setdefault("nargs", "?")
        kwargs.setdefault("help", argparse.SUPPRESS)
        super().__init__(option_strings, dest, **kwargs)

    def __call__(
        self, parser: Any, namespace: Any, values: Any, option_string: str | None = None
    ) -> None:
        used = getattr(namespace, "_dropped", None) or []
        used.append(option_string or self.option_strings[-1])
        namespace._dropped = used


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _common(sub: Any, name: str, **kwargs: Any) -> Parser:
    parser = sub.add_parser(name, **kwargs)
    parser.add_argument(
        "-R",
        "--repo",
        metavar="REPO",
        help="Select another repository (an issue space on the server)",
    )
    return parser


def _json_flags(parser: Parser, fields: list[str]) -> None:
    parser.add_argument(
        "--json",
        nargs="?",
        const="",
        metavar="fields",
        help="Output JSON with the specified fields",
    )
    parser.add_argument(
        "-q",
        "--jq",
        metavar="expression",
        help="Filter JSON output using a jq expression",
    )
    parser.set_defaults(_fields=fields)


def _body_flags(parser: Parser) -> None:
    parser.add_argument("-b", "--body", help="Supply a body")
    parser.add_argument(
        "-F",
        "--body-file",
        metavar="file",
        help='Read body text from file (use "-" to read from standard input)',
    )


def _dropped(parser: Parser, command: str) -> None:
    for opts in DROPPED_FLAGS.get(command, []):
        parser.add_argument(
            *opts,
            action=_Dropped,
            dest="_dropped_" + opts[-1].lstrip("-").replace("-", "_"),
        )


def build_parser() -> Parser:
    """Build the full `jh` parser."""
    root = Parser(prog="jh", description="gh issue, backed by a local jh-server.")
    top = root.add_subparsers(dest="command", metavar="<command>")
    top.required = True

    # ---- issue ----
    issue = top.add_parser("issue", help="Manage issues")
    isub = issue.add_subparsers(dest="subcommand", metavar="<subcommand>")
    isub.required = True

    p = _common(isub, "create", aliases=["new"], help="Create a new issue")
    p.add_argument(
        "-a",
        "--assignee",
        action="append",
        metavar="login",
        help='Assign people by their login. Use "@me" to self-assign.',
    )
    _body_flags(p)
    p.add_argument(
        "-l", "--label", action="append", metavar="name", help="Add labels by name"
    )
    p.add_argument(
        "-m", "--milestone", metavar="name", help="Add the issue to a milestone by name"
    )
    p.add_argument("-t", "--title", help="Supply a title")
    p.add_argument(
        "--blocked-by",
        action="append",
        metavar="numbers",
        help="Issues that block this one (jh extension)",
    )
    _dropped(p, "issue create")

    p = _common(isub, "list", aliases=["ls"], help="List issues in a repository")
    p.add_argument("-a", "--assignee", help="Filter by assignee")
    p.add_argument("-A", "--author", help="Filter by author")
    _json_flags(p, ISSUE_FIELDS)
    p.add_argument(
        "-l", "--label", action="append", metavar="strings", help="Filter by label"
    )
    p.add_argument(
        "-L",
        "--limit",
        type=int,
        default=30,
        help="Maximum number of issues to fetch (default 30)",
    )
    p.add_argument("-m", "--milestone", help="Filter by milestone number or title")
    p.add_argument("-S", "--search", metavar="query", help="Search issues with query")
    p.add_argument(
        "-s",
        "--state",
        default="open",
        help='Filter by state: {open|closed|all} (default "open")',
    )
    p.add_argument(
        "--ready",
        action="store_true",
        help="Open, not blocked by any open issue, and not labelled draft (jh extension)",
    )
    p.add_argument(
        "--blocked-by", metavar="N", help="Issues blocked by N (jh extension)"
    )
    p.add_argument("--blocking", metavar="N", help="Issues blocking N (jh extension)")
    _dropped(p, "issue list")

    p = _common(isub, "view", help="View an issue")
    p.add_argument("number", metavar="{<number> | <url>}")
    p.add_argument("-c", "--comments", action="store_true", help="View issue comments")
    _json_flags(p, ISSUE_FIELDS)
    _dropped(p, "issue view")

    p = _common(isub, "edit", help="Edit issues")
    p.add_argument("numbers", nargs="+", metavar="{<numbers> | <urls>}")
    p.add_argument(
        "--add-assignee",
        action="append",
        metavar="login",
        help="Add assigned users by their login",
    )
    p.add_argument(
        "--add-label", action="append", metavar="name", help="Add labels by name"
    )
    _body_flags(p)
    p.add_argument(
        "-m",
        "--milestone",
        metavar="name",
        help="Edit the milestone the issue belongs to by name",
    )
    p.add_argument(
        "--remove-assignee",
        action="append",
        metavar="login",
        help="Remove assigned users by their login",
    )
    p.add_argument(
        "--remove-label", action="append", metavar="name", help="Remove labels by name"
    )
    p.add_argument(
        "--remove-milestone",
        action="store_true",
        help="Remove the milestone association from the issue",
    )
    p.add_argument("-t", "--title", help="Set the new title.")
    p.add_argument(
        "--add-blocked-by",
        action="append",
        metavar="numbers",
        help="Add blocking issues (jh extension)",
    )
    p.add_argument(
        "--remove-blocked-by",
        action="append",
        metavar="numbers",
        help="Remove blocking issues (jh extension)",
    )
    _dropped(p, "issue edit")

    p = _common(isub, "close", help="Close issue")
    p.add_argument("number", metavar="{<number> | <url>}")
    p.add_argument("-c", "--comment", help="Leave a closing comment")
    p.add_argument(
        "--duplicate-of",
        metavar="string",
        help="Mark as duplicate of another issue by number or URL",
    )
    p.add_argument(
        "-r", "--reason", help="Reason for closing: {completed|not planned|duplicate}"
    )

    p = _common(isub, "reopen", help="Reopen issue")
    p.add_argument("number", metavar="{<number> | <url>}")
    p.add_argument("-c", "--comment", help="Add a reopening comment")

    p = _common(isub, "comment", help="Add a comment to an issue")
    p.add_argument("number", metavar="{<number> | <url>}")
    _body_flags(p)
    p.add_argument(
        "--delete-last",
        action="store_true",
        help="Delete the last comment of the current user",
    )
    p.add_argument(
        "--edit-last",
        action="store_true",
        help="Edit the last comment of the current user",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the delete confirmation prompt when --delete-last is provided",
    )
    _dropped(p, "issue comment")

    p = _common(isub, "delete", help="Delete issue")
    p.add_argument("number", metavar="{<number> | <url>}")
    p.add_argument(
        "--yes", action="store_true", help="Confirm deletion without prompting"
    )

    for name in DROPPED_ISSUE_COMMANDS:
        p = _common(isub, name, help=argparse.SUPPRESS)
        p.add_argument("rest", nargs=argparse.REMAINDER)
        p.set_defaults(_dropped_command=name)

    # ---- label ----
    label = top.add_parser("label", help="Manage labels")
    lsub = label.add_subparsers(dest="subcommand", metavar="<subcommand>")
    lsub.required = True

    p = _common(lsub, "list", aliases=["ls"], help="List labels in a repository")
    _json_flags(p, LABEL_FIELDS)
    p.add_argument(
        "-L",
        "--limit",
        type=int,
        default=30,
        help="Maximum number of labels to fetch (default 30)",
    )
    p.add_argument("-S", "--search", help="Search label names and descriptions")
    _dropped(p, "label list")

    p = _common(lsub, "create", help="Create a new label")
    p.add_argument("name")
    p.add_argument("-c", "--color", help="Color of the label")
    p.add_argument("-d", "--description", help="Description of the label")
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Update the label color and description if label already exists",
    )

    p = _common(lsub, "edit", help="Edit a label")
    p.add_argument("name")
    p.add_argument("-c", "--color", help="Color of the label")
    p.add_argument("-d", "--description", help="Description of the label")
    p.add_argument("-n", "--name", dest="new_name", help="New name of the label")

    p = _common(lsub, "delete", help="Delete a label from a repository")
    p.add_argument("name")
    p.add_argument(
        "--yes", action="store_true", help="Confirm deletion without prompting"
    )

    # ---- milestone (jh extension; gh has no milestone command) ----
    milestone = top.add_parser("milestone", help="Manage milestones (jh extension)")
    msub = milestone.add_subparsers(dest="subcommand", metavar="<subcommand>")
    msub.required = True

    p = _common(msub, "list", aliases=["ls"], help="List milestones")
    p.add_argument(
        "-s",
        "--state",
        default="open",
        help='Filter by state: {open|closed|all} (default "open")',
    )
    _json_flags(p, MILESTONE_FIELDS)

    p = _common(msub, "create", help="Create a milestone")
    p.add_argument("-t", "--title", required=True, help="Milestone title")
    p.add_argument("-d", "--description", help="Milestone description")
    p.add_argument("--due-on", metavar="YYYY-MM-DD", help="Due date")
    _json_flags(p, MILESTONE_FIELDS)

    p = _common(msub, "edit", help="Edit a milestone")
    p.add_argument("milestone", metavar="{<number> | <title>}")
    p.add_argument("-t", "--title", help="New title")
    p.add_argument("-d", "--description", help="New description")
    p.add_argument("--due-on", metavar="YYYY-MM-DD", help='New due date ("" to clear)')
    p.add_argument("--reopen", action="store_true", help="Reopen a closed milestone")
    _json_flags(p, MILESTONE_FIELDS)

    p = _common(msub, "close", help="Close a milestone")
    p.add_argument("milestone", metavar="{<number> | <title>}")

    # ---- repo ----
    repo = top.add_parser("repo", help="Manage issue spaces on the server")
    rsub = repo.add_subparsers(dest="subcommand", metavar="<subcommand>")
    rsub.required = True
    rsub.add_parser("list", aliases=["ls"], help="List repos").add_argument(
        "--json", nargs="?", const="", metavar="fields"
    )
    rsub.add_parser("create", help="Create a repo").add_argument("name")

    # ---- board ----
    p = _common(
        top, "board", help="Print the board URL, or write a self-contained snapshot"
    )
    p.add_argument(
        "--snapshot",
        metavar="FILE",
        help="Write the board as a self-contained HTML file",
    )

    return root


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


class Client:
    """Thin JSON client for jh-server.

    Attributes:
        base: Server base URL.
        actor: Value sent as `X-JH-Actor`.
    """

    def __init__(self, base: str, actor: str) -> None:
        self.base = base.rstrip("/")
        self.actor = actor

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        """Send one request and return the decoded JSON reply.

        Raises:
            CliError: When the server is unreachable or answers with an error.
        """
        url = self.base + path
        if query:
            clean = {k: v for k, v in query.items() if v not in (None, "", False, [])}
            if clean:
                url += "?" + parse.urlencode(clean, doseq=True)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("X-JH-Actor", self.actor)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except error.HTTPError as err:
            raw = err.read()
            try:
                message = json.loads(raw).get("message", raw.decode("utf-8", "replace"))
            except (json.JSONDecodeError, AttributeError):
                message = raw.decode("utf-8", "replace") or err.reason
            raise CliError(str(message)) from None
        except error.URLError as err:
            raise CliError(
                f"could not reach jh-server at {self.base}: {err.reason}\n"
                "Is jh-server running? For a server on another host, tunnel with: ssh -L 7411:127.0.0.1:7411 HOST"
            ) from None
        return json.loads(raw) if raw else None


def server_url() -> str:
    """Return the server URL: `JH_SERVER`, else `http://127.0.0.1:7411`."""
    return os.environ.get("JH_SERVER") or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def actor_name() -> str:
    """Return the actor: `JH_ACTOR`, else the login name."""
    if os.environ.get("JH_ACTOR"):
        return os.environ["JH_ACTOR"]
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "anonymous"


def resolve_repo(client: Client, flag: str | None) -> str:
    """Resolve the repo from `-R`, then `JH_REPO`, else fail listing repos."""
    if flag:
        return flag
    if os.environ.get("JH_REPO"):
        return os.environ["JH_REPO"]
    known = (
        ", ".join(r["name"] for r in client.call("GET", "/repos"))
        or "(none; create one with `jh repo create NAME`)"
    )
    raise CliError(
        f"no repository specified: use `-R REPO` or set JH_REPO. Known repos: {known}"
    )


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def is_tty() -> bool:
    """Whether stdout is a terminal (gh switches layout on this)."""
    return sys.stdout.isatty()


def relative_time(ts: str | None, now: datetime | None = None) -> str:
    """Return gh's `about N units ago` wording for an ISO timestamp."""
    if not ts:
        return ""
    then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    seconds = max(0, (now - then).total_seconds())
    if seconds < 60:
        return "less than a minute ago"
    units = [
        (60, "minute", 60),
        (3600, "hour", 24),
        (86400, "day", 30),
        (86400 * 30, "month", 12),
        (86400 * 365, "year", None),
    ]
    for size, name, cap in units:
        amount = int(seconds // size)
        if cap is None or amount < cap:
            return f"about {amount} {name}{'s' if amount != 1 else ''} ago"
    return ""


def fuzzy_abbr(ts: str | None, now: datetime | None = None) -> str:
    """Return gh's abbreviated age (`4h`, `2d`, `Jan 5, 2026`)."""
    if not ts:
        return ""
    then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    seconds = max(0, (now - then).total_seconds())
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)}d"
    return then.strftime("%b %-d, %Y")


def truncate(text: str, width: int) -> str:
    """Truncate to `width` columns with gh's `...` suffix."""
    text = text.replace("\n", " ")
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def print_table(
    headers: list[str], rows: list[list[str]], flexible: set[int] | None = None
) -> None:
    """Print a gh-style table: padded columns on a tty, tabs when piped."""
    if not is_tty():
        for row in rows:
            print("\t".join(row))
        return
    width = shutil.get_terminal_size((120, 24)).columns
    cols = len(headers)
    natural = [max([len(headers[i])] + [len(r[i]) for r in rows]) for i in range(cols)]
    flexible = flexible if flexible is not None else set()
    fixed = sum(natural[i] for i in range(cols) if i not in flexible) + 2 * (cols - 1)
    spare = max(width - fixed, 10 * max(1, len(flexible)))
    widths = list(natural)
    if flexible:
        total_flex = sum(natural[i] for i in flexible) or 1
        for i in flexible:
            widths[i] = max(4, min(natural[i], spare * natural[i] // total_flex))
    print(
        "  ".join(
            truncate(h, widths[i]).ljust(widths[i]) for i, h in enumerate(headers)
        ).rstrip()
    )
    for row in rows:
        print(
            "  ".join(
                truncate(c, widths[i]).ljust(widths[i]) for i, c in enumerate(row)
            ).rstrip()
        )


def emit_json(data: Any, fields: list[str], jq: str | None) -> None:
    """Select fields and print JSON, or pipe it through jq."""
    if isinstance(data, list):
        selected: Any = [select_fields(d, fields) for d in data]
    else:
        selected = select_fields(data, fields)
    if jq:
        print(run_jq(jq, selected), end="")
        return
    if is_tty():
        print(json.dumps(selected, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(selected, ensure_ascii=False))


def run_jq(expr: str, data: Any) -> str:
    """Filter `data` with jq: the `jq` binary if present, else the built-in subset."""
    binary = shutil.which("jq")
    if binary:
        proc = subprocess.run(
            [binary, "-r", expr], input=json.dumps(data), capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise CliError(
                proc.stderr.strip() or f"jq failed with status {proc.returncode}"
            )
        return proc.stdout
    results = simple_jq(expr, data)
    out = []
    for item in results:
        out.append(
            item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        )
    return "".join(f"{line}\n" for line in out)


def simple_jq(expr: str, data: Any) -> list[Any]:
    """Evaluate the built-in jq subset (paths, `[]`, `[N]`, pipes, `length`, `keys`).

    Raises:
        CliError: For anything outside the subset.
    """
    values = [data]
    for stage in [s.strip() for s in expr.split("|")]:
        if not stage:
            raise CliError(
                f"unsupported jq expression {expr!r}: install jq for full support"
            )
        values = [out for value in values for out in _jq_stage(stage, value, expr)]
    return values


def _jq_stage(stage: str, value: Any, expr: str) -> list[Any]:
    if stage == "length":
        return [len(value) if value is not None else 0]
    if stage == "keys":
        return [
            sorted(value.keys()) if isinstance(value, dict) else list(range(len(value)))
        ]
    if stage == ".":
        return [value]
    if not stage.startswith("."):
        raise CliError(
            f"unsupported jq expression {expr!r}: built-in jq only handles dot paths, install jq for more"
        )
    current = [value]
    for token in re.findall(
        r"\.[A-Za-z_][A-Za-z0-9_]*|\.\"[^\"]*\"|\[\]|\[-?\d+\]", stage
    ):
        nxt: list[Any] = []
        for item in current:
            if token == "[]":
                if isinstance(item, list):
                    nxt.extend(item)
                elif isinstance(item, dict):
                    nxt.extend(item.values())
                elif item is not None:
                    raise CliError(f"jq: cannot iterate over {type(item).__name__}")
            elif token.startswith("["):
                index = int(token[1:-1])
                if isinstance(item, list):
                    nxt.append(item[index] if -len(item) <= index < len(item) else None)
                else:
                    nxt.append(None)
            else:
                key = token[1:].strip('"')
                if isinstance(item, dict):
                    nxt.append(item.get(key))
                elif item is None:
                    nxt.append(None)
                else:
                    raise CliError(
                        f"jq: cannot index {type(item).__name__} with {key!r}"
                    )
        current = nxt
    rebuilt = "".join(
        re.findall(r"\.[A-Za-z_][A-Za-z0-9_]*|\.\"[^\"]*\"|\[\]|\[-?\d+\]", stage)
    )
    core = stage[1:] if stage.startswith(".[") else stage
    if rebuilt != core.rstrip("."):
        raise CliError(
            f"unsupported jq expression {expr!r}: built-in jq only handles dot paths, install jq for more"
        )
    return current


def read_body(args: argparse.Namespace) -> str | None:
    """Return the body from `-b` or `-F` (`-` is stdin), or None if neither."""
    if getattr(args, "body_file", None):
        if args.body_file == "-":
            return sys.stdin.read()
        with open(args.body_file, encoding="utf-8") as handle:
            return handle.read()
    return getattr(args, "body", None)


def issue_number(text: str) -> int:
    """Parse `{<number> | <url>}`."""
    match = _NUMBER_RE.match(text.strip())
    if not match:
        raise CliError(f"invalid issue format: {text!r}")
    return int(match.group(1))


def split_list(values: list[str] | None) -> list[str]:
    """Flatten repeated and comma-separated flag values."""
    out: list[str] = []
    for value in values or []:
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return out


def resolve_me(values: list[str], actor: str) -> list[str]:
    """Replace gh's `@me` with the actor."""
    return [actor if v == "@me" else v for v in values]


def info(message: str) -> None:
    """Print a status line to stderr (gh prints these on stderr too)."""
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def check_dropped(
    args: argparse.Namespace, parser_name: str, supported: list[str]
) -> None:
    used = getattr(args, "_dropped", None)
    if used:
        flags = ", ".join(sorted(set(used)))
        raise CliError(
            f"{flags} is not supported by `jh {parser_name}`; supported flags: {', '.join(supported)}"
        )


def supported_flags(root: Parser, path: list[str]) -> list[str]:
    """Return the long option strings of a subcommand, minus dropped ones."""
    parser: Any = root
    for name in path:
        parser = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        ).choices[name]
    out = []
    for action in parser._actions:
        if (
            isinstance(action, _Dropped)
            or not action.option_strings
            or action.option_strings == ["-h", "--help"]
        ):
            continue
        out.append(action.option_strings[-1])
    return out


def json_requested(args: argparse.Namespace) -> list[str] | None:
    """Return the field list for `--json`, or None; validates `--jq` pairing."""
    spec = getattr(args, "json", None)
    if spec is None:
        if getattr(args, "jq", None):
            raise CliError("cannot use `--jq` without specifying `--json`")
        return None
    if spec == "":
        listing = "\n".join(f"  {f}" for f in sorted(args._fields))
        raise CliError(
            f"Specify one or more comma-separated fields for `--json`:\n{listing}"
        )
    try:
        return parse_fields(spec, args._fields)
    except JhError as err:
        raise CliError(err.message) from None


def cmd_issue_create(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    check_dropped(args, "issue create", supported_flags(root, ["issue", "create"]))
    body = read_body(args)
    if not args.title or body is None:
        raise CliError(
            "must provide `--title` and `--body` when not running interactively"
        )
    payload = {
        "title": args.title,
        "body": body,
        "labels": split_list(args.label),
        "assignees": resolve_me(split_list(args.assignee), client.actor),
        "milestone": args.milestone,
        "blockedBy": [issue_number(n) for n in split_list(args.blocked_by)],
    }
    if is_tty():
        info(f"\nCreating issue in {repo}\n")
    issue = client.call("POST", f"/{repo}/issues", payload)
    print(issue["url"])
    return 0


def cmd_issue_list(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    check_dropped(args, "issue list", supported_flags(root, ["issue", "list"]))
    fields = json_requested(args)
    if args.state not in ("open", "closed", "all"):
        raise CliError(
            f'invalid argument "{args.state}" for "-s, --state" flag: valid values are {{open|closed|all}}'
        )
    query = {
        "state": args.state,
        "labels": ",".join(split_list(args.label)),
        "milestone": args.milestone,
        "assignee": (
            resolve_me([args.assignee], client.actor)[0] if args.assignee else None
        ),
        "author": (resolve_me([args.author], client.actor)[0] if args.author else None),
        "search": args.search,
        "limit": args.limit,
        "ready": "1" if args.ready else None,
        "blocked_by": issue_number(args.blocked_by) if args.blocked_by else None,
        "blocking": issue_number(args.blocking) if args.blocking else None,
    }
    issues = client.call("GET", f"/{repo}/issues", query=query)
    if fields is not None:
        emit_json(issues, fields, args.jq)
        return 0
    if not issues:
        if is_tty():
            raise CliError(
                f"no {args.state if args.state != 'all' else ''} issues match your search in {repo}".replace(
                    "  ", " "
                )
            )
        return 0
    if is_tty():
        state_word = "" if args.state == "all" else f"{args.state} "
        total = len(
            client.call("GET", f"/{repo}/issues", query={**query, "limit": 100000})
        )
        print(f"\nShowing {len(issues)} of {total} {state_word}issues in {repo}\n")
    rows = []
    for issue in issues:
        labels = ", ".join(l["name"] for l in issue["labels"])
        if is_tty():
            rows.append(
                [
                    f"#{issue['number']}",
                    issue["title"],
                    labels,
                    relative_time(issue["updatedAt"]),
                ]
            )
        else:
            rows.append(
                [
                    str(issue["number"]),
                    issue["state"],
                    issue["title"],
                    labels,
                    issue["updatedAt"],
                ]
            )
    print_table(["ID", "TITLE", "LABELS", "UPDATED"], rows, flexible={1, 2})
    if is_tty():
        print()
    return 0


def cmd_issue_view(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    check_dropped(args, "issue view", supported_flags(root, ["issue", "view"]))
    fields = json_requested(args)
    number = issue_number(args.number)
    issue = client.call("GET", f"/{repo}/issues/{number}")
    if fields is not None:
        emit_json(issue, fields, args.jq)
        return 0
    labels = ", ".join(l["name"] for l in issue["labels"])
    assignees = ", ".join(a["login"] for a in issue["assignees"])
    milestone = issue["milestone"]["title"] if issue["milestone"] else ""
    comments = issue["comments"]
    if not is_tty():
        print(f"title:\t{issue['title']}")
        print(f"state:\t{issue['state']}")
        print(f"author:\t{issue['author']['login']}")
        print(f"labels:\t{labels}")
        print(f"comments:\t{len(comments)}")
        print(f"assignees:\t{assignees}")
        print("projects:\t")
        print(f"milestone:\t{milestone}")
        print(f"number:\t{issue['number']}")
        if issue["blockedBy"]:
            print(
                "blocked by:\t"
                + ", ".join(f"#{d['number']}" for d in issue["blockedBy"])
            )
        print("--")
        print(issue["body"])
        if args.comments:
            for comment in comments:
                print(f"author:\t{comment['author']['login']}")
                print("association:\tnone")
                print(
                    f"edited:\t{'true' if comment['includesCreatedEdit'] else 'false'}"
                )
                print("status:\tnone")
                print("--")
                print(comment["body"])
                print("--")
        return 0
    state = "Open" if issue["state"] == "OPEN" else "Closed"
    count = f"{len(comments)} comment{'s' if len(comments) != 1 else ''}"
    print(f"{issue['title']} {repo}#{issue['number']}")
    print(
        f"{state} • {issue['author']['login']} opened {relative_time(issue['createdAt'])} • {count}"
    )
    if labels:
        print(f"Labels: {labels}")
    if assignees:
        print(f"Assignees: {assignees}")
    if milestone:
        print(f"Milestone: {milestone}")
    if issue["blockedBy"]:
        print(
            "Blocked by: "
            + ", ".join(
                f"#{d['number']} ({d['state'].lower()})" for d in issue["blockedBy"]
            )
        )
    if issue["blocking"]:
        print("Blocking: " + ", ".join(f"#{d['number']}" for d in issue["blocking"]))
    print()
    print(_indent(issue["body"] or "No description provided"))
    print()
    shown = comments if args.comments else comments[-1:]
    if comments and not args.comments:
        print(
            f"\n——————— Not showing {len(comments) - 1} comments ———————\n"
            if len(comments) > 1
            else ""
        )
    for i, comment in enumerate(shown):
        newest = (
            " • Newest comment" if comment is comments[-1] and len(comments) > 1 else ""
        )
        edited = " • Edited" if comment["includesCreatedEdit"] else ""
        print(
            f"{comment['author']['login']} • {fuzzy_abbr(comment['createdAt'])}{edited}{newest}"
        )
        print()
        print(_indent(comment["body"]))
        print()
    if comments and not args.comments:
        print("Use --comments to view the full conversation")
    print()
    print(f"View this issue on jh: {issue['url']}")
    return 0


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines()) or "  "


def cmd_issue_edit(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    check_dropped(args, "issue edit", supported_flags(root, ["issue", "edit"]))
    body = read_body(args)
    payload: dict[str, Any] = {}
    if args.title is not None:
        payload["title"] = args.title
    if body is not None:
        payload["body"] = body
    if args.add_label:
        payload["addLabels"] = split_list(args.add_label)
    if args.remove_label:
        payload["removeLabels"] = split_list(args.remove_label)
    if args.milestone is not None and args.remove_milestone:
        raise CliError("cannot use `--milestone` and `--remove-milestone` together")
    if args.milestone is not None:
        payload["milestone"] = args.milestone
    if args.remove_milestone:
        payload["milestone"] = None
    if args.add_assignee:
        payload["addAssignees"] = resolve_me(
            split_list(args.add_assignee), client.actor
        )
    if args.remove_assignee:
        payload["removeAssignees"] = resolve_me(
            split_list(args.remove_assignee), client.actor
        )
    if args.add_blocked_by:
        payload["addBlockedBy"] = [
            issue_number(n) for n in split_list(args.add_blocked_by)
        ]
    if args.remove_blocked_by:
        payload["removeBlockedBy"] = [
            issue_number(n) for n in split_list(args.remove_blocked_by)
        ]
    if not payload:
        raise CliError("specify at least one field to edit")
    numbers = [issue_number(n) for n in args.numbers]
    if len(numbers) > 1 and ("title" in payload or "body" in payload):
        raise CliError("cannot use `--title` or `--body` when editing multiple issues")
    failures = 0
    for number in numbers:
        try:
            issue = client.call("PATCH", f"/{repo}/issues/{number}", payload)
            print(issue["url"])
        except CliError as err:
            failures += 1
            info(f"failed to update {client.base}/{repo}/issues/{number}: {err}")
    if failures:
        raise CliError(
            f"failed to update {failures} issue{'s' if failures != 1 else ''}"
        )
    return 0


def cmd_issue_close(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    number = issue_number(args.number)
    if args.reason is not None and args.reason not in STATE_REASONS:
        raise CliError(
            f'invalid argument "{args.reason}" for "-r, --reason" flag: valid values are {{completed|not planned|duplicate}}'
        )
    issue = client.call("GET", f"/{repo}/issues/{number}")
    if issue["state"] == "CLOSED":
        info(f"! Issue {repo}#{number} ({issue['title']}) is already closed")
        return 0
    payload: dict[str, Any] = {"state": "closed"}
    if args.reason:
        payload["stateReason"] = args.reason
    if args.comment:
        payload["comment"] = args.comment
    if args.duplicate_of:
        payload["duplicateOf"] = issue_number(args.duplicate_of)
    client.call("PATCH", f"/{repo}/issues/{number}", payload)
    info(f"✓ Closed issue {repo}#{number} ({issue['title']})")
    return 0


def cmd_issue_reopen(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    number = issue_number(args.number)
    issue = client.call("GET", f"/{repo}/issues/{number}")
    if issue["state"] == "OPEN":
        info(f"! Issue {repo}#{number} ({issue['title']}) is already open")
        return 0
    payload: dict[str, Any] = {"state": "open"}
    if args.comment:
        payload["comment"] = args.comment
    client.call("PATCH", f"/{repo}/issues/{number}", payload)
    info(f"✓ Reopened issue {repo}#{number} ({issue['title']})")
    return 0


def cmd_issue_comment(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    check_dropped(args, "issue comment", supported_flags(root, ["issue", "comment"]))
    number = issue_number(args.number)
    body = read_body(args)
    if args.edit_last and args.delete_last:
        raise CliError("specify only one of `--edit-last` or `--delete-last`")
    if args.edit_last or args.delete_last:
        mine = [
            c
            for c in client.call("GET", f"/{repo}/issues/{number}/comments")
            if c["author"]["login"] == client.actor
        ]
        if not mine:
            raise CliError(f"no comments found for current user on issue #{number}")
        last = mine[-1]
        if args.delete_last:
            if not args.yes:
                raise CliError("--yes required when not running interactively")
            client.call("DELETE", f"/{repo}/issues/comments/{last['databaseId']}")
            info(f"✓ Deleted comment on issue {repo}#{number}")
            return 0
        if body is None:
            raise CliError("must provide `--body` when not running interactively")
        comment = client.call(
            "PATCH", f"/{repo}/issues/comments/{last['databaseId']}", {"body": body}
        )
        print(comment["url"])
        return 0
    if body is None:
        raise CliError("must provide `--body` when not running interactively")
    comment = client.call("POST", f"/{repo}/issues/{number}/comments", {"body": body})
    print(comment["url"])
    return 0


def cmd_issue_delete(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    number = issue_number(args.number)
    if not args.yes:
        raise CliError("--yes required when not running interactively")
    issue = client.call("DELETE", f"/{repo}/issues/{number}")
    info(f"✓ Deleted issue {repo}#{number} ({issue['title']}).")
    return 0


def cmd_label_list(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    check_dropped(args, "label list", supported_flags(root, ["label", "list"]))
    fields = json_requested(args)
    labels = client.call(
        "GET", f"/{repo}/labels", query={"search": args.search, "limit": args.limit}
    )
    if fields is not None:
        emit_json(labels, fields, args.jq)
        return 0
    if not labels:
        if is_tty():
            raise CliError(
                f"no labels {'match your search ' if args.search else ''}in {repo}"
            )
        return 0
    if is_tty():
        total = len(
            client.call(
                "GET", f"/{repo}/labels", query={"search": args.search, "limit": 100000}
            )
        )
        print(f"\nShowing {len(labels)} of {total} labels in {repo}\n")
    rows = [[l["name"], l["description"] or "", f"#{l['color']}"] for l in labels]
    print_table(["NAME", "DESCRIPTION", "COLOR"], rows, flexible={1})
    return 0


def cmd_label_create(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    payload = {
        "name": args.name,
        "color": args.color,
        "description": args.description,
        "force": args.force,
    }
    client.call("POST", f"/{repo}/labels", payload)
    if is_tty():
        info(f'✓ Label "{args.name}" created in {repo}')
    return 0


def cmd_label_edit(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    payload = {
        "name": args.new_name,
        "color": args.color,
        "description": args.description,
    }
    if not any(v for v in payload.values()) and args.description is None:
        raise CliError(
            "specify at least one of `--color`, `--description`, or `--name`"
        )
    client.call("PATCH", f"/{repo}/labels/{parse.quote(args.name)}", payload)
    if is_tty():
        info(f'✓ Label "{args.name}" updated in {repo}')
    return 0


def cmd_label_delete(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    if not args.yes:
        raise CliError("--yes required when not running interactively")
    client.call("DELETE", f"/{repo}/labels/{parse.quote(args.name)}")
    if is_tty():
        info(f'✓ Label "{args.name}" deleted from {repo}')
    return 0


def cmd_milestone_list(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    fields = json_requested(args)
    milestones = client.call("GET", f"/{repo}/milestones", query={"state": args.state})
    if fields is not None:
        emit_json(milestones, fields, args.jq)
        return 0
    if not milestones:
        if is_tty():
            raise CliError(
                f"no {args.state if args.state != 'all' else ''} milestones in {repo}".replace(
                    "  ", " "
                )
            )
        return 0
    if is_tty():
        print(f"\nShowing {len(milestones)} milestones in {repo}\n")
    rows = [
        [
            str(m["number"]),
            m["title"],
            (m["dueOn"] or "")[:10],
            m["state"],
            m["description"] or "",
        ]
        for m in milestones
    ]
    print_table(
        ["NUMBER", "TITLE", "DUE ON", "STATE", "DESCRIPTION"], rows, flexible={1, 4}
    )
    return 0


def cmd_milestone_create(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    fields = json_requested(args)
    payload = {
        "title": args.title,
        "description": args.description,
        "dueOn": args.due_on,
    }
    milestone = client.call("POST", f"/{repo}/milestones", payload)
    if fields is not None:
        emit_json(milestone, fields, args.jq)
    else:
        print(milestone["url"])
    return 0


def cmd_milestone_edit(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    fields = json_requested(args)
    payload: dict[str, Any] = {}
    if args.title:
        payload["title"] = args.title
    if args.description is not None:
        payload["description"] = args.description
    if args.due_on is not None:
        payload["dueOn"] = args.due_on or None
    if args.reopen:
        payload["state"] = "open"
    if not payload:
        raise CliError(
            "specify at least one of `--title`, `--description`, `--due-on`, or `--reopen`"
        )
    milestone = client.call(
        "PATCH", f"/{repo}/milestones/{parse.quote(args.milestone)}", payload
    )
    if fields is not None:
        emit_json(milestone, fields, args.jq)
    else:
        print(milestone["url"])
    return 0


def cmd_milestone_close(
    client: Client, repo: str, args: argparse.Namespace, root: Parser
) -> int:
    milestone = client.call(
        "PATCH",
        f"/{repo}/milestones/{parse.quote(args.milestone)}",
        {"state": "closed"},
    )
    info(f'✓ Closed milestone {repo} "{milestone["title"]}"')
    return 0


def cmd_board(client: Client, repo: str, args: argparse.Namespace, root: Parser) -> int:
    url = f"{client.base}/{repo}/board"
    if args.snapshot:
        data = client.call("GET", f"/{repo}/board", query={"format": "json"})
        with open(args.snapshot, "w", encoding="utf-8") as handle:
            handle.write(board.render_from_data(data, live=False))
        info(f"✓ Wrote board snapshot for {repo} to {args.snapshot}")
        return 0
    print(url)
    return 0


def cmd_repo(client: Client, args: argparse.Namespace) -> int:
    if args.subcommand in ("list", "ls"):
        repos = client.call("GET", "/repos")
        if args.json is not None:
            print(json.dumps(repos, indent=2 if is_tty() else None))
            return 0
        for repo in repos:
            print(f"{repo['name']}\t{repo['issues']} open issues\t{repo['url']}")
        return 0
    repo = client.call("POST", "/repos", {"name": args.name})
    info(f"✓ Created repo {repo['name']} at {repo['url']}")
    return 0


COMMANDS = {
    ("issue", "create"): cmd_issue_create,
    ("issue", "new"): cmd_issue_create,
    ("issue", "list"): cmd_issue_list,
    ("issue", "ls"): cmd_issue_list,
    ("issue", "view"): cmd_issue_view,
    ("issue", "edit"): cmd_issue_edit,
    ("issue", "close"): cmd_issue_close,
    ("issue", "reopen"): cmd_issue_reopen,
    ("issue", "comment"): cmd_issue_comment,
    ("issue", "delete"): cmd_issue_delete,
    ("label", "list"): cmd_label_list,
    ("label", "ls"): cmd_label_list,
    ("label", "create"): cmd_label_create,
    ("label", "edit"): cmd_label_edit,
    ("label", "delete"): cmd_label_delete,
    ("milestone", "list"): cmd_milestone_list,
    ("milestone", "ls"): cmd_milestone_list,
    ("milestone", "create"): cmd_milestone_create,
    ("milestone", "edit"): cmd_milestone_edit,
    ("milestone", "close"): cmd_milestone_close,
    ("board", None): cmd_board,
}


def run(argv: list[str]) -> int:
    """Parse and execute one command; raises `CliError` on failure."""
    root = build_parser()
    args = root.parse_args(argv)
    if getattr(args, "_dropped_command", None):
        supported = "create, list, view, edit, close, reopen, comment, delete"
        raise CliError(
            f"`jh issue {args._dropped_command}` is not supported; supported subcommands: {supported}"
        )
    client = Client(server_url(), actor_name())
    if args.command == "repo":
        return cmd_repo(client, args)
    repo = resolve_repo(client, getattr(args, "repo", None))
    handler = COMMANDS[(args.command, getattr(args, "subcommand", None))]
    return handler(client, repo, args, root)


def main(argv: list[str] | None = None) -> int:
    """Entry point: run and translate errors into gh-style exits."""
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except CliError as err:
        print(str(err), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
