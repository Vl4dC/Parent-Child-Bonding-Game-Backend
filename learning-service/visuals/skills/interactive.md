---
name: interactive
description: Hand-built HTML + SVG + JavaScript widget. Use when the learner understands by doing or by looking closely - a slider that changes one variable and shows the effect (more sunlight means more sugar), a labelled illustration of parts (a leaf, the heart, an atom, a circuit), a step-by-step animation with Next/Back buttons, or a tiny simulation. Most effort, so use it only when a diagram or chart can't show the idea.
---
# How to build an interactive

Put an **HTML fragment** in `code`: markup, then one `<style>`, then one `<script>`.
No `<html>`, `<head>` or `<body>` tags. The page wraps the fragment for you.

## Hard limits (the visual is rejected otherwise)
- Self-contained: no external scripts, images, fonts or links. No `fetch`, `XMLHttpRequest`, `WebSocket`.
- No `localStorage`, `sessionStorage` or `document.cookie`. They throw in the sandbox.
- No `alert`, `prompt`, `confirm`, `<iframe>`.

## Design rules
- One idea per widget. Keep it under about 250 lines.
- Draw with inline `<svg viewBox="...">` and `width="100%"` so it scales on phones.
- Colours only through the page's CSS variables, so it works in light and dark mode:
  `var(--fg)` text, `var(--muted)` secondary text, `var(--bg)` background, `var(--line)` borders,
  `var(--accent)` highlight, `var(--accent-2)` second highlight, `var(--ok)` good, `var(--bad)` bad.
- Controls are real `<input type="range">` / `<button>` elements with visible labels. Show the
  current value next to a slider, and update the picture on every `input` event.
- Everything works by keyboard and touch. Never hide information behind hover alone.
- Text in the learner's vocabulary; font-size at least 14px (16px for learners under 10).
- No overlapping text: give every label its own clear space (plan x/y positions so labels
  sit at least one line-height apart and away from shapes), and use one label per meter or bar.
- Be accurate: label parts the way the lesson does, and keep cause and effect physically right
  (if the effect saturates or has a limit in reality, show that).

## Skeleton
<div class="w">
  <label>Sunlight <input id="light" type="range" min="0" max="10" value="5"> <b id="lv">5</b></label>
  <svg viewBox="0 0 300 160" width="100%" role="img" aria-label="Plant making sugar">...</svg>
</div>
<style>.w{display:grid;gap:12px} label{display:flex;gap:8px;align-items:center}</style>
<script>
  const light = document.getElementById("light");
  function draw() { document.getElementById("lv").textContent = light.value; /* update svg */ }
  light.addEventListener("input", draw); draw();
</script>
