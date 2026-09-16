"""Shared fixtures: an in-process jh-server on a spare port and a CLI runner."""

from __future__ import annotations

import io
import os
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Callable

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from jh import jh_cli  # noqa: E402
from jh.jh_lib import Store  # noqa: E402
from jh.jh_server import JhServer  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "gh-2.88.1"


class RunningServer:
    """A jh-server running in a thread on a temp home.

    Attributes:
        home: Log base directory.
        server: The `JhServer` instance.
        url: Base URL.
    """

    def __init__(self, home: Path, docs: dict[str, Path] | None = None) -> None:
        """Start serving `home` on a free port, with optional docs roots."""
        self.home = home
        self.docs = docs
        self.server = JhServer("127.0.0.1", 0, home, docs=docs)
        self.url = self.server.url
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def store(self) -> Store:
        """The server's store."""
        return self.server.store

    def restart(self) -> None:
        """Stop and start again on the same home (a different port is fine)."""
        self.stop()
        self.server = JhServer("127.0.0.1", 0, self.home, docs=self.docs)
        self.url = self.server.url
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Shut the server down."""
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def running(tmp_path: Path) -> Any:
    """A running server on a temp home."""
    srv = RunningServer(tmp_path / "home")
    yield srv
    srv.stop()


class CliResult:
    """Captured result of one CLI invocation.

    Attributes:
        code: Exit status.
        out: Captured stdout.
        err: Captured stderr.
    """

    def __init__(self, code: int, out: str, err: str) -> None:
        """Record one invocation's exit status and captured streams."""
        self.code = code
        self.out = out
        self.err = err

    def __repr__(self) -> str:
        """Debug form showing code, stdout and stderr."""
        return f"CliResult(code={self.code}, out={self.out!r}, err={self.err!r})"


@pytest.fixture
def cli(
    running: RunningServer, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., CliResult]:
    """Run `jh` in-process against the running server as actor `claude`."""
    monkeypatch.setenv("JH_SERVER", running.url)
    monkeypatch.setenv("JH_ACTOR", "claude")
    monkeypatch.setenv("JH_REPO", "demo")
    monkeypatch.setattr(jh_cli, "is_tty", lambda: False)

    def run(
        *argv: str, stdin: str | None = None, actor: str | None = None
    ) -> CliResult:
        out, err = io.StringIO(), io.StringIO()
        old_env = os.environ.get("JH_ACTOR")
        if actor:
            os.environ["JH_ACTOR"] = actor
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = jh_cli.main(list(argv))
        finally:
            sys.stdin = old_stdin
            if actor:
                os.environ["JH_ACTOR"] = old_env or ""
        return CliResult(code, out.getvalue(), err.getvalue())

    run("repo", "create", "demo")
    return run
