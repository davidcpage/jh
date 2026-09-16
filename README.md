# jh: a local `gh issue` clone

`jh` is `gh issue` character for character, backed by a small HTTP server
(`jh-server`) over an append-only JSONL event log instead of GitHub. The name
is short for JSONL hub. Agents already know `gh`; the only thing to learn is
that the binary is called `jh`. Stdlib Python only, no dependencies.

It is meant for a project whose issue tracker is read and written mostly by
coding agents: the CLI is the interface they already have, the board is the
view for the person, and the log is a file you can back up with `cp`.

```
bin/jh, bin/jh-server      launchers for running from a checkout
jh/jh_lib.py               model, event log, replay, ready/blocked logic
jh/jh_server.py            single-threaded http.server, REST subset, /board
jh/jh_cli.py               argparse mirroring gh's grammar, --json/--jq
jh/board.py                board HTML (live on the server, or a snapshot)
jh/docs.py                 docs viewer: markdown under --docs REPO=DIR at /REPO/docs
skills/issues/SKILL.md     the one-paragraph skill agents read
tests/                     pytest, incl. gh 2.88.1 parity fixtures
```

## Install

Either install the package:

```bash
uv tool install git+https://github.com/davidcpage/jh      # or: pipx install git+https://github.com/davidcpage/jh
jh --help
```

or run straight from a checkout, which needs nothing but Python 3.11+:

```bash
git clone https://github.com/davidcpage/jh
export PATH="$PATH:$PWD/jh/bin"
```

The checkout doubles as a Claude Code plugin: `claude --plugin-dir ./jh`
loads the `issues` skill.

## Environment

| Variable    | Meaning                                                 | Default                     |
| ----------- | ------------------------------------------------------- | --------------------------- |
| `JH_REPO`   | Issue space to target when `-R` is not given            | none; the error lists repos |
| `JH_ACTOR`  | Name recorded on every event (`author.login`, comments) | login name                  |
| `JH_SERVER` | Server URL for the client                               | `http://127.0.0.1:7411`     |
| `JH_HOME`   | Log base directory for the server                       | `~/.local/share/jh`         |

Agent sessions should set `JH_ACTOR=claude`; you appear as yourself from
your own shell. Put `JH_REPO=<repo>` in the project's environment (for
example a direnv file or the project's `CLAUDE.md`) so sessions never think
about it.

To let agents call `jh` without a permission prompt, allow it in the
project's `.claude/settings.json`:

```json
{ "permissions": { "allow": ["Bash(jh *)"] } }
```

## Running the server

The server binds `127.0.0.1:7411` and writes one `events.jsonl` per repo
under `~/.local/share/jh/<repo>/`. On a machine that stays up, run it as a
user-level systemd service:

```ini
# ~/.config/systemd/user/jh-server.service
[Unit]
Description=jh issue tracker server

[Service]
ExecStart=%h/.local/bin/jh-server --host 127.0.0.1 --port 7411
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now jh-server
loginctl enable-linger "$USER"     # keep it running when you log out
jh repo create demo
```

Backup is a copy of `~/.local/share/jh/`. If the log is ever edited by hand,
restart the service; it replays from scratch and there is no cache.

## Reaching it from another machine

One line in `~/.ssh/config` for the host running the server:

```
Host myserver
    LocalForward 7411 127.0.0.1:7411
```

Then `jh` and the board (`http://127.0.0.1:7411/demo/board`) work unchanged
on the other machine while the SSH session is up. There is no
authentication beyond SSH because there is no exposure beyond SSH. Several
people pointing `jh` at the same server share one board; the actor name on
each event is whatever their `JH_ACTOR` says.

## Usage

Everything `gh issue` does, with the same flags and the same `--json` fields:

```bash
jh issue create -t "Port VOQ router" -b "..." -l component:noc -m "Increment 1" --blocked-by 3,7
jh issue list --ready                       # open, not blocked by any open issue, not labelled draft
jh issue list -s all --json number,title,state -q '.[].number'
jh issue view 12 --json blockedBy -q '.blockedBy[].number'
jh issue edit 12 --add-label in-progress
jh issue close 12 -r completed -c "what changed"   # also drops the in-progress label
jh label create component:noc -c 0e8a16 -d "NoC work"
jh milestone create -t "Increment 1" --due-on 2026-10-01
jh board                                    # prints the board URL
jh board --snapshot board.html              # self-contained copy
```

Issue bodies and comments are GitHub-flavoured markdown. The board renders
them in the browser (marked from cdnjs, as the docs viewer does): raw HTML
is shown escaped, `#12` links to that card, mermaid fences are drawn. When
the repo has a docs root (below), a document path in the text links into
the docs viewer: `docs/plan.md`, `plan.md` if the name is unique in the
tree, and `docs/plan.md#Heading text` or `[the plan](docs/plan.md#Heading)`
for a section, the heading slugged the way the viewer slugs its ids.

Extensions (GitHub concepts gh's CLI does not expose) are `--blocked-by`,
`--add-blocked-by` / `--remove-blocked-by`, `jh issue list --ready |
--blocked-by N | --blocking N`, the `blockedBy` / `blocking` JSON fields,
`jh milestone`, `jh repo` and `jh board`. gh flags with no local meaning
(`--web`, `--template`, `--project`, `--editor`, ...) are accepted by the
parser and fail with a message naming the supported set. `create` without
both `--title` and `--body` errors instead of prompting.

`--jq` uses the `jq` binary when one is on `PATH`. Without it a built-in
subset handles dot paths (`.a`, `.a.b`, `.[]`, `.[0]`, `.a[].b`), pipes
between them, `length` and `keys`; anything richer needs real `jq`.

## Docs viewer

`jh-server --docs REPO=DIR` (repeatable) serves the markdown under `DIR` at
`http://127.0.0.1:7411/REPO/docs/`, rendered in the browser with a file tree
and an outline in the sidebar; images and other files referenced relatively
are served from the same directory, so a `docs/` tree that renders under
mkdocs renders here unchanged, including mermaid fences. `?format=raw` on a
document returns the markdown, `?format=json` on `/REPO/docs` the file tree.
Rendered blocks carry stable `data-block` ids and headings GitHub-style ids;
those are the anchors a future annotation layer attaches comments to.

```bash
jh-server --docs demo=path/to/docs
open http://127.0.0.1:7411/demo/docs/
```

## Tests

```bash
python3 -m pytest tests
```

`tests/fixtures/gh-2.88.1/` holds the help texts and `--json` field lists
captured from gh 2.88.1. The parity tests compare jh's parser against them,
so a divergence is a failing test rather than a surprise for an agent.

## Licence

MIT.
