---
name: diagram
description: Mermaid diagram of boxes and arrows. Use for processes and cycles (water cycle, steps of digestion), cause-and-effect chains, parts of a whole or hierarchies (food chains, classification), sequences and timelines, and decision flows. Not for numbers (use chart) or anything the learner should play with (use interactive).
---
# How to draw a diagram

Put **Mermaid source only** in `code`: no ``` fences, no explanation.

## Pick the diagram type
- `flowchart LR` (or `TD`): processes, cause and effect, inputs to outputs. The default choice.
  Use `LR` for up to 5 nodes in a row; use `TD` for longer chains and cycles, so the text
  stays readable on narrow screens.
- `timeline`: events in order with dates or stages.
- `mindmap`: one idea broken into parts.
- `sequenceDiagram`: who passes what to whom, step by step.
- `stateDiagram-v2`: something that changes state (ice, water, steam).

## Rules
- 4-10 nodes. For learners under 10, at most 6.
- Every node label in double quotes, short (6 words or fewer), in the learner's vocabulary: `A["Sunlight hits the leaf"]`.
- Label arrows with what happens: `A -->|"gives energy"| B`.
- Show the real mechanism from the lesson, not a decorative summary of headings.
- No `style`, `classDef`, `click` or `%%{init}%%` lines, no HTML in labels. The page handles colours.
- Never use `(` `)` `[` `]` `{` `}` `;` inside a label except within the double quotes.

## Example
flowchart LR
  sun["Sunlight"] -->|"energy"| leaf["Leaf"]
  water["Water from roots"] --> leaf
  air["Carbon dioxide from air"] --> leaf
  leaf -->|"makes"| sugar["Sugar: food for the plant"]
  leaf -->|"releases"| oxygen["Oxygen"]
