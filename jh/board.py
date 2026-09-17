"""Board HTML for jh.

One row per milestone (plus "No milestone"), columns Draft / Blocked /
Ready / In progress / Closed (the path a card takes, left to right), a label filter, a dependency list on every
card and a mermaid dependency graph (every open issue is a node, with or
without dependencies). Issue bodies and comments are GitHub-flavoured
markdown, rendered in the browser with marked (raw HTML in them is shown
escaped, `#N` links to the card for issue N, mermaid fences are drawn, and
when the repo has a docs root a document path such as `docs/plan.md` or
`plan.md#Heading` links into the docs viewer, the heading slugged the way
the viewer slugs it). The
server renders it live at `/:repo/board` (polling `/:repo/events?since=SEQ`);
`jh board --snapshot FILE` writes the same page with the data inlined and
polling disabled. The theme starts from the browser's preference; the
sun/moon in the header flips it, remembered per browser. Both marked and
mermaid come from cdnjs; without them the
page still renders, with bodies as plain text and the graph as source.

Each card's title links to the issue's reading page, `/:repo/issues/N` in a
browser: the same title, labels, milestone, blockers, body and comments at
full width, rendered by the same client-side code (`render_issue`). There
`#N` links to issue N's own reading page. The page also carries the issue's
title and body revisions (`Store.issue_history`); `#diff=A..B` in the URL
shows revision B with the words changed since revision A marked, insertions
highlighted and deletions struck through, computed in the browser
(`DIFF_JS`). `#diff=A..A` shows revision A as it was.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from jh import docs
from jh.jh_lib import DRAFT_LABEL, GLOSSARY_LABEL, IN_PROGRESS_LABEL, Store

MARKED_CDN = "https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.12/marked.min.js"
MERMAID_CDN = "https://cdnjs.cloudflare.com/ajax/libs/mermaid/11.12.0/mermaid.min.js"
HLJS_CDN = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/highlight.min.js"
HLJS_LIGHT_CSS = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/github.min.css"
HLJS_DARK_CSS = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/github-dark.min.css"


_GLOSSARY_ENTRY = re.compile(
    r"^\*\*(?P<term>[^*]+?)\.\*\*\s+(?P<definition>.+?)(?:\s+\(#(?P<issue>\d+)\))?\s*$"
)


def glossary(store: Store, repo: str) -> dict[str, Any] | None:
    """The repo's glossary as data, parsed from the open issue labelled `glossary`.

    An entry is one line of the form `**Term.** One sentence. (#N)`, the
    trailing issue reference optional. The lowest-numbered open issue with
    the label is the glossary; other lines of its body are ignored.

    Args:
        store: The store to read from.
        repo: Repo name.

    Returns:
        `{"source": <issue number>, "entries": [{"term", "definition",
        "issue"}, ...]}` in body order, or None when no issue carries the label.
    """
    state = store.repo(repo)
    sources = [
        i
        for i in state.live_issues()
        if i["state"] == "OPEN" and GLOSSARY_LABEL in i["labels"]
    ]
    if not sources:
        return None
    source = min(sources, key=lambda i: i["number"])
    entries = []
    for line in (source.get("body") or "").splitlines():
        m = _GLOSSARY_ENTRY.match(line.strip())
        if m:
            entries.append(
                {
                    "term": m["term"],
                    "definition": m["definition"],
                    "issue": int(m["issue"]) if m["issue"] else None,
                }
            )
    return {"source": source["number"], "entries": entries}


def board_data(
    store: Store, repo: str, docs_root: Path | None = None
) -> dict[str, Any]:
    """Collect everything the board needs as plain JSON.

    Args:
        store: The store to read from.
        repo: Repo name.
        docs_root: The directory served at `/REPO/docs`, when one is
            configured. The board then turns document paths in issue text
            (`docs/plan.md`, `plan.md#Heading`) into links to the docs viewer.

    Returns:
        The payload the board template renders; `docs` is null without a
        docs root, else `{"base": <root dir name>, "files": [<paths>]}`.
    """
    state = store.repo(repo)
    return {
        "repo": repo,
        "baseUrl": store.base_url,
        "seq": state.seq,
        "issues": store.issue_list(repo, {"state": "all", "limit": 100000}),
        "labels": store.label_list(repo, {"limit": 100000}),
        "milestones": store.milestone_list(repo, {"state": "all"}),
        "glossary": glossary(store, repo),
        "docs": (
            {"base": docs_root.name, "files": [f["path"] for f in docs.tree(docs_root)]}
            if docs_root
            else None
        ),
    }


def render_board(
    store: Store, repo: str, live: bool = True, docs_root: Path | None = None
) -> str:
    """Render the board page.

    Args:
        store: The store to read from.
        repo: Repo name.
        live: When true the page polls the server for new events and reloads;
            when false it is a self-contained snapshot.
        docs_root: See `board_data`.

    Returns:
        A complete HTML document.
    """
    return render_from_data(board_data(store, repo, docs_root), live=live)


def render_from_data(data: dict[str, Any], live: bool = False) -> str:
    """Render the board page from a `board_data` payload (used for snapshots)."""
    repo = data["repo"]
    payload = json.dumps(data).replace("</", "<\\/")
    title = f"{repo} board"
    if not live:
        title += " (snapshot)"
    return (
        _fill(_TEMPLATE)
        .replace("__TITLE__", html.escape(title))
        .replace("__DATA__", payload)
        .replace("__LIVE__", "true" if live else "false")
        .replace("__INPROGRESS__", IN_PROGRESS_LABEL)
        .replace("__DRAFT__", DRAFT_LABEL)
    )


def issue_data(
    store: Store, repo: str, number: int, docs_root: Path | None = None
) -> dict[str, Any]:
    """The payload the reading page renders: the issue, its revisions, and what `docHref` needs."""
    return {
        "repo": repo,
        "baseUrl": store.base_url,
        "issue": store.issue_get(repo, number),
        "history": store.issue_history(repo, number),
        "glossary": glossary(store, repo),
        "docs": (
            {"base": docs_root.name, "files": [f["path"] for f in docs.tree(docs_root)]}
            if docs_root
            else None
        ),
    }


def render_issue(
    store: Store, repo: str, number: int, docs_root: Path | None = None
) -> str:
    """Render one issue's reading page: `/:repo/issues/N` in a browser.

    Title, labels, milestone, blockers, body and comments at full width,
    rendered client-side exactly as the board renders a card, with the
    revision history for the `#diff=A..B` view.
    """
    data = issue_data(store, repo, number, docs_root)
    payload = json.dumps(data).replace("</", "<\\/")
    title = f"#{number} {data['issue']['title']} · {repo}"
    return (
        _fill(_ISSUE_TEMPLATE)
        .replace("__TITLE__", html.escape(title))
        .replace("__DATA__", payload)
    )


# Pieces shared by the board and the issue reading page. `THEME_CSS` is the
# palette (light, dark, and the reader's override), `THEME_BTN_CSS` the
# sun/moon button, `BODY_CSS` how rendered markdown looks. `THEME_JS` expects
# a `#theme` button and calls `onThemeChange()` after a flip; `MARKDOWN_JS`
# expects `data` (`repo`, `baseUrl`, `docs`) and `issueHref(n)` in scope and
# defines `esc`, `slug`, `docHref`, `setupMarked`, `md`, `drawMermaid`,
# `setupHljs` and `highlightCode`.
THEME_CSS = r""":root {
  color-scheme: light dark;
  --bg: #fafafa; --fg: #1f2328; --muted: #656d76; --line: #d0d7de; --card: #ffffff;
  --ready: #1a7f37; --blocked: #bc4c00; --progress: #0969da; --closed: #8250df; --draft: #6e7781;
  --hilite: #fff8c5; --code: #f0f2f4; --panel: #f0f2f5; --chip: #e9ecf0;
  --tint-draft: #eef0f2; --tint-ready: #e6f3ea; --tint-blocked: #f8ece2; --tint-progress: #e4eef9; --tint-closed: #eee9f8;
  --shadow: 0 1px 2px rgba(31, 35, 40, .08);
  --ins: #dafbe1; --del: #ffebe9;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --line: #30363d; --card: #161b22;
    --ready: #3fb950; --blocked: #f0883e; --progress: #58a6ff; --closed: #a371f7; --draft: #8b949e;
    --hilite: #3b3419; --code: #21262d; --panel: #161b22; --chip: #1c2128;
    --tint-draft: #181b20; --tint-ready: #0f1f16; --tint-blocked: #221709; --tint-progress: #0e1a2e; --tint-closed: #181427;
    --shadow: 0 1px 0 rgba(255, 255, 255, .04) inset, 0 6px 16px rgba(0, 0, 0, .35);
    --ins: #12351f; --del: #4a1a1f;
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
  --ins: #12351f; --del: #4a1a1f;
}"""

THEME_BTN_CSS = r"""#theme { margin-left: auto; border: 0; background: none; color: var(--muted); padding: 4px; cursor: pointer;
  display: inline-flex; align-items: center; border-radius: 6px; opacity: .7; }
