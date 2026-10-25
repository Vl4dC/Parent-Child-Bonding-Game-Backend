---
name: diagram
description: Mermaid diagram of boxes and arrows. Use for processes and cycles (water cycle, steps of digestion), cause-and-effect chains, parts of a whole or hierarchies (food chains, classification), sequences and timelines, and decision flows. Not for numbers (use chart) or anything the learner should play with (use interactive).
---
# How to draw a diagram

Put **Mermaid source only** in `code`: no ``` fences, no explanation.

A diagram is the lesson's idea drawn as one connected picture. After studying it the learner should be able to
tell the whole story out loud, from the first box to the last, using only the boxes and the arrow labels.

## Pick the diagram type
- `flowchart TD` (top to bottom): the default. Processes, cause and effect, cycles, hierarchies, anything longer than
  four boxes in a row.
- `flowchart LR` (left to right): only a short chain of up to four boxes in a row.
- `timeline`: events in order with dates or stages.
- `mindmap`: one idea broken into parts.
- `sequenceDiagram`: who passes what to whom, step by step.
- `stateDiagram-v2`: something that changes state (ice, water, steam).

## What makes it good
- **The whole mechanism, start to finish.** If the section explains how electricity reaches a home, the picture runs
  from the power station to the home, not just the first two stages. Never stop halfway and never draw only the
  parts without the links between them.
- **5-9 boxes** (at most 7 for learners under 10). Fewer is a fragment; more cannot be read.
- **One connected picture.** Every box has an arrow in or out. No floating notes, no second diagram beside the first.
- **Every arrow says what happens**, in 6 words or fewer, with a verb or a reason: `A -->|"carries"| B`,
  `A -->|"because"| B`. If an arrow needs a long explanation, make that a box of its own. Never an
  empty arrow where the link is the thing being taught, and never a vague word like "then" or "next".
- **Shape the structure.** Use branches where one thing splits (`A --> B` and `A --> C`), merges where several
  things combine (`B --> D` and `C --> D`), a loop back for a cycle, and a diamond for a real yes/no choice:
  `Q{"Is it cold enough?"} -->|"yes"| R["Water freezes"]`. A plain straight chain is only right for a plain sequence.
- **Group with `subgraph`** when the boxes belong to two or three places or stages (inside the cell, outside it).
- **Mark the roles** so the eye knows where to look: add `:::start` to the box where it begins, `:::key` to the one
  step that matters most, `:::result` to where it ends. Use each at most once or twice.
- Show the real mechanism from the lesson. Don't invent facts, and don't draw a decorative summary of headings.

## Rules
- Every node label in double quotes, short (6 words or fewer), in the learner's vocabulary: `A["Sunlight hits the leaf"]`.
- Give every box an id (`sun`, `leaf`) and use it again for later arrows instead of repeating the label.
- Never use `(` `)` `[` `]` `{` `}` `;` inside a label except within the double quotes.
- No `style`, `classDef`, `click` or `%%{init}%%` lines and no HTML in labels. The page handles colours; use only the
  three `:::` roles above.

## Example (a process with a split, a merge and an ending)
flowchart TD
  sun["Sunlight hits the leaf"]:::start -->|"gives energy to"| chloro["Chlorophyll in the leaf"]:::key
  water["Water from the roots"] -->|"travels up the stem to"| chloro
  air["Carbon dioxide from the air"] -->|"enters through tiny holes in"| chloro
  chloro -->|"uses the energy to build"| sugar["Sugar: food for the plant"]:::result
  chloro -->|"lets go of"| oxygen["Oxygen released into the air"]:::result
