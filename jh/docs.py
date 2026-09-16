"""Docs viewer for jh-server: markdown from a directory, read in the browser.

`jh-server --docs REPO=DIR` publishes `DIR` at `/:repo/docs/`. A `.md` path
returns the viewer page with the file's text embedded and a tree of every
markdown file under the root in the sidebar; any other path under the root is
served as a static file, so images referenced relatively resolve. Rendering
is client-side (marked and mermaid from cdnjs) because the server is stdlib
only; `?format=raw` returns the markdown itself and `?format=json` the tree.

Every rendered top-level block carries a stable `data-block` index and every
heading a GitHub-style id. Those are the anchors an annotation layer attaches
to later, so they exist now.
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

from jh.jh_lib import JhError

MARKED_CDN = "https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.12/marked.min.js"
MERMAID_CDN = "https://cdnjs.cloudflare.com/ajax/libs/mermaid/11.12.0/mermaid.min.js"


def parse_docs_args(specs: list[str]) -> dict[str, Path]:
    """Turn `REPO=DIR` command-line specs into a mapping, checking each dir.

    Args:
        specs: Values of repeated `--docs` flags.

    Returns:
        Repo name to resolved docs root.

    Raises:
        SystemExit: On a malformed spec or a missing directory.
    """
    docs: dict[str, Path] = {}
    for spec in specs:
        repo, sep, raw = spec.partition("=")
        if not sep or not repo or not raw:
            raise SystemExit(f"--docs expects REPO=DIR, got {spec!r}")
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            raise SystemExit(f"--docs {repo}: {root} is not a directory")
        docs[repo] = root
    return docs


def resolve(root: Path, rel: str) -> Path:
    """The file `rel` names under `root`, refusing anything that escapes it."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        raise JhError(404, f"no such document: {rel}")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as err:
        raise JhError(404, f"no such document: {rel}") from err
    if not candidate.is_file():
        raise JhError(404, f"no such document: {rel}")
    return candidate