#theme:hover { opacity: 1; background: var(--chip); }
#theme svg { width: 16px; height: 16px; stroke: currentColor; fill: none; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
#theme .sun { display: none; } :root[data-theme="dark"] #theme .sun { display: block; } :root[data-theme="dark"] #theme .moon { display: none; }"""

BODY_CSS = r""".body { margin-top: 6px; font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }
.body.plain { white-space: pre-wrap; }
.body > :first-child { margin-top: 0; } .body > :last-child { margin-bottom: 0; }
.body p, .body ul, .body ol, .body pre, .body table, .body blockquote { margin: 6px 0; }
.body h1, .body h2, .body h3, .body h4 { font-size: 13px; margin: 10px 0 4px; }
.body h1 { font-size: 14px; }
.body ul, .body ol { padding-left: 22px; }
.body li + li { margin-top: 2px; }
.body code { font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: var(--code); border-radius: 4px; padding: 1px 4px; }
.body pre { background: var(--code); border-radius: 6px; padding: 8px 10px; overflow-x: auto; }
.body pre code { background: none; padding: 0; }
.body pre.mermaid { background: none; text-align: center; }
.body abbr.gloss { text-decoration: none; border-bottom: 1px dotted var(--muted); position: relative; cursor: default;
  padding: 3px 0; margin: -3px 0; }
.body abbr.gloss:hover { border-bottom-color: var(--fg); }
#gloss-tip { display: none; position: fixed; z-index: 50; width: max-content; max-width: min(380px, 80vw); padding: 6px 9px;
  border-radius: 6px; background: var(--panel); color: var(--fg); border: 1px solid var(--line); box-shadow: var(--shadow);
  font: 12px/1.4 system-ui, sans-serif; white-space: normal; text-align: left; pointer-events: none; }
.body blockquote { border-left: 3px solid var(--line); color: var(--muted); padding: 0 10px; }
.body table { border-collapse: collapse; display: block; max-width: 100%; overflow-x: auto; }
.body th, .body td { border: 1px solid var(--line); padding: 3px 8px; text-align: left; }
.body th { background: var(--code); }
.body img { max-width: 100%; }
.body a { color: var(--progress); }
.body a.doc { text-decoration: underline dotted; }
.body hr { border: 0; border-top: 1px solid var(--line); }
.body input[type=checkbox] { margin: 0 4px 0 0; vertical-align: middle; }"""

