# Which model should draw the visuals? A bake-off

Date: 2026-10-24. Question: would a separate, cheaper "visualization agent" draw better or cheaper visuals than
the model the app already uses (`openai/gpt-6-luna`, $0.10 / $0.50 per million tokens)? No image generation: the
visuals are code (Mermaid flowcharts, Vega-Lite charts, small HTML widgets), and hide-a-box questions need that structure.

## Method

Seven models, each given the same six visual tasks through the real harness (`visuals/harness.draw_visual`: the
skill instructions, the validator, up to 3 attempts), one sample per cell:

| Task | Kind |
|---|---|
| The water cycle as a loop (age 10) | diagram |
| How a web page reaches you (age 14) | diagram |
| Planet sizes with the real numbers given (age 10) | chart |
| What air is made of, tiny parts must stay visible (age 12) | chart |
| Light intensity vs sugar-making rate, including where it levels off (age 12) | interactive |
| A lever that balances when weight x distance is equal on both sides (age 11) | interactive |

Every result was rendered in headless Chrome (console errors, fallback message, whether the slider changes anything,
how many boxes of a diagram could be hidden for a quiz) and then looked at on side-by-side contact sheets.
Total cost of the run: **$0.14**, 5 minutes.

## Results

| Model | Drew all 6 | Right first try | s / visual | $ / visual | What the pictures were like |
|---|---|---|---|---|---|
| **openai/gpt-6-luna** (current) | 6/6 | 5/6 | 18 | 0.0010 | Best overall. Readable vertical flowcharts, planets in order from the Sun with labelled bars, correct lever physics, and the only light widget that also draws the "levels off" curve. |
| anthropic/claude-haiku-5.5 | 6/6 | 6/6 | 14 | 0.0016 | A close second: readable diagrams, good charts, correct lever. |
| anthropic/claude-sonnet-5.5 (reference) | 6/6 | 6/6 | 12 | 0.0184 | Good diagrams and charts, but the lever showed the same distance on both sides (24 vs 12): wrong physics, at 18x the price. |
| deepseek/deepseek-v4-flash | 6/6 | 6/6 | 33 | 0.0005 | Wide horizontal flowcharts with tiny text; charts sorted alphabetically; interactives work. |
| qwen/qwen3-coder-30b-a3b-instruct | 6/6 | 6/6 | 13 | 0.0002 | Tiny horizontal diagrams, bars without value labels, lever physics wrong (5 and 5). |
| google/gemini-2.5-flash-lite | 6/6 | 6/6 | 2.7 | 0.0003 | Fastest, but a 3-box diagram, unlabelled bars, a plant drawn as an orange arrow, lever wrong. |
| qwen/qwen3.7-flash | 6/6 | 5/6 | 50 | 0.0006 | Slowest (one chart took 3 tries and 113 s); an unreadable one-row diagram; both interactives broken. |

"Drew" only means the code ran. Quality is what separates the models, and the validator doesn't see it.

## What it means

- **Cost is not the lever.** A lesson has about five visuals: the current model spends about half a cent on them, the cheapest
  alternative a tenth of a cent. Even the expensive reference is under 10 cents per lesson.
- **No cheaper model was as good.** The three cheapest ones (Qwen3-Coder, Gemini-lite, DeepSeek) wrote the wide
  horizontal diagrams the skill file tells them not to, skipped the value labels the brief asked for, and got the lever wrong.
- **A pricier model isn't automatically better.** Sonnet matched Haiku on diagrams and charts and still got the
  physics wrong; gpt-6-luna got it right.
- **Decision: keep `gpt-6-luna` for visuals.** A separate drawing model is available as a setting (`VISUAL_MODEL` in `.env`,
  same format as `QUIZ_MODEL`, e.g. `openrouter:anthropic/claude-haiku-5.5`); unset means the main model. Planning which
  visuals a lesson needs stays on the main model.

## Caveats

- One sample per model and task, judged by eye and by the automated checks above. A single wrong lever may not repeat.
- Model behaviour changes; re-run before switching.
- The automated "facts present" score was unreliable for interactives (string matching), so it is not used above.

## What would actually improve visuals

The lever result shows the real risk: a widget that *runs* but teaches something false, even from an expensive model.
A cheap second pass that reads the finished widget's code against the brief ("does the product change when the
fulcrum moves?") would catch more than switching models. Not built yet.
