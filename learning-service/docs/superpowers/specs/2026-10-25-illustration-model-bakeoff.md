# Which image model should draw illustrations? A bake-off

Date: 2026-10-25. Question: for the new `illustration` visual kind (a picture with no words in it, drawn by an
image model instead of code), which OpenRouter image model gives the best pictures quickly?

## How illustrations are drawn

No chat model is involved. The lesson writer already writes a `brief` for every visual; `visuals/illustrate.py`
wraps it in a fixed house style (picture-book for ages up to 11, science-textbook above), the age rules and a
"no text, letters or numbers" rule, and sends one request to `POST https://openrouter.ai/api/v1/images`
(4:3, 1K). The image is shrunk to a WebP no wider than 1024 px and stored in the lesson as a data URL.
Title, caption and alt text come from the plan, so the text safety check still reads them.

## Method

Six models, the same four briefs, one sample per cell, all sent in parallel. Timings are wall-clock from this
machine; costs are OpenRouter's `usage.cost`. Total cost of the run: about **$0.61**.

| Brief | Age | Tests |
|---|---|---|
| A volcano cut away to show the magma chamber and pipe, lava down one side | 9 | a cut-away that is right |
| A coral reef with branching and brain corals, a turtle, a clownfish in an anemone | 12 | many named things |
| A market street in ancient Rome, stalls, tunics and togas, a temple | 15 | historical accuracy |
| A bean seedling in a cut-away of soil, roots spreading, first two leaves | 10 | botany detail |

## Results

| Model | s / image | $ / image | WebP KB | What the pictures were like |
|---|---|---|---|---|
| **google/gemini-3.1-flash-lite-image** (chosen) | 6.5 | 0.034 | 16-76 | Followed every brief: magma chamber and pipe, the bean still attached to the seedling, the clownfish in its anemone. Clean, consistent style. |
| google/gemini-3.1-flash-image | 12.2 | 0.067 | 16-108 | The richest detail (the Roman market especially), but twice the time and price, and the seedling's leaves were malformed. |
| black-forest-labs/flux-3-image-klein | 9.2 | 0.009 | 30-122 | Good pictures at a quarter of the price; the volcano's chamber was less clear. The best cheap option. |
| bytedance-seed/seedream-5-0-flash | 16.3 | 0.018 | 35-144 | Very accurate (best seedling), but slow. |
| recraft/recraft-v4.1-flash | 3.1 | 0.007 | 15-144 | Fastest, but often wrong: white "lava", a modern-looking street in ancient Rome. |
| qwen/qwen-image-2.1-turbo | 23.2 | 0.018 | 67-248 | Near-photographic, ignores the house style, and the slowest. |

No model put any text in any picture, so the "no text" rule in the prompt holds.

## Live check

Two real lessons with `ILLUSTRATIONS_ENABLED=true` (volcanoes at 10, the coral reef ecosystem at 13): the
lesson writer chose an illustration for the "what is it / what does it look like" section and diagrams or
interactives for the processes. Each illustration took about 5 s and $0.034 and was 23-93 KB stored. A first
wording of the writer's rule ("only where seeing it teaches more than a labelled diagram") made it choose none;
the rule now says to use one when a section is about what something looks like or how it is built.

## Decision

- Default `ILLUSTRATION_MODEL=google/gemini-3.1-flash-lite-image`.
- `black-forest-labs/flux-3-image-klein` if cost matters more (a quarter of the price, ~3 s slower).
- `google/gemini-3.1-flash-image` if the best detail matters more than speed.
- `ILLUSTRATIONS_PER_LESSON` defaults to 2: an image costs ~30x a code visual.

## Caveats

- One sample per model and brief, judged by eye. Re-run before switching; image models change quickly.
- Pictures are not checked by the safety model (it reads text). The prompt is built from a brief that was
  already checked as part of the lesson, plus the provider's own filters. A vision check would add ~1-2 s.
