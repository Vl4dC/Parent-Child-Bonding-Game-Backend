"""Real local browser, no AI: run in the dedicated diagram-renderer CI job."""

import os
from types import SimpleNamespace

import pytest

from app.diagram_render import geometry_signature, render_snapshot, sanitized

pytestmark = pytest.mark.skipif(
    not os.getenv("DIAGRAM_RENDERER_TESTS"),
    reason="Enable local headless renderer checks explicitly",
)


@pytest.mark.parametrize(
    "code,expected",
    [
        (
            'flowchart LR\n A["Sunlight"] --> B["Photosynthesis"]\n B --> C["Sugar"]',
            ["Sunlight", "Photosynthesis", "Sugar"],
        ),
        (
            'flowchart TD\n A{"Ready?"} -->|Yes| B["Begin"]\n A -->|No| C["Wait"]',
            ["Ready?", "Begin", "Wait", "Yes", "No"],
        ),
        ('flowchart LR\n A["Water"] --> B["Water"]', ["Water", "Water"]),
        ('flowchart LR\n A["Wärme"] --> B["蒸発"]', ["Wärme", "蒸発"]),
        (
            'flowchart TB\n subgraph S["Cycle"]\n A["First"] --> B["Second"]\n end',
            ["Cycle", "First", "Second"],
        ),
        ('flowchart LR\n A["Two<br/>lines"] --> B["Done"]', ["Two lines", "Done"]),
    ],
)
def test_real_snapshot_preserves_geometry_and_removes_all_labels(code, expected):
    settings = SimpleNamespace(
        diagram_node_executable="node",
        diagram_browser_executable=os.getenv("DIAGRAM_BROWSER_EXECUTABLE"),
        diagram_browser_sandbox=os.getenv("DIAGRAM_BROWSER_SANDBOX") != "0",
        diagram_render_timeout=30,
    )
    result = render_snapshot(code, settings)
    assert sorted(s["text"] for s in result["slots"]) == sorted(expected)
    assert geometry_signature(sanitized(result["reference_svg"])) == geometry_signature(
        sanitized(result["puzzle_svg"])
    )
    assert all(text not in result["puzzle_svg"] for text in expected)
