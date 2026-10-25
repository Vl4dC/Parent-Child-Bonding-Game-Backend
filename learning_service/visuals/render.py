"""
Guardrails and renderer for model-written visuals.

validate() rejects code that would break or misbehave, with a message the drawer
model can act on. document() wraps a visual in a standalone page meant for an
<iframe sandbox="allow-scripts" srcdoc=...>: opaque origin (no access to the app's
page or cookies) and a CSP with no network access except the pinned chart and
diagram libraries.
"""

from __future__ import annotations

import html
import json
import re

from learning_service.visuals import diagram_lint
from learning_service.visuals.models import Visual, VisualKind

LIBS = {
    "diagram": ["https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"],
    "chart": [
        "https://cdn.jsdelivr.net/npm/vega@5.30.0/build/vega.min.js",
        "https://cdn.jsdelivr.net/npm/vega-lite@5.21.0/build/vega-lite.min.js",
        "https://cdn.jsdelivr.net/npm/vega-embed@6.26.0/build/vega-embed.min.js",
    ],
    "interactive": [],
    "illustration": [],
}

# An illustration's code is a WebP data URL (~100-300 KB), produced by illustrate.py, not by a chat model
MAX_CHARS = {"diagram": 5_000, "chart": 30_000, "interactive": 40_000, "illustration": 1_500_000}
ILLUSTRATION_URL = re.compile(r"data:image/(webp|png|jpeg);base64,[A-Za-z0-9+/]+={0,2}")

MERMAID_TYPES = {
    "flowchart",
    "graph",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "stateDiagram-v2",
    "erDiagram",
    "timeline",
    "mindmap",
    "pie",
    "journey",
    "quadrantChart",
    "xychart-beta",
    "gantt",
}
MERMAID_BANNED = [
    (r"%%\{", "remove the %%{init}%% directive; the page sets the theme"),
    (r"^\s*click\s", "remove click handlers"),
    (r"<\s*script|javascript:", "no scripts or javascript: links in labels"),
]

VEGA_VIEW_KEYS = {"mark", "layer", "concat", "hconcat", "vconcat", "facet", "repeat"}

INTERACTIVE_BANNED = [
    (r"<\s*script[^>]*\bsrc\s*=", "no external scripts; put all JavaScript in one inline <script>"),
    (
        r"\b(src|href)\s*=\s*[\"']?\s*(https?:)?//",
        "no external images, fonts or links; draw with inline SVG",
    ),
    (r"@import|url\(\s*[\"']?\s*(https?:)?//", "no external CSS or fonts"),
    (
        r"\bfetch\s*\(|XMLHttpRequest|WebSocket|EventSource|navigator\.sendBeacon",
        "no network calls",
    ),
    (
        r"localStorage|sessionStorage|indexedDB|document\.cookie",
        "no storage; it throws inside the sandbox",
    ),
    (
        r"\b(alert|confirm|prompt)\s*\(",
        "no alert/confirm/prompt dialogs; show messages in the page",
    ),
    (r"<\s*iframe", "no iframes"),
    (
        r"<\s*(!doctype|html|head|body)\b",
        "return a fragment only, without <html>, <head> or <body>",
    ),
]


def clean(code: str) -> str:
    """Strip a ``` fence if the model wrapped its code in one."""
    code = code.strip()
    m = re.fullmatch(r"```[\w-]*\s*\n(.*?)\n?```", code, flags=re.S)
    return m.group(1).strip() if m else code