THEME_JS = r"""  // Light or dark: the browser's preference until the reader clicks the
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
    var light = document.getElementById("hljs-light"), dark = document.getElementById("hljs-dark");
    if (light && dark) { light.disabled = isDark(); dark.disabled = !isDark(); }
  }
  applyTheme();
  document.getElementById("theme").onclick = function () {
    theme = isDark() ? "light" : "dark";
    try { localStorage.setItem(themeKey, theme); } catch (e) {}
    applyTheme(); onThemeChange();
  };"""

MARKDOWN_JS = r"""  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); };

  // Bodies and comments are GitHub-flavoured markdown. Raw HTML inside them
  // is escaped rather than trusted, "#12" becomes a link to that card, and
  // mermaid fences are drawn once the card is on the page. Until marked has
  // loaded (or if it never does) the text is shown escaped, whitespace kept.
  var markedReady = false;
  function slug(text) {
    return String(text).toLowerCase().replace(/[^\w\s-]/g, "").trim().replace(/\s+/g, "-");
  }
  // "docs/increments/01.md#Heading" -> the docs viewer URL for that file,
  // or null. The path may be relative to the docs root, carry the root's
  // directory name (or the repo name before it), or be a bare file name
  // that occurs once in the tree. The fragment is slugged like the viewer
  // slugs its heading ids.
  function docHref(ref) {
    if (!data.docs || !data.docs.files.length) return null;
    var m = /^([^#]*)(?:#(.*))?$/.exec(ref);
    var path = m[1], frag = m[2];
    var files = data.docs.files;
    var hit = null;
    // The root's own directory name, or "docs": text written for a docs
    // tree that has since moved keeps resolving.
    [data.docs.base, "docs"].forEach(function (base) {
      if (hit) return;
      var i = path.indexOf(base + "/");
      if (i === 0 || (i > 0 && path.charAt(i - 1) === "/")) {
        var rest = path.slice(i + base.length + 1);
        if (files.indexOf(rest) >= 0) hit = rest;
      }
    });
    if (!hit && files.indexOf(path) >= 0) hit = path;
    if (!hit) {
      var tail = "/" + path;
      var cands = files.filter(function (f) { return f.slice(-tail.length) === tail; });
      if (cands.length === 1) hit = cands[0];
    }
    if (!hit) return null;
    var url = data.baseUrl + "/" + encodeURIComponent(data.repo) + "/docs/" + hit;
    if (frag) { try { frag = decodeURIComponent(frag); } catch (e) {} url += "#" + slug(frag); }
    return url;
  }
  var PATH = /^((?:[\w.-]+\/)*[\w.-]+\.md)(#[\w.%-]+)?/;
  function setupMarked() {
    if (!window.marked) return;
    var docref = { name: "docref", level: "inline",
      start: function (src) {
        var m = /(^|[\s(\[`"'<])((?:[\w.-]+\/)*[\w.-]+\.md)/.exec(src);
        return m ? m.index + m[1].length : undefined;
      },
      tokenizer: function (src) {
        var m = PATH.exec(src);
        if (!m) return;
        var href = docHref(m[1] + (m[2] || ""));
        if (href) return { type: "docref", raw: m[0], text: m[0], href: href };
      },
      renderer: function (t) {
        return '<a class="doc" href="' + esc(t.href) + '" target="_blank" rel="noopener">' + esc(t.text) + '</a>';
      } };
    var link = { name: "issueref", level: "inline",
      start: function (src) { var m = /(^|[^\w&])#\d/.exec(src); return m ? m.index + m[1].length : undefined; },
      tokenizer: function (src) {
        var m = /^#(\d+)(?![\w#])/.exec(src);
        if (m) return { type: "issueref", raw: m[0], number: m[1] };
      },
      renderer: function (t) { return '<a href="' + esc(issueHref(t.number)) + '">#' + t.number + '</a>'; } };
    window.marked.use({ gfm: true, breaks: false, extensions: [link, docref],
      renderer: {
        html: function (t) { return esc(t.raw || t.text || t); },
        // [text](docs/plan.md#Heading): resolve a relative .md href the same way.
        link: function (t) {
          var href = t.href, cls = "";
          if (!/^[a-z][a-z0-9+.-]*:/i.test(href) && href.charAt(0) !== "#" && PATH.test(href)) {
            var r = docHref(href); if (r) { href = r; cls = ' class="doc" target="_blank" rel="noopener"'; }
          }
          return '<a href="' + esc(href) + '"' + (t.title ? ' title="' + esc(t.title) + '"' : "") + cls + '>' +
            this.parser.parseInline(t.tokens) + '</a>';
        } } });
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
      if (c.parentNode.classList.contains("changed")) m.classList.add("changed");
      c.parentNode.replaceWith(m); nodes.push(m);
    });
    try { var p = window.mermaid.run({ nodes: nodes }); if (p && p.catch) p.catch(function () {}); } catch (e) {}
  }
  // Code fences with a language get highlight.js colours; a fence with no
  // language, such as command output, is left alone rather than guessed at.
  // Starlark is close enough to Python for its grammar.
  function setupHljs() {
    if (!window.hljs) return;
    try {
      window.hljs.configure({ ignoreUnescapedHTML: true });
      window.hljs.registerAliases(["starlark", "bzl", "bazel"], { languageName: "python" });
    } catch (e) {}
  }
  function highlightCode(root) {
    if (!window.hljs) return;
    var fences = root.querySelectorAll('.body pre > code[class*="language-"]');
    Array.prototype.forEach.call(fences, function (c) {
      if (c.classList.contains("language-mermaid") || c.classList.contains("hljs")) return;
      try { window.hljs.highlightElement(c); } catch (e) {}
    });
  }
  // Glossary terms in rendered prose get a dotted underline and the
  // definition as a tooltip: the first occurrence in each issue, counting
  // its body and comments as one document, longest term first, whole words,
  // plural "s" allowed, never inside code, links or headings, and never on
  // the glossary issue itself. `number` is the issue a body belongs to; on
  // the board it is read from the card.
  var glossRe = null, glossDefs = null;
  function setupGlossary() {
    var g = data.glossary;
    if (!g || !g.entries.length) return;
    glossDefs = {};
    var terms = g.entries.map(function (e) { glossDefs[e.term.toLowerCase()] = e.definition; return e.term; });
    terms.sort(function (a, b) { return b.length - a.length; });
    var alt = terms.map(function (t) { return t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }).join("|");
    try { glossRe = new RegExp("(?<![\\w.-])(" + alt + ")(s?)(?![\\w-])", "i"); } catch (e) { glossRe = null; }
    // One tooltip element for the whole page, fixed to the viewport, so a
    // term inside a scrolling table (or any clipped box) is not cut off. It
    // hangs below the term from its left edge, shifts left when it would run
    // off the right of the window, and goes above the term when it would run
    // off the bottom.
    var tip = document.createElement("div"); tip.id = "gloss-tip"; document.body.appendChild(tip);
    document.addEventListener("mouseover", function (ev) {
      var a = ev.target && ev.target.closest && ev.target.closest("abbr.gloss");
      if (!a) return;
      tip.textContent = a.getAttribute("data-def"); tip.style.display = "block";
      var r = a.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
      var left = Math.max(8, Math.min(r.left, window.innerWidth - 8 - w));
      var top = r.bottom + 4; if (top + h > window.innerHeight - 8) top = r.top - 4 - h;
      tip.style.left = left + "px"; tip.style.top = top + "px";
    });
    document.addEventListener("mouseout", function (ev) {
      var a = ev.target && ev.target.closest && ev.target.closest("abbr.gloss");
      if (a && !(ev.relatedTarget && a.contains(ev.relatedTarget))) tip.style.display = "none";
    });
  }
  var GLOSS_SKIP = { CODE: 1, PRE: 1, A: 1, ABBR: 1, H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1 };
  function applyGlossary(root, number) {
    if (!glossRe || !root) return;
    var seenByIssue = {};
    Array.prototype.forEach.call(root.querySelectorAll(".body"), function (body) {
      var card = body.closest(".card");
      var n = number != null ? number : (card ? parseInt(card.id.replace("issue-", ""), 10) : null);
      if (n === data.glossary.source) return;
      var seen = seenByIssue[n] || (seenByIssue[n] = {});
      var walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
        acceptNode: function (t) {
          for (var e = t.parentNode; e && e !== body; e = e.parentNode) if (GLOSS_SKIP[e.nodeName]) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        } });
      var nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(function (t) {
        var text = t.nodeValue, m, offset = 0, node = t;
        while ((m = glossRe.exec(node.nodeValue))) {
          var key = m[1].toLowerCase();
          if (seen[key]) { offset = m.index + m[0].length; node = splitAt(node, offset); if (!node) break; continue; }
          seen[key] = true;
          var after = node.splitText(m.index + m[0].length);
          var hit = node.splitText(m.index);
          var abbr = document.createElement("abbr"); abbr.className = "gloss";
          abbr.setAttribute("data-def", glossDefs[key].replace(/`/g, ""));
          hit.parentNode.replaceChild(abbr, hit); abbr.appendChild(hit);
          node = after;
        }
      });
    });
  }
  function splitAt(node, i) { return i < node.nodeValue.length ? node.splitText(i) : null; }"""


