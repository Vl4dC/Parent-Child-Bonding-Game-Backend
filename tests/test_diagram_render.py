import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.diagram_render import SVG, freeze, geometry_signature, render_snapshot, sanitized

SAVED = json.loads((Path(__file__).parent / "fixtures" / "diagram_rendered.json").read_text())


def test_frozen_geometry_labels_and_metadata_secrecy():
    value = freeze(copy.deepcopy(SAVED))
    reference = sanitized(value["reference_svg"])
    puzzle = sanitized(value["puzzle_svg"])
    assert reference.attrib["viewBox"] == puzzle.attrib["viewBox"]
    assert geometry_signature(reference) == geometry_signature(puzzle)
    assert len(value["slots"]) == 3
    assert all(slot["text"] not in value["puzzle_svg"] for slot in value["slots"])
    assert all(
        not any(key.startswith("data-") for key in e.attrib) or "data-slot-id" in e.attrib
        for e in puzzle.iter()
    )
    assert not any(
        e.tag.endswith(("foreignObject", "text", "tspan", "title", "desc")) for e in puzzle.iter()
    )
    original_ids = {e.get("id") for e in reference.iter() if e.get("id")}
    assert original_ids.isdisjoint(e.get("id") for e in puzzle.iter() if e.get("id"))


@pytest.mark.parametrize(
    "markup",
    [
        "<script>alert(1)</script>",
        '<image href="https://evil.example/pixel"/>',
        '<g onclick="steal()"/>',
        '<style>@import "https://evil.example";</style>',
        '<rect style="fill:url(https://evil.example/x)"/>',
    ],
)
def test_active_svg_is_rejected(markup):
    with pytest.raises(HTTPException) as error:
        sanitized(f'<svg xmlns="{SVG}">{markup}</svg>')
    assert error.value.status_code == 422


def test_entities_bad_paths_and_incomplete_manifests_are_rejected():
    with pytest.raises(HTTPException):
        sanitized(f'<!DOCTYPE svg [<!ENTITY leak "secret">]><svg xmlns="{SVG}">&leak;</svg>')
    for change in ("missing", "bad_path", "bad_box", "bad_text"):
        source = copy.deepcopy(SAVED)
        if change == "missing":
            source["labels"].pop()
        if change == "bad_path":
            source["labels"][0]["path"] = [90000]
        if change == "bad_box":
            source["labels"][0]["width"] = float("inf")
        if change == "bad_text":
            source["labels"][0]["text"] = "wrong answer"
        with pytest.raises(HTTPException):
            freeze(source)


def test_css_colors_are_not_corrupted_by_opaque_id_rewriting():
    source = {
        "svg": f'<svg xmlns="{SVG}" viewBox="0 0 200 60"><style>#abc .Water {{fill:#abc;stroke:#fff}}</style>'
        '<g id="abc" class="Water"><rect x="0" y="0" width="80" height="40"/><text>Water</text></g>'
        '<g><rect x="100" y="0" width="80" height="40"/><text>Air</text></g></svg>',
        "labels": [
            {"text": "Water", "path": [1, 1], "x": 10, "y": 10, "width": 40, "height": 20},
            {"text": "Air", "path": [2, 1], "x": 110, "y": 10, "width": 40, "height": 20},
        ],
        "renderer": {},
    }
    result = freeze(source)
    assert "fill:#abc" in result["puzzle_svg"] and "stroke:#fff" in result["puzzle_svg"]
    assert 'class="Water"' not in result["puzzle_svg"] and 'id="abc"' not in result["puzzle_svg"]


def test_renderer_process_receives_no_provider_or_database_credentials(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "private-database-credential")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-provider-credential")
    monkeypatch.setenv("GH_TOKEN", "private-github-credential")
    seen = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, args, **kwargs):
            seen.update(kwargs)

        def communicate(self, *args, **kwargs):
            return json.dumps(SAVED), ""

    settings = SimpleNamespace(
        diagram_node_executable="node", diagram_browser_executable=None, diagram_render_timeout=10
    )
    with patch("app.diagram_render.subprocess.Popen", FakeProcess):
        assert render_snapshot('flowchart LR\n A["A"] --> B["B"]', settings)["slots"]
    assert not {"DATABASE_URL", "ANTHROPIC_API_KEY", "GH_TOKEN"} & seen["env"].keys()
    assert seen["start_new_session"] is True
