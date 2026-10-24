---
name: chart
description: Vega-Lite data chart. Use only when the lesson involves real quantities - comparing amounts (planet sizes, animal speeds), change over time (temperature through a day, population growth), parts of a whole (what air is made of), or how two measures relate. Needs numbers that are accurate or clearly marked as approximate.
---
# How to draw a chart

Put a **Vega-Lite v5 JSON spec** in `code`: JSON only, no fences, no comments.

## Pick the mark
- `bar`: comparing amounts between things. The default choice.
- `line` (with `"point": true`): change over time or along a sequence.
- `arc` (pie/donut): parts of a whole, at most 6 parts, adding up to 100%.
- `point`: relationship between two measures.

## Rules
- Data inline only: `"data": {"values": [...]}`. Never use a `url` anywhere.
- `"width": "container", "height": 260`.
- Axis titles with units (`"title": "Distance from Sun (million km)"`).
- Add `"tooltip"` encodings so values show on hover and tap.
- For learners under 12: at most 8 data points, round numbers, everyday units.
- Use real values. If they are approximate, say "about" in the caption.
- No `"config"` or colour schemes. The page handles theming.
- Value labels on bars: use a `layer` with the bar first and a `text` mark second. Put all
  axis settings in the first layer only. Never `"axis": null` in a later layer: layers share
  axes, so it hides the first layer's axis labels too.
- When values differ hugely (96% vs 0.01%), still show every category name on the axis and
  label each bar with its value, so tiny bars aren't invisible.

## Example
{"$schema": "https://vega.github.io/schema/vega-lite/v5.json",
 "data": {"values": [{"gas": "Nitrogen", "percent": 78}, {"gas": "Oxygen", "percent": 21}, {"gas": "Other", "percent": 1}]},
 "mark": {"type": "arc", "innerRadius": 50},
 "encoding": {"theta": {"field": "percent", "type": "quantitative"},
              "color": {"field": "gas", "type": "nominal", "title": "Gas"},
              "tooltip": [{"field": "gas"}, {"field": "percent", "title": "%"}]},
 "width": "container", "height": 260}
