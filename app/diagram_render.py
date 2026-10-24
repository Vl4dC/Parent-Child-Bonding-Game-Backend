"""Freeze a harness flowchart once, then remove labels without running layout again."""

import copy
import json
import math
import os
import re
import signal
import subprocess
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

SVG = "http://www.w3.org/2000/svg"
XHTML = "http://www.w3.org/1999/xhtml"
ET.register_namespace("", SVG)
ET.register_namespace("html", XHTML)
ALLOWED = {
    "svg",
    "g",
    "defs",
    "marker",
    "path",
    "rect",
    "line",
    "polyline",
    "polygon",
    "circle",
    "ellipse",
    "style",
    "text",
    "tspan",
    "title",
    "desc",
    "foreignObject",
    "div",
    "span",
    "p",
    "br",
    "b",
    "strong",
    "i",
    "em",
    "clipPath",
    "filter",
    "feDropShadow",
}
GEOMETRY = {
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "width",
    "height",
    "d",
    "points",
    "rx",
    "ry",
    "r",
    "cx",
    "cy",
    "transform",
    "viewBox",
}


def unsupported():
    raise HTTPException(422, "This saved visual cannot be used as a text-label flowchart puzzle.")


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def normalize(text):
    return " ".join(text.split())


def label_text(node):
    if local_name(node.tag) != "foreignObject":
        return normalize("".join(node.itertext()))

    def content(element):
        text = element.text or ""
        for child in element:
            text += " " if local_name(child.tag) == "br" else content(child)
            if local_name(child.tag) in ("p", "div"):
                text += " "
            text += child.tail or ""
        return text

    return normalize(content(node))


def sanitized(svg):
    if len(svg) > 1_500_000 or re.search(r"<!DOCTYPE|<!ENTITY", svg, flags=re.I):
        unsupported()
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        unsupported()
    if root.tag != f"{{{SVG}}}svg":
        unsupported()
    nodes = list(root.iter())
    if len(nodes) > 5000:
        unsupported()
    for node in nodes:
        tag = local_name(node.tag)
        if tag not in ALLOWED or not node.tag.startswith(("{" + SVG + "}", "{" + XHTML + "}")):
            unsupported()
        for key, value in node.attrib.items():
            name = local_name(key).lower()
            if name.startswith("on") or (name in ("href", "src") and not value.startswith("#")):
                unsupported()
            if re.search(r"javascript:|@import|expression\s*\(", value, re.I):
                unsupported()
            for target in re.findall(r"url\(\s*['\"]?([^)'\"]+)", value, re.I):
                if not target.strip().startswith("#"):
                    unsupported()
        if tag == "style":
            value = node.text or ""
            if re.search(r"@import|javascript:|expression\s*\(", value, re.I):
                unsupported()
            if any(
                not x.strip().startswith("#")
                for x in re.findall(r"url\(\s*['\"]?([^)'\"]+)", value, re.I)
            ):
                unsupported()
    return root


def geometry_signature(root):
    """All original non-text geometry, independent of ids, CSS classes and slot overlays."""
    return [
        (
            local_name(node.tag),
            tuple(sorted((k, v) for k, v in node.attrib.items() if k in GEOMETRY)),
        )
        for node in root.iter()
        if local_name(node.tag)
        not in (
            "text",
            "tspan",
            "foreignObject",
            "div",
            "span",
            "p",
            "br",
            "b",
            "strong",
            "i",
            "em",
            "style",
            "title",
            "desc",
        )
        and "data-slot-id" not in node.attrib
    ]