# Word-level diff for the reading page. `markup(a, b)` returns `b`'s
# markdown with the words changed since `a` wrapped in private-use
# sentinel characters (so marked still sees plain markdown); after
# rendering, `applyMarks(root)` turns the sentinels into <ins>/<del>
# around the affected text nodes, however the markup split them. A mermaid
# fence with changes inside is flagged `changed` as a whole instead.
DIFF_JS = r"""  var INS0 = "", INS1 = "", DEL0 = "", DEL1 = "";
  var SENTINEL = /[-]/;
  function words(text) { return text.match(/\s+|\S+/g) || []; }
  function lines(text) { return text.match(/[^\n]*\n|[^\n]+$/g) || []; }
  // Longest-common-subsequence alignment of two token arrays: [op, token]
  // with op " " (same), "-" (only in a) or "+" (only in b).
  function align(a, b) {
    var n = a.length, m = b.length, s = 0, e = 0, out = [], i, j;
    while (s < n && s < m && a[s] === b[s]) s++;
    while (e < n - s && e < m - s && a[n - 1 - e] === b[m - 1 - e]) e++;
    for (i = 0; i < s; i++) out.push([" ", a[i]]);
    var A = a.slice(s, n - e), B = b.slice(s, m - e), N = A.length, M = B.length, W = M + 1;
    if (N && M) {
      var L = new Uint32Array((N + 1) * W);
      for (i = N - 1; i >= 0; i--) for (j = M - 1; j >= 0; j--) {
        L[i * W + j] = A[i] === B[j] ? L[(i + 1) * W + j + 1] + 1 : Math.max(L[(i + 1) * W + j], L[i * W + j + 1]);
      }
      i = 0; j = 0;
      while (i < N && j < M) {
        if (A[i] === B[j]) { out.push([" ", A[i]]); i++; j++; }
        else if (L[(i + 1) * W + j] >= L[i * W + j + 1]) { out.push(["-", A[i]]); i++; }
        else { out.push(["+", B[j]]); j++; }
      }
      while (i < N) out.push(["-", A[i++]]);
      while (j < M) out.push(["+", B[j++]]);
    } else {
      for (i = 0; i < N; i++) out.push(["-", A[i]]);
      for (j = 0; j < M; j++) out.push(["+", B[j]]);
    }
    for (i = n - e; i < n; i++) out.push([" ", a[i]]);
    return out;
  }
  // Block syntax a marker must not sit in front of: headings, list items,
  // quotes, table rows, fences. The marker goes after it instead.
  var PREFIX = /^[ \t]*(?:```[^\n]*|~~~[^\n]*|#{1,6}|>|[-*+]|\d+[.)]|\|)[ \t]*/;
  function wrap(text, open, close, lineStart) {
    var lead = /^\s*/.exec(text)[0], tail = /\s*$/.exec(text)[0];
    if (lead.length === text.length) return text;
    var core = text.slice(lead.length, text.length - tail.length);
    if (lineStart || lead.indexOf("\n") >= 0) {
      var m = PREFIX.exec(core);
      if (m && m[0]) { lead += m[0]; core = core.slice(m[0].length); if (!core) return text; }
    }
    var t = /\|[ \t]*$/.exec(core);
    if (t && t[0].length < core.length) { core = core.slice(0, -t[0].length); tail = t[0] + tail; }
    return lead + open + core + close + tail;
  }
  var LIMIT = 4e6;
  function markup(a, b) {
    if (a === b) return b;
    var ta = words(a), tb = words(b);
    if ((ta.length + 1) * (tb.length + 1) > LIMIT) { ta = lines(a); tb = lines(b); }
    var ops = (ta.length + 1) * (tb.length + 1) > LIMIT ? [["-", a], ["+", b]] : align(ta, tb);
    var out = "", i = 0;
    while (i < ops.length) {
      if (ops[i][0] === " ") { out += ops[i][1]; i++; continue; }
      var del = "", ins = "";
      while (i < ops.length && ops[i][0] !== " ") { if (ops[i][0] === "-") del += ops[i][1]; else ins += ops[i][1]; i++; }
      var lineStart = !out || out.slice(-1) === "\n";
      out += wrap(del, DEL0, DEL1, lineStart) + wrap(ins, INS0, INS1, lineStart);
    }
    return out;
  }
  function applyMarks(root) {
    var mode = null, walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(function (node) {
      var text = node.nodeValue;
      if (!mode && !SENTINEL.test(text)) return;
      var code = node.parentNode.closest("code.language-mermaid");
      var frag = document.createDocumentFragment();
      text.split(/([-])/).forEach(function (part) {
        if (part === INS0) mode = "ins";
        else if (part === DEL0) mode = "del";
        else if (part === INS1 || part === DEL1) mode = null;
        else if (part) {
          if (code) {
            // A diagram is drawn from its current source, flagged as a whole.
            if (mode) code.parentNode.classList.add("changed");
            if (mode !== "del") frag.appendChild(document.createTextNode(part));
          }
          else if (mode && part.trim()) { var el = document.createElement(mode); el.textContent = part; frag.appendChild(el); }
          else frag.appendChild(document.createTextNode(part));
        }
      });
      node.parentNode.replaceChild(frag, node);
    });
  }
  function marksToHtml(text) {
    return esc(text).split(INS0).join("<ins>").split(INS1).join("</ins>").split(DEL0).join("<del>").split(DEL1).join("</del>");
  }"""