def tree(root: Path) -> list[dict[str, str]]:
    """Every markdown file under `root`, sorted, with its title.

    The title is the first `# ` heading, else the file name. Hidden
    directories and files are skipped.
    """
    out: list[dict[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            out.append(
                {
                    "path": rel,
                    "dir": str(Path(rel).parent) if "/" in rel else "",
                    "title": _title(path, name),
                }
            )
    return out


def _title(path: Path, fallback: str) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("# "):
                    return line[2:].strip()
                if line.strip() and not line.startswith(("---", "```")):
                    break
    except OSError:
        pass
    return fallback


def index_path(root: Path) -> str | None:
    """The document `/:repo/docs` opens: `index.md` or `README.md` at the root, else the first in the tree."""
    for name in ("index.md", "README.md"):
        if (root / name).is_file():
            return name
    files = tree(root)
    return files[0]["path"] if files else None


def serve(handler: Any, repo: str, root: Path, rel: str, query: dict[str, Any]) -> None:
    """Answer one `GET /:repo/docs/<rel>` on `handler`."""
    path = resolve(root, rel)
    fmt = query.get("format")
    if fmt == "json":
        handler._send_json(200, {"repo": repo, "root": str(root), "files": tree(root)})
        return
    if not rel.endswith(".md"):
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        handler._send(200, ctype, path.read_bytes())
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if fmt == "raw":
        handler._send(200, "text/markdown; charset=utf-8", text.encode("utf-8"))
        return
    updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    handler._send_html(render_page(repo, rel, text, tree(root), updated))


def render_page(
    repo: str, rel: str, text: str, files: list[dict[str, str]], updated: str
) -> str:
    """The viewer page for one document."""
    title = next((f["title"] for f in files if f["path"] == rel), Path(rel).name)
    data = {
        "repo": repo,
        "path": rel,
        "title": title,
        "text": text,
        "files": files,
        "updated": updated,
    }
    payload = json.dumps(data).replace("</", "<\\/")
    return (
        _TEMPLATE.replace("__TITLE__", html.escape(f"{title} · {repo} docs"))
        .replace("__DATA__", payload)
        .replace("__MARKED__", MARKED_CDN)
        .replace("__MERMAID__", MERMAID_CDN)
    )


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Serif:ital,wght@0,400;0,600;1,400&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {
  color-scheme: light dark;
  --paper: #f3f5f7; --card: #ffffff; --ink: #1b2430; --ink-2: #3d4a58; --muted: #66727f; --rule: #d6dce2;
  --accent: #0f6e74; --accent-soft: #e0eff0; --code: #eaeef2; --hilite: #fbefdc;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #10161c; --card: #171f27; --ink: #e4e9ee; --ink-2: #b9c3cd; --muted: #8e9ba7; --rule: #2a343f;
    --accent: #4fb3b8; --accent-soft: #14343a; --code: #1e2831; --hilite: #33270f;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.6 "IBM Plex Sans", "Segoe UI", Helvetica, Arial, sans-serif;
  display: grid; grid-template-columns: 272px minmax(0, 1fr); grid-template-rows: auto 1fr; }
.mono { font-family: "IBM Plex Mono", Menlo, Consolas, monospace; }
header { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 8px 22px; align-items: baseline;
  padding: 12px 20px; border-bottom: 1px solid var(--rule); background: var(--card); position: sticky; top: 0; z-index: 2; }
header h1 { margin: 0; font: 500 12px/1 "IBM Plex Mono", Menlo, Consolas, monospace; letter-spacing: .12em; text-transform: uppercase; }
header h1 a { color: var(--accent); text-decoration: none; }
header .crumbs { color: var(--muted); font-size: 14px; }
header .crumbs a { color: var(--muted); text-decoration: none; }
header .crumbs a:hover { color: var(--accent); }
header .right { margin-left: auto; display: flex; gap: 16px; font-size: 13px; color: var(--muted); font-variant-numeric: tabular-nums; }
header .right a { color: var(--accent); text-decoration: none; }
nav { border-right: 1px solid var(--rule); padding: 18px 12px 28px 20px; overflow-y: auto;
  position: sticky; top: 49px; height: calc(100vh - 49px); font-size: 14px; }
nav h2 { margin: 18px 0 8px; font: 500 11.5px/1 "IBM Plex Mono", Menlo, Consolas, monospace; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
nav h2:first-child { margin-top: 0; }
nav ul { list-style: none; margin: 0; padding: 0; }
nav li a { display: block; padding: 4px 8px; border-radius: 4px; color: var(--ink-2); text-decoration: none;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
nav li a:hover { background: var(--code); color: var(--ink); }
nav li a.current { background: var(--accent-soft); color: var(--accent); font-weight: 500; }
nav .dir { color: var(--muted); font: 500 11.5px/1 "IBM Plex Mono", Menlo, Consolas, monospace; letter-spacing: .06em; padding: 10px 8px 4px; }
nav .outline a { color: var(--muted); }
nav .outline .h3 a { padding-left: 22px; font-size: 13px; }
main { min-width: 0; padding: 36px 40px 96px; }
article { max-width: 72ch; }
article h1, article h2, article h3, article h4 { font-family: "IBM Plex Serif", Georgia, serif; font-weight: 600; text-wrap: balance; letter-spacing: -.005em; }
article h1 { font-size: 38px; line-height: 1.15; margin: 0 0 18px; }
article h2 { font-size: 26px; line-height: 1.25; margin: 44px 0 14px; padding-top: 18px; border-top: 1px solid var(--rule); }
article h3 { font-size: 19px; line-height: 1.3; margin: 28px 0 8px; }
article h4 { font-size: 16.5px; margin: 22px 0 6px; }
article h1 + p { font-family: "IBM Plex Serif", Georgia, serif; font-size: 18.5px; line-height: 1.5; }
article h1 + pre + p { font-family: "IBM Plex Serif", Georgia, serif; font-size: 18.5px; line-height: 1.5; }
article p, article ul, article ol { margin: 0 0 14px; }
article ul, article ol { padding-left: 22px; }
article li { margin: 0 0 6px; }
article li::marker { color: var(--accent); }
article a { color: var(--accent); }
article em { color: var(--ink-2); }
article img { max-width: 100%; height: auto; display: block; margin: 14px 0; }
article code, article pre { font: 14px/1.55 "IBM Plex Mono", Menlo, Consolas, monospace; }
article code { background: var(--code); border-radius: 4px; padding: 1px 5px; }
article pre { background: var(--card); border: 1px solid var(--rule); border-radius: 6px; padding: 12px 14px; overflow-x: auto; margin: 0 0 16px; }
article pre code { background: none; border: 0; padding: 0; font-size: 13.5px; }
article pre.mermaid { text-align: center; }
article .table { overflow-x: auto; margin: 18px 0 16px; }
article table { border-collapse: collapse; width: 100%; font-size: 14.5px; font-variant-numeric: tabular-nums; }
article th, article td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--rule); vertical-align: top; }
article th { font-weight: 600; color: var(--muted); font-size: 12.5px; letter-spacing: .04em; text-transform: uppercase; }
article blockquote { margin: 0 0 16px; padding: 10px 16px; background: var(--card); border-left: 3px solid var(--accent); border-radius: 0 6px 6px 0; color: var(--ink-2); }
article blockquote p:last-child { margin: 0; }
article hr { border: 0; border-top: 1px solid var(--rule); margin: 28px 0; }
article h1 .anchor, article h2 .anchor, article h3 .anchor, article h4 .anchor { color: var(--muted); text-decoration: none; margin-left: 8px; opacity: 0; font-weight: 400; font-family: "IBM Plex Mono", Menlo, Consolas, monospace; font-size: .7em; }
article h1:hover .anchor, article h2:hover .anchor, article h3:hover .anchor, article h4:hover .anchor { opacity: 1; }
article [data-block]:target { background: var(--hilite); outline: 8px solid var(--hilite); border-radius: 2px; }
#error { display: none; color: #b42318; }
@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }
@media (max-width: 880px) {
  body { grid-template-columns: 1fr; }
  nav { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--rule); }
  main { padding: 20px 16px 64px; }
  article h1 { font-size: 30px; }
}
</style>
</head>
<body>
<header>
  <h1><a href="./" id="repo-link">docs</a></h1>
  <span class="crumbs" id="crumbs"></span>
  <span class="right">
    <span id="updated"></span>
    <a id="raw-link" href="?format=raw">raw</a>
    <a id="board-link" href="../board">board</a>
  </span>
