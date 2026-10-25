"""
Structure checks for a Mermaid flowchart, run before a diagram is accepted.

The validator in render.py only knows the code is Mermaid-shaped; it can't see a picture that is technically valid
but teaches little. These checks catch what made the weakest diagrams: a fragment of two or three boxes, boxes with no
arrows, a drawing that splits into separate pieces, and labels that break Mermaid's parser. Each message says what to
change, because it is sent back to the drawing model as feedback. `syntax_problem` is a hard rejection (the diagram
would not draw); `problem` is about quality, so the harness keeps the draft if the model can't fix it.

Only flowcharts are checked. Other diagram types (sequence, timeline, mindmap ...) have their own shape.
"""

from __future__ import annotations

import re

MIN_NODES = 5
MAX_NODES = 12
MAX_LABEL_WORDS = 9
MAX_ARROW_WORDS = 8

KEYWORDS = {"subgraph", "end", "direction", "flowchart", "graph", "LR", "RL", "TD", "TB", "BT"}
OPENERS = r"\(\[|\[\(|\(\(|\[\[|\{\{|\[|\(|\{|>"
DECLARED = re.compile(rf'(?<![\w|"])(?P<id>[A-Za-z_]\w*)\s*(?:{OPENERS})\s*"(?P<label>[^"\n]+)"')
UNQUOTED = re.compile(r"(?<![\w|\"])[A-Za-z_]\w*\[(?![\"\[\(/\\])[^\]\"\n]*[(){};][^\]\n]*\]")
EDGE_LABEL = re.compile(r'\|\s*"?([^|"\n]+)"?\s*\|')
ARROW = re.compile(r"<?-{2,}[>xo-]?|<?={2,}>?|<?-\.+-?>?")
IDENT = re.compile(r"\b[A-Za-z_]\w*\b")


def is_flowchart(code: str) -> bool:
    first = code.strip().split(maxsplit=1)
    return bool(first) and first[0] in ("flowchart", "graph")


def _edge_groups(code: str) -> list[set[str]]:
    """The box ids on each line that has an arrow (a chain `a --> b --> c` is one group)."""
    groups = []
    for line in code.splitlines():
        if line.strip().startswith(("subgraph", "classDef", "class ", "style ", "linkStyle")):
            continue
        plain = re.sub(r'"[^"\n]*"', "", line)
        plain = re.sub(r":::\w+", "", plain)  # a role (:::key) is not a box
        plain = re.sub(r"\|[^|\n]*\|", "", plain)
        if not ARROW.search(plain):
            continue
        ids = {i for i in IDENT.findall(plain) if i not in KEYWORDS}
        if ids:
            groups.append(ids)
    return groups


def _pieces(groups: list[set[str]]) -> list[set[str]]:
    """Merge groups that share a box: the separate connected pictures in the drawing."""
    pieces: list[set[str]] = []
    for group in groups:
        merged = set(group)
        rest = []
        for piece in pieces:
            if piece & merged:
                merged |= piece
            else:
                rest.append(piece)
        pieces = [*rest, merged]
    return pieces


def syntax_problem(code: str) -> str | None:
    """A label Mermaid can't parse (the diagram would not draw at all), else None."""
    if is_flowchart(code) and UNQUOTED.search(code):
        return (
            'a label with ( ) { } or ; in it must be wrapped in double quotes, e.g. a["Sun (star)"]; '
            "put every label in double quotes"
        )
    return None


def problem(code: str) -> str | None:
    """What makes this flowchart a weak explanation (it still draws), else None."""
    if not is_flowchart(code):
        return None
    groups = _edge_groups(code)
    declared: dict[str, str] = {}
    for line in code.splitlines():
        if line.strip().startswith("subgraph"):
            continue
        for m in DECLARED.finditer(line):
            declared.setdefault(m["id"], m["label"].strip())
    connected = set().union(*groups) if groups else set()
    nodes = connected | set(declared)
    if len(nodes) < MIN_NODES:
        return (
            f"only {len(nodes)} boxes: that is a fragment, not an explanation. Draw the whole idea from where it "
            f"starts to where it ends, with {MIN_NODES}-9 boxes"
        )
    if len(nodes) > MAX_NODES:
        return f"{len(nodes)} boxes is too many to read; keep the main idea and use at most 9"
    loose = [declared[i] for i in declared if i not in connected]
    if loose:
        return (
            f"these boxes have no arrows to anything: {', '.join(repr(x) for x in loose[:4])}. Connect "
            "each one into the flow or remove it (no floating notes)"
        )
    pieces = _pieces(groups)
    if len(pieces) > 1:
        return (
            f"the drawing falls into {len(pieces)} separate pieces. It must be one connected picture: add the "
            "arrows that link the pieces, or draw only one of them"
        )
    wordy = [x for x in declared.values() if len(x.split()) > MAX_LABEL_WORDS]
    if wordy:
        return f"label too long ({wordy[0][:40]!r}...): keep each box to {MAX_LABEL_WORDS} words or fewer"
    chatty = [x for x in EDGE_LABEL.findall(code) if len(x.split()) > MAX_ARROW_WORDS]
    if chatty:
        return (
            f"arrow label too long ({chatty[0][:40]!r}...): an arrow says what happens in "
            f"{MAX_ARROW_WORDS} words or fewer; move the detail into its own box"
        )
    return None
