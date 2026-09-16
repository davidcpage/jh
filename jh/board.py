"""Board HTML for jh.

One row per milestone (plus "No milestone"), columns Draft / Blocked /
Ready / In progress / Closed (the path a card takes, left to right), a label filter, a dependency list on every
card and a mermaid dependency graph (every open issue is a node, with or
without dependencies). Issue bodies and comments are GitHub-flavoured
markdown, rendered in the browser with marked (raw HTML in them is shown
escaped, `#N` links to the card for issue N, mermaid fences are drawn). The
server renders it live at `/:repo/board` (polling `/:repo/events?since=SEQ`);
`jh board --snapshot FILE` writes the same page with the data inlined and
polling disabled. The theme starts from the browser's preference; the
sun/moon in the header flips it, remembered per browser. Both marked and
mermaid come from cdnjs; without them the
page still renders, with bodies as plain text and the graph as source.
"""

from __future__ import annotations

import html
import json
from typing import Any

from jh.jh_lib import DRAFT_LABEL, IN_PROGRESS_LABEL, Store

MARKED_CDN = "https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.12/marked.min.js"
MERMAID_CDN = "https://cdnjs.cloudflare.com/ajax/libs/mermaid/11.12.0/mermaid.min.js"


def board_data(store: Store, repo: str) -> dict[str, Any]:
    """Collect everything the board needs as plain JSON."""
    state = store.repo(repo)
    return {
        "repo": repo,
        "baseUrl": store.base_url,
        "seq": state.seq,
        "issues": store.issue_list(repo, {"state": "all", "limit": 100000}),
        "labels": store.label_list(repo, {"limit": 100000}),
        "milestones": store.milestone_list(repo, {"state": "all"}),
    }


def render_board(store: Store, repo: str, live: bool = True) -> str:
    """Render the board page.

    Args:
        store: The store to read from.
        repo: Repo name.
        live: When true the page polls the server for new events and reloads;
            when false it is a self-contained snapshot.

    Returns:
        A complete HTML document.
    """
    return render_from_data(board_data(store, repo), live=live)


def render_from_data(data: dict[str, Any], live: bool = False) -> str:
    """Render the board page from a `board_data` payload (used for snapshots)."""
    repo = data["repo"]
    payload = json.dumps(data).replace("</", "<\\/")
    title = f"{repo} board"
    if not live:
        title += " (snapshot)"
    return (
        _TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__DATA__", payload)
        .replace("__LIVE__", "true" if live else "false")
        .replace("__MARKED__", MARKED_CDN)
        .replace("__MERMAID__", MERMAID_CDN)
        .replace("__INPROGRESS__", IN_PROGRESS_LABEL)
        .replace("__DRAFT__", DRAFT_LABEL)
    )


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #fafafa; --fg: #1f2328; --muted: #656d76; --line: #d0d7de; --card: #ffffff;
  --ready: #1a7f37; --blocked: #bc4c00; --progress: #0969da; --closed: #8250df; --draft: #6e7781;
  --hilite: #fff8c5; --code: #f0f2f4; --panel: #f0f2f5; --chip: #e9ecf0;
  --tint-draft: #eef0f2; --tint-ready: #e6f3ea; --tint-blocked: #f8ece2; --tint-progress: #e4eef9; --tint-closed: #eee9f8;
  --shadow: 0 1px 2px rgba(31, 35, 40, .08);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --line: #30363d; --card: #161b22;
    --ready: #3fb950; --blocked: #f0883e; --progress: #58a6ff; --closed: #a371f7; --draft: #8b949e;
    --hilite: #3b3419; --code: #21262d; --panel: #161b22; --chip: #1c2128;
    --tint-draft: #181b20; --tint-ready: #0f1f16; --tint-blocked: #221709; --tint-progress: #0e1a2e; --tint-closed: #181427;
    --shadow: 0 1px 0 rgba(255, 255, 255, .04) inset, 0 6px 16px rgba(0, 0, 0, .35);
  }
}
:root[data-theme="light"] { color-scheme: light; }
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --line: #30363d; --card: #161b22;
  --ready: #3fb950; --blocked: #f0883e; --progress: #58a6ff; --closed: #a371f7; --draft: #8b949e;
  --hilite: #3b3419; --code: #21262d; --panel: #161b22; --chip: #1c2128;
  --tint-draft: #181b20; --tint-ready: #0f1f16; --tint-blocked: #221709; --tint-progress: #0e1a2e; --tint-closed: #181427;
  --shadow: 0 1px 0 rgba(255, 255, 255, .04) inset, 0 6px 16px rgba(0, 0, 0, .35);
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
header { display: flex; flex-wrap: wrap; gap: 12px 24px; align-items: baseline; margin-bottom: 12px; }
h1 { font-size: 24px; font-weight: 800; letter-spacing: -.02em; margin: 0; }
h1 small { color: var(--muted); font-weight: 500; font-size: 14px; margin-left: 8px; letter-spacing: 0; }
#filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
#filters label { display: inline-flex; align-items: center; gap: 6px; border-radius: 999px; padding: 3px 10px;
  cursor: pointer; font-size: 12px; font-weight: 700; background: var(--chip); color: var(--muted); }