def _has_url(node) -> bool:
    if isinstance(node, dict):
        return "url" in node or any(_has_url(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_url(v) for v in node)
    return False


def validate(kind: VisualKind, code: str) -> str | None:
    """Return None if the code is acceptable, else a short reason to send back to the model."""
    if not code.strip():
        return "code is empty"
    if len(code) > MAX_CHARS[kind]:
        return f"code is {len(code)} characters; keep it under {MAX_CHARS[kind]}"

    if kind == "diagram":
        first = code.strip().splitlines()[0].split()[0]
        if first not in MERMAID_TYPES:
            return f"first line must start with a Mermaid diagram type (e.g. 'flowchart LR'), got '{first}'"
        for pattern, why in MERMAID_BANNED:
            if re.search(pattern, code, flags=re.I | re.M):
                return why
        return diagram_lint.syntax_problem(code)

    if kind == "chart":
        try:
            spec = json.loads(code)
        except json.JSONDecodeError as exc:
            return f"code must be valid JSON ({exc.msg} at line {exc.lineno})"
        if not isinstance(spec, dict) or not VEGA_VIEW_KEYS & spec.keys():
            return "code must be a Vega-Lite spec object with a 'mark' (or layer/concat/facet)"
        if _has_url(spec):
            return "data must be inline ('data': {'values': [...]}); remove every 'url'"
        for layer in spec.get("layer", [])[1:]:
            if any(
                isinstance(ch, dict) and "axis" in ch and ch["axis"] is None
                for ch in layer.get("encoding", {}).values()
            ):
                return (
                    'don\'t set "axis": null in later layers: layers share axes, so it hides the '
                    "first layer's axis too; leave axis out of later layers instead"
                )
        return None

    if kind == "illustration":
        if not ILLUSTRATION_URL.fullmatch(code):
            return "an illustration must be a base64 WebP, PNG or JPEG data URL"
        return None

    for pattern, why in INTERACTIVE_BANNED:
        if re.search(pattern, code, flags=re.I):
            return why
    if "<" not in code:
        return "code must be an HTML fragment"
    return None


# --------------------------------------------------------------------------
# Page wrapper
# --------------------------------------------------------------------------
THEME = """
:root { --fg:#1d1d1b; --muted:#6b6a65; --bg:#ffffff; --line:#e2e0d8;
        --accent:#3b5bdb; --accent-2:#e8590c; --ok:#1f8a4c; --bad:#c0392b; color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#ecebe6; --muted:#a09e96; --bg:#22221f; --line:#3a3934;
          --accent:#7c95ff; --accent-2:#ff9a5a; --ok:#5fd08f; --bad:#ff8a7a; }
}
html, body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
#root { padding:12px; }
.mermaid { display:flex; justify-content:center; margin:0; }
.illustration { display:block; width:100%; max-width:1024px; height:auto; margin:0 auto; border-radius:8px; }
.visual-error { padding:12px; border-radius:8px; border:1px dashed var(--line); color:var(--muted); }
"""

# Reports height to the parent page (which matches it by event.source) and shows a
# readable fallback instead of a blank box if drawing fails.
BOOT = """
(function () {
  var id = __ID__;
  function post() {
    parent.postMessage({ type: "visual-height", id: id,
                         height: Math.ceil(document.documentElement.scrollHeight) }, "*");
  }
  window.__fail = function (why) {
    var root = document.getElementById("root"), box = document.createElement("div");
    box.className = "visual-error";
    box.textContent = "This visual couldn't be drawn. " + __ALT__;
    root.replaceChildren(box);
    console.error(why);
    post();
  };
  addEventListener("error", function (e) { if (e.message) window.__fail(e.message); });
  addEventListener("load", post);
  new ResizeObserver(post).observe(document.documentElement);
})();
"""

# Colours come from the page's CSS variables (light and dark), the same source the charts use, instead of Mermaid's
# stock grey theme. A flowchart box can take a role: `a["Sun"]:::start`, `:::key` (the step that matters most) or
# `:::result` (where it ends up); the class definitions are added here so the drawing model never writes colours.
DIAGRAM_RUN = """
var css = getComputedStyle(document.documentElement);
function v(name) { return css.getPropertyValue(name).trim(); }
function rgb(hex) {
  hex = hex.replace("#", "");
  if (hex.length === 3) hex = hex.replace(/./g, "$&$&");
  return [0, 2, 4].map(function (i) { return parseInt(hex.substr(i, 2), 16); });
}
function mix(a, b, t) {  // t of colour a over colour b, as #rrggbb
  var x = rgb(a), y = rgb(b);
  return "#" + x.map(function (c, i) {
    return Math.round(c * t + y[i] * (1 - t)).toString(16).padStart(2, "0");
  }).join("");
}
var fg = v("--fg"), bg = v("--bg"), accent = v("--accent"), warm = v("--accent-2"), good = v("--ok");
var font = 'system-ui, -apple-system, "Segoe UI", sans-serif';
var node = document.querySelector(".mermaid"), code = node.textContent;
if (/^\\s*(flowchart|graph)\\b/.test(code)) {
  code = code + "\\n" +
    "classDef start fill:" + mix(good, bg, 0.2) + ",stroke:" + good + ",stroke-width:2.5px,color:" + fg + "\\n" +
    "classDef key fill:" + mix(warm, bg, 0.22) + ",stroke:" + warm + ",stroke-width:2.5px,color:" + fg + "\\n" +
    "classDef result fill:" + mix(accent, bg, 0.32) + ",stroke:" + accent + ",stroke-width:2.5px,color:" + fg + "\\n";
}
node.textContent = "";
mermaid.initialize({
  startOnLoad: false, securityLevel: "strict", theme: "base", fontFamily: font,
  flowchart: { curve: "linear", padding: 14, nodeSpacing: 46, rankSpacing: 56, htmlLabels: true,
               useMaxWidth: true, wrappingWidth: 190 },
  themeVariables: {
    fontFamily: font, fontSize: "16px", background: bg, textColor: fg,
    primaryColor: mix(accent, bg, 0.13), primaryBorderColor: accent, primaryTextColor: fg,
    secondaryColor: mix(warm, bg, 0.13), tertiaryColor: mix(v("--line"), bg, 0.5),
    lineColor: mix(fg, bg, 0.6), nodeTextColor: fg, nodeBorder: accent,
    clusterBkg: mix(accent, bg, 0.05), clusterBorder: mix(accent, bg, 0.45), titleColor: fg,
    edgeLabelBackground: bg
  },
  themeCSS: ".node rect, .node polygon, .node path { stroke-width: 2px; } .node rect { rx: 12px; ry: 12px; } " +
    ".flowchart-link { stroke-width: 2px; } .edgeLabel, .labelBkg { background-color: " + bg + " !important; } " +
    ".edgeLabel { font-size: 14px; padding: 2px 4px; border-radius: 6px; } .edgeLabel p { margin: 0; } " +
    ".cluster rect { rx: 14px; ry: 14px; stroke-dasharray: 5 4; }"
});
// A left-to-right chain that is wider than the page would be shrunk until its text is unreadable: draw it
// top-to-bottom instead (the page is always taller than it is wide on a phone, and scrolls down for free).
function naturalWidth(svg) {
  var m = /viewBox="[-\\d.]+ [-\\d.]+ ([\\d.]+)/.exec(svg);
  return m ? parseFloat(m[1]) : 0;
}
(async function () {
  var room = document.getElementById("root").clientWidth - 24, drawn = await mermaid.render("qn-diagram", code);
  var sideways = /^(\\s*(?:flowchart|graph))\\s+(LR|RL)\\b/;
  if (sideways.test(code) && room > 0 && naturalWidth(drawn.svg) > room * 1.1) {
    drawn = await mermaid.render("qn-diagram-down", code.replace(sideways, "$1 TD"));
  }
  node.innerHTML = drawn.svg;
})().catch(function (e) { __fail(e.message || String(e)); });
"""

# Chart colours come from the same CSS variables as everything else, so light/dark just works.
CHART_RUN = """
var css = getComputedStyle(document.documentElement);
function v(name) { return css.getPropertyValue(name).trim(); }
var fg = v("--fg"), muted = v("--muted"), line = v("--line");
vegaEmbed("#chart", __SPEC__, {
  actions: false, renderer: "svg",
  config: {
    background: "transparent", font: "system-ui, sans-serif",
    padding: { left: 4, top: 4, bottom: 4, right: 64 }, // room for value labels past the longest bar
    title: { color: fg, fontSize: 15 },
    axis: { labelColor: muted, titleColor: fg, domainColor: line, tickColor: line, gridColor: line, labelFontSize: 12 },
    legend: { labelColor: fg, titleColor: fg },
    header: { labelColor: fg, titleColor: fg },
    mark: { color: v("--accent") }, text: { fill: fg }, view: { stroke: null },
    range: { category: [v("--accent"), v("--accent-2"), v("--ok"), v("--bad"), muted, fg] }
  }
}).catch(function (e) { __fail(e.message || String(e)); });
"""


def _script_json(value) -> str:
    """JSON that is safe to place inside a <script> element."""
    return json.dumps(value).replace("</", "<\\/")


def csp(kind: VisualKind) -> str:
    scripts = "'unsafe-inline'" + (" 'unsafe-eval' https://cdn.jsdelivr.net" if LIBS[kind] else "")
    return (
        f"default-src 'none'; script-src {scripts}; style-src 'unsafe-inline'; "
        "img-src data: blob:; font-src data:"
    )


def document(visual: Visual, visual_id: str) -> str:
    """Standalone HTML page for one visual. Assumes validate() already passed."""
    if visual.kind == "diagram":
        body = f'<pre class="mermaid">{html.escape(visual.code)}</pre>'
        run = DIAGRAM_RUN
    elif visual.kind == "chart":
        body = '<div id="chart" style="width:100%"></div>'
        run = CHART_RUN.replace("__SPEC__", _script_json(json.loads(visual.code)))
    elif visual.kind == "illustration":
        body = (
            f'<img class="illustration" src="{html.escape(visual.code)}" alt="{html.escape(visual.alt)}" '
            "onerror=\"__fail('image did not load')\">"
        )
        run = ""
    else:
        body, run = visual.code, ""

    boot = BOOT.replace("__ID__", _script_json(visual_id)).replace(
        "__ALT__", _script_json(visual.alt)
    )
    libs = "".join(f'<script src="{src}"></script>' for src in LIBS[visual.kind])
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{csp(visual.kind)}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(visual.title)}</title><style>{THEME}</style></head>"
        f'<body><script>{boot}</script><div id="root">{body}</div>{libs}'
        f"{f'<script>{run}</script>' if run else ''}</body></html>"
    )