</header>
<nav>
  <h2>Documents</h2>
  <ul id="files"></ul>
  <h2>On this page</h2>
  <ul id="outline" class="outline"></ul>
</nav>
<main>
  <p id="error"></p>
  <article id="doc"></article>
</main>
<script src="__MARKED__"></script>
<script src="__MERMAID__"></script>
<script>
const DATA = __DATA__;
const depth = DATA.path.split("/").length - 1;
const toRoot = depth ? "../".repeat(depth) : "./";
document.title = DATA.title + " · " + DATA.repo + " docs";
document.getElementById("repo-link").textContent = DATA.repo + " docs";
document.getElementById("repo-link").href = toRoot;
document.getElementById("board-link").href = toRoot + "../board";
document.getElementById("updated").textContent = "updated " + DATA.updated;

// Breadcrumbs: directory segments, then the file.
const crumbs = document.getElementById("crumbs");
const segs = DATA.path.split("/");
segs.forEach((s, i) => {
  if (i) crumbs.appendChild(document.createTextNode(" / "));
  const last = i === segs.length - 1;
  const el = document.createElement(last ? "span" : "a");
  el.textContent = s;
  if (!last) el.href = toRoot + segs.slice(0, i + 1).join("/") + "/";
  crumbs.appendChild(el);
});

// File tree grouped by directory.
const files = document.getElementById("files");
let lastDir = null;
for (const f of DATA.files) {
  if (f.dir !== lastDir) {
    lastDir = f.dir;
    if (f.dir) { const d = document.createElement("li"); d.className = "dir"; d.textContent = f.dir + "/"; files.appendChild(d); }
  }
  const li = document.createElement("li");
  const a = document.createElement("a");
  a.href = toRoot + f.path;
  a.textContent = f.title;
  a.title = f.path;
  if (f.path === DATA.path) a.className = "current";
  li.appendChild(a);
  files.appendChild(li);
}

// Render.
function slug(text) {
  return text.toLowerCase().replace(/[^\w\s-]/g, "").trim().replace(/\s+/g, "-");
}
try {
  marked.use({ gfm: true, breaks: false });
  const doc = document.getElementById("doc");
  doc.innerHTML = marked.parse(DATA.text);
  // Heading ids and anchors; outline.
  const seen = {};
  const outline = document.getElementById("outline");
  for (const h of doc.querySelectorAll("h1, h2, h3, h4")) {
    let id = slug(h.textContent) || "section";
    if (seen[id] !== undefined) { seen[id] += 1; id = id + "-" + seen[id]; } else { seen[id] = 0; }
    h.id = id;
    const a = document.createElement("a");
    a.className = "anchor"; a.href = "#" + id; a.textContent = "#"; a.setAttribute("aria-label", "link to this section");
    h.appendChild(a);
    if (h.tagName === "H2" || h.tagName === "H3") {
      const li = document.createElement("li");
      li.className = h.tagName.toLowerCase();
      const link = document.createElement("a");
      link.href = "#" + id; link.textContent = h.textContent.replace(/#$/, "");
      li.appendChild(link); outline.appendChild(li);
    }
  }
  // Wrap tables so wide ones scroll on their own.
  for (const t of doc.querySelectorAll("table")) {
    const w = document.createElement("div"); w.className = "table";
    t.parentNode.insertBefore(w, t); w.appendChild(t);
  }
  // Mermaid fences.
  for (const c of doc.querySelectorAll("pre > code.language-mermaid")) {
    const pre = c.parentNode;
    const m = document.createElement("pre"); m.className = "mermaid"; m.textContent = c.textContent;
    pre.replaceWith(m);
  }
  // Stable block anchors: the hook for annotations.
  let i = 0;
  for (const el of doc.children) { el.dataset.block = String(i++); if (!el.id) el.id = "b" + el.dataset.block; }
  if (window.mermaid) {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    mermaid.initialize({ startOnLoad: false, theme: dark ? "dark" : "neutral", fontFamily: "IBM Plex Sans, sans-serif" });
    mermaid.run({ nodes: doc.querySelectorAll("pre.mermaid") });
  }
  if (location.hash) { const t = document.querySelector(location.hash); if (t) t.scrollIntoView(); }
} catch (err) {
  const e = document.getElementById("error");
  e.style.display = "block";
  e.textContent = "Could not render (is the marked CDN reachable?): " + err;
  document.getElementById("doc").innerHTML = "<pre>" + DATA.text.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])) + "</pre>";
}
</script>
</body>
</html>
"""