#filters label.on { background: var(--fg); color: var(--bg); }
.swatch { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.milestone { margin: 18px 0; border-top: 1px solid var(--line); padding-top: 8px; }
.milestone > summary { list-style: none; cursor: pointer; display: flex; align-items: baseline; gap: 8px; }
.milestone > summary::-webkit-details-marker { display: none; }
.milestone > summary::before { content: "▸"; color: var(--muted); font-size: 12px; flex: 0 0 12px; }
.milestone[open] > summary::before { content: "▾"; }
.milestone h2 { font-size: 17px; font-weight: 800; margin: 0 0 10px; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
.milestone h2 .meta { color: var(--muted); font-weight: 700; font-size: 12px; }
.milestone h2 .bar { flex: 0 0 160px; height: 6px; border-radius: 3px; background: var(--chip); overflow: hidden; }
.milestone h2 .bar span { display: block; height: 100%; background: var(--closed); }
.columns { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
@media (max-width: 1100px) { .columns { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 800px) { .columns { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 500px) { .columns { grid-template-columns: 1fr; } }
.col { border-radius: 14px; padding: 8px; display: flex; flex-direction: column; gap: 8px; background: var(--panel); }
.col.draft { background: var(--tint-draft); } .col.ready { background: var(--tint-ready); } .col.blocked { background: var(--tint-blocked); }
.col.progress { background: var(--tint-progress); } .col.closed { background: var(--tint-closed); }
.col h3 { font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: .06em; margin: 0; padding: 6px 10px;
  border-radius: 8px; color: #0b0e12; display: flex; justify-content: space-between; background: var(--muted); }
.col.ready h3 { background: var(--ready); } .col.blocked h3 { background: var(--blocked); } .col.draft h3 { background: var(--draft); }
.col.progress h3 { background: var(--progress); } .col.closed h3 { background: var(--closed); }
.col.closed h3, .col.progress h3 { color: #ffffff; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) .col.closed h3, :root:not([data-theme="light"]) .col.progress h3 { color: #0b0e12; } }
:root[data-theme="dark"] .col.closed h3, :root[data-theme="dark"] .col.progress h3 { color: #0b0e12; }
#theme { margin-left: auto; border: 0; background: none; color: var(--muted); padding: 4px; cursor: pointer;
  display: inline-flex; align-items: center; border-radius: 6px; opacity: .7; }
#theme:hover { opacity: 1; background: var(--chip); }
#theme svg { width: 16px; height: 16px; stroke: currentColor; fill: none; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
#theme .sun { display: none; } :root[data-theme="dark"] #theme .sun { display: block; } :root[data-theme="dark"] #theme .moon { display: none; }
.card { background: var(--card); border-radius: 10px; padding: 12px; box-shadow: var(--shadow); display: flex; flex-direction: column; gap: 8px; }
.card.hilite { background: var(--hilite); }
.col.closed .card { opacity: .6; } .col.closed .card:hover, .col.closed .card.hilite { opacity: 1; }
.card .num { font-weight: 800; font-variant-numeric: tabular-nums; }
.col.ready .num { color: var(--ready); } .col.blocked .num { color: var(--blocked); } .col.draft .num { color: var(--draft); }
.col.progress .num { color: var(--progress); } .col.closed .num { color: var(--closed); }
.card .title { font-weight: 700; font-size: 14px; line-height: 1.35; }
.card .labels { display: flex; flex-wrap: wrap; gap: 4px; }
.card .foot { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.card .foot .deps { display: flex; gap: 10px; flex-wrap: wrap; margin: 0; }
.lbl { font-size: 11px; font-weight: 700; border-radius: 4px; padding: 1px 6px; }
.card details.more summary { cursor: pointer; list-style: none; }
.card details.more summary::-webkit-details-marker { display: none; }
.card details.more[open] summary .title { text-decoration: underline dotted; }
.card .meta { font-size: 12px; color: var(--muted); }
.card .body { margin-top: 6px; font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }
.card .body.plain { white-space: pre-wrap; }
.card .body > :first-child { margin-top: 0; } .card .body > :last-child { margin-bottom: 0; }
.card .body p, .card .body ul, .card .body ol, .card .body pre, .card .body table, .card .body blockquote { margin: 6px 0; }
.card .body h1, .card .body h2, .card .body h3, .card .body h4 { font-size: 13px; margin: 10px 0 4px; }
.card .body h1 { font-size: 14px; }
.card .body ul, .card .body ol { padding-left: 22px; }
.card .body li + li { margin-top: 2px; }
.card .body code { font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: var(--code); border-radius: 4px; padding: 1px 4px; }
.card .body pre { background: var(--code); border-radius: 6px; padding: 8px 10px; overflow-x: auto; }
.card .body pre code { background: none; padding: 0; }
.card .body pre.mermaid { background: none; text-align: center; }
.card .body blockquote { border-left: 3px solid var(--line); color: var(--muted); padding: 0 10px; }
.card .body table { border-collapse: collapse; display: block; max-width: 100%; overflow-x: auto; }
.card .body th, .card .body td { border: 1px solid var(--line); padding: 3px 8px; text-align: left; }
.card .body th { background: var(--code); }
.card .body img { max-width: 100%; }
.card .body a { color: var(--progress); }
.card .body hr { border: 0; border-top: 1px solid var(--line); }
.card .body input[type=checkbox] { margin: 0 4px 0 0; vertical-align: middle; }
.card .comments { border-top: 1px solid var(--line); padding-top: 6px; }
.card .comment { margin-bottom: 8px; } .card .comment .who { font-weight: 600; font-size: 12px; }
.card .comment .when { font-size: 12px; color: var(--muted); }
.card.hilite { transition: background 1s; }
.deps { font-size: 12px; font-weight: 500; color: var(--muted); }
.deps .open { color: var(--blocked); } .deps .closed { text-decoration: line-through; }
.deps a { color: inherit; }
.empty { color: var(--muted); font-size: 12px; font-style: italic; padding: 2px 4px; }
#graphbox { margin-top: 24px; border-top: 1px solid var(--line); padding-top: 8px; }
summary { cursor: pointer; font-weight: 600; }
#graph { overflow-x: auto; margin-top: 8px; }
footer { margin-top: 24px; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <div id="filters"></div>
  <button id="theme" title="Switch theme" aria-label="Switch theme">
    <svg class="moon" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
    <svg class="sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
  </button>
</header>
<main id="board"></main>
<details id="graphbox"><summary>Dependency graph</summary><div id="graph"></div></details>
<footer id="foot"></footer>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  var LIVE = __LIVE__;
  var IN_PROGRESS = "__INPROGRESS__";
  var DRAFT = "__DRAFT__";
  var data = JSON.parse(document.getElementById("data").textContent);
  var active = {};
  // Light or dark: the browser's preference until the reader clicks the
  // sun/moon in the header, then that choice, remembered per browser.
  var themeKey = "jh-board-theme";
  var theme = null;
  try { theme = localStorage.getItem(themeKey); } catch (e) {}
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function isDark() { return theme === "dark"; }
  function applyTheme() {
    document.documentElement.setAttribute("data-theme", theme);
    if (window.mermaid) window.mermaid.initialize({ startOnLoad: false, theme: isDark() ? "dark" : "default" });
  }
  applyTheme();
  document.getElementById("theme").onclick = function () {
    theme = isDark() ? "light" : "dark";
    try { localStorage.setItem(themeKey, theme); } catch (e) {}
    applyTheme(); render();
  };
  var collapsedKey = "jh-board-collapsed-" + data.repo;
  var collapsed = {};
  try { collapsed = JSON.parse(localStorage.getItem(collapsedKey) || "{}") || {}; } catch (e) { collapsed = {}; }
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); };

  // Bodies and comments are GitHub-flavoured markdown. Raw HTML inside them
  // is escaped rather than trusted, "#12" becomes a link to that card, and
  // mermaid fences are drawn once the card is on the page. Until marked has
  // loaded (or if it never does) the text is shown escaped, whitespace kept.
  var markedReady = false;
  function setupMarked() {
    if (!window.marked) return;
    var link = { name: "issueref", level: "inline",
      start: function (src) { var m = /(^|[^\w&])#\d/.exec(src); return m ? m.index + m[1].length : undefined; },
      tokenizer: function (src) {
        var m = /^#(\d+)(?![\w#])/.exec(src);
        if (m) return { type: "issueref", raw: m[0], number: m[1] };
      },
      renderer: function (t) { return '<a href="#issue-' + t.number + '">#' + t.number + '</a>'; } };
    window.marked.use({ gfm: true, breaks: false, extensions: [link],
      renderer: { html: function (t) { return esc(t.raw || t.text || t); } } });
    markedReady = true;
  }
  function md(text) {
    if (!text) return "";
    if (!markedReady) return '<div class="body plain">' + esc(text) + '</div>';
    try { return '<div class="body">' + window.marked.parse(text) + '</div>'; }
    catch (e) { return '<div class="body plain">' + esc(text) + '</div>'; }
  }
  function drawMermaid(root) {
    if (!window.mermaid) return;
    var fences = root.querySelectorAll(".body pre > code.language-mermaid");
    if (!fences.length) return;
    var nodes = [];
    Array.prototype.forEach.call(fences, function (c) {
      var m = document.createElement("pre"); m.className = "mermaid"; m.textContent = c.textContent;
      c.parentNode.replaceWith(m); nodes.push(m);
    });
    try { var p = window.mermaid.run({ nodes: nodes }); if (p && p.catch) p.catch(function () {}); } catch (e) {}
  }
  function textColor(hex) {
    var n = parseInt(hex || "ededed", 16);
    var r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#1f2328" : "#ffffff";
  }
  function ago(iso) {
    if (!iso) return "";
    var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return "about " + Math.round(s / 60) + " minutes ago";
    if (s < 86400) return "about " + Math.round(s / 3600) + " hours ago";
    return "about " + Math.round(s / 86400) + " days ago";
  }
  function labelChip(l) {
    return '<span class="lbl" style="background:#' + esc(l.color) + ';color:' + textColor(l.color) +
      '" title="' + esc(l.description) + '">' + esc(l.name) + '</span>';
  }
  function isBlocked(issue, byNumber) {
    return issue.blockedBy.some(function (d) { var b = byNumber[d.number]; return b && b.state === "OPEN"; });
  }
  function column(issue, byNumber) {
    if (issue.state === "CLOSED") return "closed";
    if (issue.labels.some(function (l) { return l.name === IN_PROGRESS; })) return "progress";
    if (issue.labels.some(function (l) { return l.name === DRAFT; })) return "draft";
    return isBlocked(issue, byNumber) ? "blocked" : "ready";
  }
  function card(issue, byNumber) {
    var deps = "";
    if (issue.blockedBy.length) {
      deps = '<span class="deps">blocked by ' + issue.blockedBy.map(function (d) {
        var b = byNumber[d.number];
        var cls = b && b.state === "OPEN" ? "open" : "closed";
        return '<a class="' + cls + '" href="#issue-' + d.number + '" title="' + esc(d.title) + '">#' + d.number + '</a>';
      }).join(", ") + '</span>';
    }
    if (issue.blocking.length) {
      deps += '<span class="deps">blocks ' + issue.blocking.map(function (d) {
        return '<a href="#issue-' + d.number + '" title="' + esc(d.title) + '">#' + d.number + '</a>';
      }).join(", ") + '</span>';
    }
    var meta = [issue.state === "CLOSED" ? "closed" + (issue.stateReason ? " (" + issue.stateReason.toLowerCase().replace("_", " ") + ")" : "") : "open",
      "opened by " + esc(issue.author && issue.author.login || "?") + " " + ago(issue.createdAt),
      issue.updatedAt !== issue.createdAt ? "updated " + ago(issue.updatedAt) : ""].filter(Boolean).join(" · ");
    var body = issue.body ? md(issue.body) : '<div class="body empty">No description.</div>';
    var comments = issue.comments.length ? '<div class="comments">' + issue.comments.map(function (c) {
      return '<div class="comment"><span class="who">' + esc(c.author && c.author.login || "?") + '</span> <span class="when">' + ago(c.createdAt) + '</span>' +
        md(c.body) + '</div>';
    }).join("") + '</div>' : "";
    var count = issue.comments.length ? " · " + issue.comments.length + (issue.comments.length === 1 ? " comment" : " comments") : "";
    return '<div class="card" id="issue-' + issue.number + '">' +
      '<details class="more"><summary>' +
      '<span class="title">' + esc(issue.title) + '</span>' +
      (issue.assignees.length ? '<span class="deps"> · ' + esc(issue.assignees.map(function (a) { return a.login; }).join(", ")) + '</span>' : "") +
      '</summary>' +
      '<div class="meta">' + meta + count + '</div>' + body + comments + '</details>' +
      '<div class="labels">' + issue.labels.map(labelChip).join("") + '</div>' +
      '<div class="foot"><span class="num">#' + issue.number + '</span><div class="deps">' + deps + '</div></div></div>';
  }
  function render() {
    var byNumber = {};
    data.issues.forEach(function (i) { byNumber[i.number] = i; });
    var wanted = Object.keys(active).filter(function (k) { return active[k]; });
    var shown = data.issues.filter(function (i) {
      return wanted.every(function (w) { return i.labels.some(function (l) { return l.name === w; }); });
    });
    document.getElementById("title").innerHTML = esc(data.repo) + ' <small>' + shown.length + ' of ' +
      data.issues.length + ' issues' + (LIVE ? "" : " · snapshot") + '</small>';

    var filters = document.getElementById("filters");
    filters.innerHTML = data.labels.map(function (l) {
      return '<label class="' + (active[l.name] ? "on" : "") + '" data-name="' + esc(l.name) + '">' +
        '<span class="swatch" style="background:#' + esc(l.color) + '"></span>' + esc(l.name) + '</label>';
    }).join("");
    Array.prototype.forEach.call(filters.children, function (el) {
      el.onclick = function () { active[el.dataset.name] = !active[el.dataset.name]; render(); };
    });

    // Order: open before closed; then the leading number in the title (the
    // increment), so "0 · review" follows "0 · tracer bullet"; then creation.
    function stage(m) { var k = /^\s*(\d+)/.exec(m.title || ""); return k ? parseInt(k[1], 10) : 1e9; }
    var rows = data.milestones.slice().sort(function (a, b) {
      if (a.state !== b.state) return a.state === "OPEN" ? -1 : 1;
      return stage(a) - stage(b) || (a.dueOn || "9999").localeCompare(b.dueOn || "9999") || a.number - b.number;
    });
    rows.push({ number: null, title: "No milestone", state: "OPEN", dueOn: null, description: "" });
    var cols = [["draft", "Draft"], ["blocked", "Blocked"], ["ready", "Ready"], ["progress", "In progress"], ["closed", "Closed"]];
    var out = rows.map(function (m) {
      var mine = shown.filter(function (i) { return (i.milestone ? i.milestone.number : null) === m.number; });
      if (!mine.length && m.number !== null && m.state === "CLOSED") return "";
      var meta = [];
      if (m.dueOn) meta.push("due " + m.dueOn.slice(0, 10));
      if (m.state === "CLOSED") meta.push("closed");
      var closedCount = mine.filter(function (i) { return i.state === "CLOSED"; }).length;
      meta.push(closedCount + "/" + mine.length + " done");
      // Collapsed when every issue is closed, unless the reader has toggled it.
      var key = m.number === null ? "none" : String(m.number);
      var allDone = mine.length > 0 && closedCount === mine.length;
      var isOpen = collapsed[key] === undefined ? !allDone : !collapsed[key];
      var pct = mine.length ? Math.round(100 * closedCount / mine.length) : 0;
      return '<details class="milestone" data-key="' + key + '"' + (isOpen ? " open" : "") + '><summary><h2>' + esc(m.title) +
        '<span class="bar" title="' + pct + '%"><span style="width:' + pct + '%"></span></span><span class="meta">' + esc(meta.join(" · ")) +
        (m.description ? " · " + esc(m.description) : "") + '</span></h2></summary><div class="columns">' +
        cols.map(function (c) {
          var items = mine.filter(function (i) { return column(i, byNumber) === c[0]; });
          if (c[0] === "closed") items.sort(function (a, b) { return (b.closedAt || "").localeCompare(a.closedAt || ""); });
          else items.sort(function (a, b) { return a.number - b.number; });
          return '<div class="col ' + c[0] + '"><h3>' + c[1] + '<span class="n">' + items.length + '</span></h3>' +
            (items.length ? items.map(function (i) { return card(i, byNumber); }).join("") : '<div class="empty">none</div>') + '</div>';
        }).join("") + '</div></details>';
    }).join("");
    document.getElementById("board").innerHTML = out;
    drawMermaid(document.getElementById("board"));
    Array.prototype.forEach.call(document.querySelectorAll("details.milestone"), function (d) {
      d.addEventListener("toggle", function () {
        collapsed[d.dataset.key] = !d.open;
        try { localStorage.setItem(collapsedKey, JSON.stringify(collapsed)); } catch (e) {}
      });
    });
    document.getElementById("foot").textContent = (LIVE ? "Live from " + data.baseUrl + " · " : "Snapshot · ") +
      "log seq " + data.seq + " · in progress = label \"" + IN_PROGRESS + "\"";
    window.__lastShown = { shown: shown, byNumber: byNumber };
    if (document.getElementById("graphbox").open) { renderGraph(shown, byNumber); }
    highlightHash();
  }
  function renderGraph(shown, byNumber) {
    var box = document.getElementById("graph");
    var open = shown.filter(function (i) { return i.state === "OPEN" || i.blocking.length; });
    if (!open.length) { box.textContent = "No open issues."; return; }
    var lines = ["flowchart LR"];
    var used = {};
    open.forEach(function (i) {
      if (i.state === "OPEN") { used[i.number] = true; }
      i.blockedBy.forEach(function (d) { used[d.number] = true; used[i.number] = true;
        lines.push("  n" + d.number + " --> n" + i.number); });
    });
    var any = false;
    Object.keys(used).forEach(function (k) {
      var i = byNumber[k]; if (!i) return; any = true;
      var t = i.title.length > 32 ? i.title.slice(0, 29) + "..." : i.title;
      t = t.replace(/["\[\]()<>]/g, "'");
      lines.push("  n" + k + '["#' + k + " " + t + '"]');
      var cls = i.state === "CLOSED" ? "closed" : (i.labels.some(function (l) { return l.name === IN_PROGRESS; }) ? "progress" : (isBlocked(i, byNumber) ? "blocked" : "ready"));
      lines.push("  class n" + k + " " + cls);
    });
    if (!any) { box.textContent = "No open issues."; return; }
    lines.push("  classDef ready stroke:#1a7f37,stroke-width:2px");
    lines.push("  classDef blocked stroke:#bc4c00,stroke-width:2px");
    lines.push("  classDef progress stroke:#0969da,stroke-width:2px");
    lines.push("  classDef closed stroke:#8250df,stroke-dasharray:4");
    var src = lines.join("\n");
    if (window.mermaid) {
      box.innerHTML = "";
      var pre = document.createElement("pre"); pre.className = "mermaid"; pre.textContent = src; box.appendChild(pre);
      var fallback = function () { box.innerHTML = "<pre>" + esc(src) + "</pre>"; };
      try {
        var p = window.mermaid.run({ nodes: [pre] });
        if (p && p.catch) { p.catch(fallback); }
      } catch (e) { fallback(); }
    } else {
      box.innerHTML = "<pre>" + esc(src) + "</pre>";
    }
  }
  document.getElementById("graphbox").addEventListener("toggle", function () {
    if (this.open && window.__lastShown) { renderGraph(window.__lastShown.shown, window.__lastShown.byNumber); }
  });
  function highlightHash() {
    var id = location.hash.replace("#", "");
    Array.prototype.forEach.call(document.querySelectorAll(".card.hilite"), function (c) { c.classList.remove("hilite"); });
    if (!id) return;
    var el = document.getElementById(id);
    if (el) {
      el.classList.add("hilite"); el.scrollIntoView({ block: "center" });
      var more = el.querySelector("details.more"); if (more) more.open = true;
      clearTimeout(window.__hiliteTimer);
      window.__hiliteTimer = setTimeout(clearHilite, 6000);
    }
  }
  function clearHilite() {
    Array.prototype.forEach.call(document.querySelectorAll(".card.hilite"), function (c) { c.classList.remove("hilite"); });
    if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  }
  window.addEventListener("hashchange", highlightHash);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") clearHilite(); });
  document.addEventListener("click", function (e) { if (!e.target.closest("a[href^='#issue-']")) clearHilite(); });

  function poll() {
    fetch(data.baseUrl + "/" + encodeURIComponent(data.repo) + "/events?since=" + data.seq, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (events) {
        if (!events.length) return;
        return fetch(data.baseUrl + "/" + encodeURIComponent(data.repo) + "/board?format=json", { headers: { Accept: "application/json" } })
          .then(function (r) { return r.json(); })
          .then(function (fresh) { data = fresh; render(); });
      })
      .catch(function () {})
      .then(function () { setTimeout(poll, 3000); });
  }
  function load(src, onload) {
    var script = document.createElement("script");
    script.src = src;
    script.onload = function () { onload(); render(); };
    script.onerror = function () { render(); };
    document.head.appendChild(script);
  }
  load("__MARKED__", setupMarked);
  load("__MERMAID__", applyTheme);
  render();
  if (LIVE) setTimeout(poll, 3000);
})();
</script>
</body>
</html>
"""