def freeze(rendered):
    try:
        svg, labels = rendered["svg"], rendered["labels"]
        if not isinstance(svg, str) or not isinstance(labels, list) or not 2 <= len(labels) <= 40:
            unsupported()
        source = sanitized(svg)
        original = ET.tostring(source, encoding="unicode")
        blank = copy.deepcopy(source)
        targets = []
        solutions = []
        for label in labels:
            node = blank
            path = label["path"]
            if not isinstance(path, list) or not path or len(path) > 64:
                unsupported()
            for index in path:
                if type(index) is not int or not 0 <= index < len(node):
                    unsupported()
                node = node[index]
            if local_name(node.tag) not in ("text", "foreignObject") or node in targets:
                unsupported()
            text = normalize(label["text"])
            if not text or len(text) > 300 or label_text(node) != text:
                unsupported()
            bounds = {key: float(label[key]) for key in ("x", "y", "width", "height")}
            if not all(math.isfinite(v) and abs(v) < 1_000_000 for v in bounds.values()):
                unsupported()
            if bounds["width"] <= 0 or bounds["height"] <= 0:
                unsupported()
            targets.append(node)
            solutions.append({"id": uuid4().hex, "text": text, **bounds})
        parents = {child: parent for parent in blank.iter() for child in parent}
        for node in targets:
            parents[node].remove(node)
        for parent in list(blank.iter()):
            for child in list(parent):
                if local_name(child.tag) in ("title", "desc"):
                    parent.remove(child)
                elif local_name(child.tag) in ("foreignObject", "text", "tspan") and not normalize(
                    " ".join(child.itertext())
                ):
                    parent.remove(child)
        # All label text must be accounted for; reject unsupported hidden or split text structures.
        if any(local_name(node.tag) in ("foreignObject", "text", "tspan") for node in blank.iter()):
            unsupported()
        for node in blank.iter():
            if local_name(node.tag) != "style" and normalize(node.text or ""):
                unsupported()
            if normalize(node.tail or ""):
                unsupported()
        ids = {node.attrib["id"]: "g" + uuid4().hex for node in blank.iter() if "id" in node.attrib}
        classes = {
            name: "c" + uuid4().hex
            for node in blank.iter()
            for name in node.get("class", "").split()
        }
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name) for name in classes):
            unsupported()

        def rewrite_selectors(value):
            for old, new in ids.items():
                value = re.sub(r"#" + re.escape(old) + r"(?![\w-])", "#" + new, value)
            for old, new in classes.items():
                value = re.sub(r"\." + re.escape(old) + r"(?![\w-])", "." + new, value)
            return value

        def rewrite_references(value):
            return re.sub(
                r"url\(\s*(['\"]?)#([^)'\"\s]+)\1\s*\)",
                lambda m: "url(#" + ids.get(m[2], m[2]) + ")",
                value,
            )

        for node in blank.iter():
            for key in list(node.attrib):
                if key.startswith(("data-", "aria-")) or key == "title":
                    del node.attrib[key]
            if "id" in node.attrib:
                node.set("id", ids[node.attrib["id"]])
            if "class" in node.attrib:
                node.set("class", " ".join(classes[x] for x in node.attrib["class"].split()))
            for key in list(node.attrib):
                if key not in ("id", "class"):
                    value = node.attrib[key]
                    if local_name(key) == "href" and value.startswith("#"):
                        value = "#" + ids.get(value[1:], value[1:])
                    node.set(key, rewrite_references(value))
            if local_name(node.tag) == "style":
                node.text = rewrite_references(
                    re.sub(r"([^{}]+)\{", lambda m: rewrite_selectors(m[1]) + "{", node.text or "")
                )
        if geometry_signature(source) != geometry_signature(blank):
            unsupported()
        for slot in solutions:
            ET.SubElement(
                blank,
                f"{{{SVG}}}rect",
                {
                    "data-slot-id": slot["id"],
                    **{key: str(slot[key]) for key in ("x", "y", "width", "height")},
                    "fill": "transparent",
                    "stroke": "#64748b",
                    "stroke-dasharray": "3 2",
                    "rx": "3",
                },
            )
        blank.set("role", "img")
        blank.set("aria-label", "Diagram with empty label placeholders")
        return {
            "reference_svg": original,
            "puzzle_svg": ET.tostring(blank, encoding="unicode"),
            "slots": solutions,
            "view_box": source.attrib["viewBox"],
            "renderer": rendered.get("renderer", {}),
            "snapshot_version": 1,
        }
    except (KeyError, TypeError, ValueError, IndexError, RecursionError):
        unsupported()


def render_snapshot(code, settings):
    if (
        len(code) > 5000
        or not re.match(r"\s*(flowchart|graph)\b", code)
        or re.search(r"%%\{|^\s*click\s", code, re.I | re.M)
    ):
        unsupported()
    helper = Path(__file__).resolve().parents[1] / "tools" / "diagram-renderer" / "render.mjs"
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "LANG", "XDG_CACHE_HOME", "PLAYWRIGHT_BROWSERS_PATH")
        if key in os.environ
    }
    if settings.diagram_browser_executable:
        env["DIAGRAM_BROWSER_EXECUTABLE"] = settings.diagram_browser_executable
    env["DIAGRAM_BROWSER_SANDBOX"] = (
        "1" if getattr(settings, "diagram_browser_sandbox", True) else "0"
    )
    try:
        process = subprocess.Popen(
            [settings.diagram_node_executable, str(helper)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
    except OSError:
        raise HTTPException(503, "Diagram renderer is not configured.") from None
    try:
        stdout, _ = process.communicate(
            json.dumps({"code": code, "budget_ms": settings.diagram_render_timeout * 1000 - 3000}),
            timeout=settings.diagram_render_timeout,
        )
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise HTTPException(503, "Diagram rendering did not complete.") from None
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        process.communicate()
        raise
    if process.returncode == 1:
        raise HTTPException(503, "Diagram renderer or browser is not configured.")
    if process.returncode or len(stdout) > 2_000_000:
        raise HTTPException(422, "The saved flowchart could not be rendered as a label puzzle.")
    try:
        return freeze(json.loads(stdout))
    except json.JSONDecodeError:
        raise HTTPException(503, "Diagram renderer returned invalid output.") from None