def _fill(template: str) -> str:
    """Splice the shared CSS and JS pieces into a page template."""
    return (
        template.replace("__THEME_CSS__", THEME_CSS)
        .replace("__THEME_BTN_CSS__", THEME_BTN_CSS)
        .replace("__BODY_CSS__", BODY_CSS)
        .replace("__THEME_JS__", THEME_JS)
        .replace("__MARKDOWN_JS__", MARKDOWN_JS)
        .replace("__DIFF_JS__", DIFF_JS)
        .replace("__MARKED__", MARKED_CDN)
        .replace("__MERMAID__", MERMAID_CDN)
        .replace("__HLJS__", HLJS_CDN)
        .replace("__HLJS_LIGHT__", HLJS_LIGHT_CSS)
        .replace("__HLJS_DARK__", HLJS_DARK_CSS)
    )


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" id="hljs-light" href="__HLJS_LIGHT__">
<link rel="stylesheet" id="hljs-dark" href="__HLJS_DARK__" disabled>
<style>
__THEME_CSS__
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
__THEME_BTN_CSS__
.card { background: var(--card); border-radius: 10px; padding: 12px; box-shadow: var(--shadow); display: flex; flex-direction: column; gap: 8px; }
.card.hilite { background: var(--hilite); }
.col.closed .card { opacity: .6; } .col.closed .card:hover, .col.closed .card.hilite { opacity: 1; }
.card .num { font-weight: 800; font-variant-numeric: tabular-nums; }
.col.ready .num { color: var(--ready); } .col.blocked .num { color: var(--blocked); } .col.draft .num { color: var(--draft); }
.col.progress .num { color: var(--progress); } .col.closed .num { color: var(--closed); }
.card .title { font-weight: 700; font-size: 14px; line-height: 1.35; color: inherit; text-decoration: none; }
.card .title:hover { text-decoration: underline; }
.card .labels { display: flex; flex-wrap: wrap; gap: 4px; }
.card .foot { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.card .foot .deps { display: flex; gap: 10px; flex-wrap: wrap; margin: 0; }
.lbl { font-size: 11px; font-weight: 700; border-radius: 4px; padding: 1px 6px; }
.card details.more summary { cursor: pointer; list-style: none; }
.card details.more summary::-webkit-details-marker { display: none; }
.card details.more[open] summary .title { text-decoration: underline dotted; }
.card .meta { font-size: 12px; color: var(--muted); }
__BODY_CSS__
.card .body th, .card .body td { white-space: nowrap; }
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
__THEME_JS__
  function onThemeChange() { render(); }
  var collapsedKey = "jh-board-collapsed-" + data.repo;
  var collapsed = {};
  try { collapsed = JSON.parse(localStorage.getItem(collapsedKey) || "{}") || {}; } catch (e) { collapsed = {}; }
__MARKDOWN_JS__
  function issueHref(n) { return "#issue-" + n; }
  function issuePage(n) { return data.baseUrl + "/" + encodeURIComponent(data.repo) + "/issues/" + n; }
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
      '<a class="title" href="' + esc(issuePage(issue.number)) + '">' + esc(issue.title) + '</a>' +
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
    highlightCode(document.getElementById("board"));
    applyGlossary(document.getElementById("board"));
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
  setupGlossary();
  load("__MERMAID__", applyTheme);
  load("__HLJS__", setupHljs);
  render();
  if (LIVE) setTimeout(poll, 3000);
})();
</script>
</body>
</html>
"""


_ISSUE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" id="hljs-light" href="__HLJS_LIGHT__">
<link rel="stylesheet" id="hljs-dark" href="__HLJS_DARK__" disabled>
<style>
__THEME_CSS__
* { box-sizing: border-box; }
body { margin: 0; padding: 16px 24px 64px; background: var(--bg); color: var(--fg);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
header { display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: baseline; margin-bottom: 14px; }
header .crumbs { color: var(--muted); font-size: 13px; font-weight: 600; }
header .crumbs a { color: var(--muted); text-decoration: none; }
header .crumbs a:hover { color: var(--fg); }
header .state { font-size: 12px; font-weight: 700; border-radius: 999px; padding: 2px 10px; color: #ffffff; background: var(--ready); }
header .state.closed { background: var(--closed); }
__THEME_BTN_CSS__
h1 { font-size: 26px; font-weight: 800; letter-spacing: -.02em; line-height: 1.25; margin: 0 0 10px; }
h1 .num { color: var(--muted); font-weight: 600; margin-right: 8px; font-variant-numeric: tabular-nums; }
.facts { display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: center; font-size: 13px; color: var(--muted); margin-bottom: 18px; }
.facts .labels { display: inline-flex; flex-wrap: wrap; gap: 4px; }
.facts a { color: var(--progress); text-decoration: none; }
.facts a:hover { text-decoration: underline; }
.lbl { font-size: 11px; font-weight: 700; border-radius: 4px; padding: 1px 6px; }
.deps .open { color: var(--blocked); } .deps .closed { text-decoration: line-through; }
.deps a { color: inherit; }
section.body-box { background: var(--card); border-radius: 12px; padding: 18px 22px; box-shadow: var(--shadow); }
__BODY_CSS__
.body { margin-top: 0; font-size: 15px; line-height: 1.55; }
.body h1, .body h2, .body h3, .body h4 { font-size: 15px; margin: 18px 0 6px; font-weight: 800; }
.body h1 { font-size: 20px; } .body h2 { font-size: 18px; } .body h3 { font-size: 16px; }
.body p, .body ul, .body ol, .body pre, .body table, .body blockquote { margin: 10px 0; }
.body code { font-size: 13.5px; }
.body.empty { color: var(--muted); font-style: italic; }
.comments { margin-top: 22px; }
.comments h2 { font-size: 13px; font-weight: 800; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 10px; }
.comment { background: var(--card); border-radius: 12px; padding: 14px 22px; box-shadow: var(--shadow); margin-bottom: 10px; }
.comment .who { font-weight: 700; font-size: 13px; }
.comment .when { font-size: 12px; color: var(--muted); margin-left: 6px; }
.comment .body { margin-top: 6px; }
.comment:target { outline: 2px solid var(--progress); }
.linkish { border: 0; background: none; padding: 0; font: inherit; color: var(--progress); cursor: pointer; }
.linkish:hover { text-decoration: underline; }
.revs { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; font-size: 13px; color: var(--muted);
  background: var(--panel); border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; }
.revs select { font: inherit; color: var(--fg); background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; }
.revs .who { font-weight: 600; color: var(--fg); }
ins { background: var(--ins); text-decoration: none; border-radius: 2px; }
del { background: var(--del); text-decoration: line-through; border-radius: 2px; }
h1 ins, h1 del { padding: 0 3px; }
.body pre.changed { outline: 2px dashed var(--progress); outline-offset: 3px; }
.body pre.mermaid.changed::after { content: "diagram changed"; display: block; font-size: 12px; color: var(--progress); margin-top: 4px; }
</style>
</head>
<body>
<header>
  <span class="crumbs" id="crumbs"></span>
  <span class="state" id="state"></span>
  <button id="theme" title="Switch theme" aria-label="Switch theme">
    <svg class="moon" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
    <svg class="sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
  </button>
</header>
<main id="issue"></main>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById("data").textContent);
  var issue = data.issue;
  var history = data.history || [];
  var repoPath = data.baseUrl + "/" + encodeURIComponent(data.repo);
__THEME_JS__
  function onThemeChange() { render(); }
  function issueHref(n) { return repoPath + "/issues/" + n; }
__MARKDOWN_JS__
__DIFF_JS__
  // "#diff=A..B" selects the view: revision B with changes since A marked,
  // or revision A as it was when A equals B. Anything else is the plain view.
  function selection() {
    var m = /^#diff=(\d+)\.\.(\d+)$/.exec(location.hash);
    if (!m) return null;
    var a = parseInt(m[1], 10), b = parseInt(m[2], 10), K = history.length;
    if (a < 1 || b < 1 || a > K || b > K) return null;
    return { from: a, to: b };
  }
  function select(a, b) {
    var hash = a ? "#diff=" + a + ".." + b : "";
    history_replace(hash); render();
  }
  function history_replace(hash) { window.history.replaceState(null, "", location.pathname + location.search + hash); }
  function revLabel(r) { return "r" + r.rev + " · " + esc(r.actor) + " · " + when(r.ts); }
  function revOptions(chosen) {
    return history.map(function (r) {
      return '<option value="' + r.rev + '"' + (r.rev === chosen ? " selected" : "") + '>' + revLabel(r) + '</option>';
    }).join("");
  }
  function textColor(hex) {
    var n = parseInt(hex || "ededed", 16);
    var r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#1f2328" : "#ffffff";
  }
  function when(iso) { return iso ? iso.slice(0, 16).replace("T", " ") : ""; }
  function labelChip(l) {
    return '<span class="lbl" style="background:#' + esc(l.color) + ';color:' + textColor(l.color) +
      '" title="' + esc(l.description) + '">' + esc(l.name) + '</span>';
  }
  function refs(list, label) {
    if (!list.length) return "";
    return '<span class="deps">' + label + ' ' + list.map(function (d) {
      var cls = d.state === "OPEN" ? "open" : "closed";
      return '<a class="' + cls + '" href="' + esc(issueHref(d.number)) + '" title="' + esc(d.title) + '">#' + d.number + '</a>';
    }).join(", ") + '</span>';
  }
  function render() {
    document.getElementById("crumbs").innerHTML =
      '<a href="' + esc(repoPath + "/board") + '">' + esc(data.repo) + ' board</a> / ' +
      '<a href="' + esc(repoPath + "/board#issue-" + issue.number) + '">card</a>' +
      (data.docs ? ' / <a href="' + esc(repoPath + "/docs/") + '">docs</a>' : "") +
      ' / <a href="' + esc(repoPath + "/book") + '">book</a>';
    var st = document.getElementById("state");
    st.className = "state " + (issue.state === "CLOSED" ? "closed" : "open");
    st.textContent = issue.state === "CLOSED"
      ? "closed" + (issue.stateReason ? " · " + issue.stateReason.toLowerCase().replace("_", " ") : "") : "open";
    var facts = [];
    if (issue.labels.length) facts.push('<span class="labels">' + issue.labels.map(labelChip).join("") + '</span>');
    if (issue.milestone) facts.push('<span>milestone <a href="' + esc(repoPath + "/board") + '">' + esc(issue.milestone.title) + '</a></span>');
    if (issue.assignees.length) facts.push('<span>assigned ' + esc(issue.assignees.map(function (a) { return a.login; }).join(", ")) + '</span>');
    facts.push(refs(issue.blockedBy, "blocked by"));
    facts.push(refs(issue.blocking, "blocks"));
    facts.push('<span>opened by ' + esc(issue.author && issue.author.login || "?") + " " + when(issue.createdAt) +
      (issue.updatedAt !== issue.createdAt ? " · updated " + when(issue.updatedAt) : "") + '</span>');
    var K = history.length, sel = selection();
    if (K > 1) {
      facts.push('<button class="linkish" id="revs">' + (sel ? "plain view" : K + " revisions") + '</button>');
    }
    var title = esc(issue.title), bodyText = issue.body, panel = "", diff = false;
    if (sel) {
      var from = history[sel.from - 1], to = history[sel.to - 1];
      if (sel.from === sel.to) {
        title = esc(from.title); bodyText = from.body;
        panel = '<span>Revision <span class="who">r' + from.rev + '</span> of ' + K + ', as ' + esc(from.actor) + ' left it ' + when(from.ts) + '</span>';
      } else {
        diff = true;
        title = marksToHtml(markup(from.title, to.title));
        bodyText = markup(from.body, to.body);
        panel = '<span>Changes since</span>';
      }
      panel = '<div class="revs">' + panel +
        (sel.from !== sel.to ? '<select id="from">' + revOptions(sel.from) + '</select><span>up to</span>' : '<span>·</span><span>compare with</span>') +
        '<select id="to">' + revOptions(sel.to) + '</select>' +
        (sel.to === K ? '<span>(current)</span>' : '') +
        '<button class="linkish" id="plain">plain view</button></div>';
    }
    var body = bodyText ? md(bodyText) : '<div class="body empty">No description.</div>';
    var comments = issue.comments.length ? '<div class="comments"><h2>' + issue.comments.length +
      (issue.comments.length === 1 ? " comment" : " comments") + '</h2>' + issue.comments.map(function (c) {
        return '<div class="comment" id="issuecomment-' + c.databaseId + '"><span class="who">' + esc(c.author && c.author.login || "?") +
          '</span><span class="when">' + when(c.createdAt) + '</span>' + md(c.body) + '</div>';
      }).join("") + '</div>' : "";
    var root = document.getElementById("issue");
    root.innerHTML =
      '<h1><span class="num">#' + issue.number + '</span>' + title + '</h1>' +
      '<div class="facts">' + facts.filter(Boolean).join("") + '</div>' + panel +
      '<section class="body-box">' + body + '</section>' + comments;
    if (diff) applyMarks(root.querySelector(".body-box"));
    drawMermaid(root);
    highlightCode(root);
    applyGlossary(root, issue.number);
    var revs = document.getElementById("revs");
    if (revs) revs.onclick = function () { if (sel) select(null); else select(Math.max(1, K - 1), K); };
    var plain = document.getElementById("plain");
    if (plain) plain.onclick = function () { select(null); };
    var fromSel = document.getElementById("from"), toSel = document.getElementById("to");
    function pick() {
      var a = fromSel ? parseInt(fromSel.value, 10) : parseInt(toSel.value, 10), b = parseInt(toSel.value, 10);
      if (fromSel && a > b) { var t = a; a = b; b = t; }
      select(a, b);
    }
    if (fromSel) fromSel.onchange = pick;
    if (toSel) toSel.onchange = fromSel ? pick : function () {
      // Viewing one revision: choosing another compares it with the current one.
      var r = parseInt(toSel.value, 10); select(Math.min(r, sel.from), Math.max(r, sel.from));
    };
    if (location.hash) { var t = document.getElementById(location.hash.slice(1)); if (t) t.scrollIntoView(); }
  }
  function load(src, onload) {
    var script = document.createElement("script");
    script.src = src;
    script.onload = function () { onload(); render(); };
    script.onerror = function () { render(); };
    document.head.appendChild(script);
  }
  window.addEventListener("hashchange", render);
  load("__MARKED__", setupMarked);
  setupGlossary();
  load("__MERMAID__", applyTheme);
  load("__HLJS__", setupHljs);
  render();
})();
</script>
</body>
</html>
"""
