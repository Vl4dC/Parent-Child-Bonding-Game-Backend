# Diagram assembly race — saved visuals, simultaneous phones

**Concept:** Players study a saved lesson diagram, then drag its shuffled labels into the exact
blank placeholders; the fastest fully correct reconstruction wins, with review and repeat rounds.

Added 24 October 2026. Backend only. The user selected separate phones playing simultaneously.
The only gameplay question is **“How does this work?”** Existing source and documentation are
preserved; this guide supplements them. No AI generation, prompt, harness or frontend code changes.

## Exact diagram reuse and its boundary

The existing harness saves `StoredLesson.visuals`: each saved visual has an ID, kind, source code,
title/caption/alt and section reference. Mermaid is rendered in the browser by the existing view;
there is no persistent SVG layout in that record. Removing labels from Mermaid and rendering it
again would change box sizes and spacing, so this game does not do that.

The new server helper reads the existing owned visual's **unchanged Mermaid source**, renders one
canonical SVG, and saves a reference plus a blank clone. It removes visible label elements from
that clone without running layout again. Box/arrow coordinates, transforms, paths, shape sizes
and viewBox stay identical between the frozen study/reference and game views. Node, edge and
subgraph labels become slots; cards contain the original text. No new diagram or label is written
by a model. Source-derived IDs/classes, title/description/accessibility hints and data attributes
are removed or made opaque so they do not reveal slot assignments.

The renderer uses pinned **Mermaid 12.2.0**, independently of the existing harness view's older
browser renderer. The exact-layout guarantee is between this frozen study copy and its blank
game clone. It is not a claim to recover pixels/coordinates from an earlier responsive browser
view that was never saved as SVG. The AI-authored source and existing renderer remain unchanged.

First release supports ordinary Mermaid `flowchart`/`graph` visuals with 2–40 visible label blocks,
each at most 300 characters. Charts, arbitrary HTML/interactive visuals, image/math-rich SVG and
other Mermaid diagram types return 422 rather than being redrawn or approximated. Unsafe SVG,
external references, scripts, event handlers and unsupported elements are rejected. Static label
removal preserves styling and geometry; colours that resemble IDs are not rewritten as selectors.

## Backend setup

1. Explicitly apply migration `20261024000800_diagram_assembly.sql` to the chosen development or
   staging database. Startup never applies migrations.
2. Install Node.js 22 and renderer dependencies, separately from Python:

```sh
cd tools/diagram-renderer
npm ci --ignore-scripts --no-fund --no-audit
npx --no-install playwright-core install --with-deps chromium
```

3. Set `DIAGRAM_ASSEMBLY_ENABLED=true` after checking the renderer and migrations. Default is false.
   Other settings: `DIAGRAM_NODE_EXECUTABLE=node`, `DIAGRAM_RENDER_TIMEOUT=20` seconds,
   `DIAGRAM_RENDER_CONCURRENCY=1`, and optional `DIAGRAM_BROWSER_EXECUTABLE` for an explicitly
   managed browser. `DIAGRAM_BROWSER_SANDBOX=true` by default; production needs a compatible OS
   browser sandbox/isolation setup. The dedicated CI job opts out only for trusted synthetic fixtures
   on the hosted runner. Do not copy that opt-out into an unisolated public deployment.

The helper is server-only, uses an isolated headless browser, blocks runtime network access, and
receives no database, model-provider, SMTP or GitHub credentials. It is time-bounded and terminates
its owned browser process. No browser or model starts with the API. Rendering has a separate
concurrency limit; a busy renderer returns 429. Compiled templates are cached per owned visual
fingerprint. Database connections/locks are released during rendering; source identity/review/hash
are rechecked in a short final transaction so deletion or edits cannot resurrect stale artifacts.

## API inventory

All routes start with `/api/diagram-assembly` and need the Bearer sign-in token; its `player_id` claim is the
participant. The feature returns 503 while disabled. Body models reject unknown fields, including
client-supplied source/SVG, actor IDs, scores and times. Idempotent operations require an 8–128
character `Idempotency-Key`.

| Method | Path | Behavior |
| --- | --- | --- |
| POST | `/templates` | Freeze an existing owned, approved lesson flowchart; idempotent/cached |
| GET | `/templates/{id}` | Owner-only template metadata, not the solution |
| POST | `/races` | Creating room host explicitly shares their selected diagram with captured participants |
| GET | `/rooms/{room_id}/race` | Discover the latest room race, restricted to captured participants |
| GET | `/races/{id}` | Phase, shared timing and participant progress counts |
| GET | `/races/{id}/study` | Frozen labelled reference while waiting; closes on start |
| POST | `/races/{id}/start` | Creating host starts one shared five-second countdown |
| GET | `/races/{id}/board` | Masked SVG, slot coordinates, caller's cards/placements and timing |
| PUT | `/races/{id}/placements` | Versioned full replacement of the caller's partial/full mapping |
| POST | `/races/{id}/finish` | Check a full mapping; record the first perfect server finish |
| POST | `/races/{id}/forfeit` | End the caller's attempt without a ranked time |
| GET | `/races/{id}/results` | Ranking after all attempts end or the operational deadline |
| GET | `/races/{id}/review` | Reference SVG and correct label mapping after the round |
| POST | `/races/{id}/cancel` | Creating owner or captured current host cancels, with version/retry checks |

Existing room APIs create/join a private lobby, manage readiness and provide reconnection snapshots.
This is game-scoped sharing of the chosen diagram only: it does not grant access to the owner's
lesson text, uploads, other visuals or account data, and a parent label grants no extra authority.

## Flow on separate phones

1. Each player signs in as a different account (a different `player_id`) and joins the same room. Two tabs signed
   in as the same account are one participant.
2. The host selects an existing owned diagram from an approved lesson:

```json
{"lesson_id":"OWNED_LESSON_ID","visual_id":"SAVED_VISUAL_ID"}
```

3. `POST /templates` returns the ID, source hash, renderer metadata and slot count. It does not
   generate content or return the answer map. Source changes during rendering reject the result.
4. The host creates a race with the current lobby version:

```json
{"template_id":"TEMPLATE_ID","room_id":"ROOM_ID","expected_room_version":4}
```

5. The 2–8 members present at creation form the fixed roster. Later joiners cannot read or play that
   race; they wait for a new round. All captured participants can discover it through the room race
   route, study the frozen labelled SVG, then mark themselves ready using the existing room command.
6. The creating host sends `{"expected_version":1}` to `/start`. Every captured player must still
   be in the room and ready. One active diagram race per room is allowed. A stale waiting game can
   be replaced after its creator loses host status. Captured host transfer supports cancellation;
   arbitrary spectators do not gain game access.
7. The server returns one `started_at` five seconds ahead and one 15-minute operational deadline.
   Both are server epoch milliseconds. Board data can preload during `countdown`; the future client
   must hide/disable play until that start instant. Backend placement/finish writes before it are
   rejected. No client timestamp is accepted. Poll room/race snapshots around every 1–2 seconds;
   no game-state WebSocket/Realtime broadcasts are added in this release.
8. Both phones receive the same frozen diagram and the same randomized label order for fairness.
   Each player has separate opaque card IDs, placements, version, receipts and finish time. A repeat
   race reuses the frozen source but draws a fresh shared tray order.

## Drag/drop contract and grading

The board returns `puzzle_svg`, its `view_box`, and slots `{id,x,y,width,height}` in that SVG's
coordinate space. `data-slot-id` marks the same placeholders in the SVG. Cards are `{id,text}`.
Only the fixed question is supplied; source title/caption/alt are not extra quiz hints. The server
does not stream or judge pointer movement: submit the semantic slot/card mapping on drop or in a
debounced batch.

```json
{
  "expected_version":1,
  "placements":{"SLOT_ID":"MY_CARD_ID"}
}
```

Mapping keys must be actual slots and values must be the caller's card IDs. One card cannot occupy
two slots. Partial mappings are allowed. Unknown/foreign/duplicate cards return 422; stale player
versions and conflicting retries return 409. Responses include the updated player version and
placed count. The same key/body returns its saved response, which may be older than current board
state; refetch after reconnect or a version conflict. Cards and tray order remain stable on resume.

`POST /finish` accepts only `{"expected_version":2}`. Every slot must be filled. The server compares
the actual label text for each slot, not a client correctness flag. Identical repeated labels are
interchangeable, so swapping two equal-text cards remains correct. No fuzzy, semantic or AI judging
is used.

An incorrect finish reports the number correct and the caller's incorrect slot IDs, increments the
submission/version counters and leaves the clock running. It does not reveal the expected labels.
The player can correct those slots and submit again. A perfect reconstruction records the first
server finish time and elapsed milliseconds; later writes cannot replace that result. Exact prior
idempotent receipts remain replayable, without double-finishing. Receipt storage is capped at 1,000
actions per participant; quota failure never silently removes replay protection.

## Results, learning and competitive limits

Study closes as soon as the host starts the countdown. Full solution/review is unavailable while
other attempts are running. Results unlock when everyone completes/forfeits, or at the 15-minute
deadline. Only perfect completions receive a rank. Equal elapsed milliseconds share a rank and
winner status. An unfinished/forfeited attempt cannot beat a correct diagram; no coin/reward,
mastery or unrelated progress grant is added.

Review returns the same frozen labelled reference plus correct slot/text pairs. Host creation of
another race with a new idempotency key provides repeat practice without model generation. Room
readiness, discovery and the study phase can be used again. Leaving/disconnecting never pauses the
clock; current membership is checked on every request. A departed player may reconnect/rejoin if
their captured participation still exists. Nonmember display names are masked as “Player left”.

This is a casual learning race, not a trusted global ranking or a pure cognition benchmark. Times
include network/request effects. The source owner already has access to their original lesson;
preloaded board data and the study copy cannot be an anti-cheat boundary. Owner confirmation is
not independent factual, guardian or safety verification. The backend does not change those AI or
identity policies.

## Data isolation, deletion and performance

Migration 008 creates private templates, races, participants and idempotency records. RLS is on and
public/anon/authenticated table access is revoked. Backend controller code checks template
ownership, current room membership, fixed participant identity, host authority, phase and versions.
Only private templates contain the answer mapping. Shared snapshots expose progress counts, not
other players' placements, tray IDs, receipts or answers.

Templates have a foreign key to the owned source lesson; lesson/material/account deletion cascades
to its templates, races and player state. Account-data deletion also removes that user's participation
in other owners' races through a caller-bound private function. Mutation lock order follows room
then race, and updates never upsert a deleted player. Tests cover concurrent finish/account deletion
and source deletion during rendering so data cannot be resurrected.

GET operations are read-only and avoid row locks. Mutations use short locks and existing bounded
control pools/timeouts. Snapshot polling projects away SVG bodies and large card/placement/receipt
payloads; the full SVG is loaded only for board/study/review. Rendering holds no DB transaction and
has a bounded per-process concurrency limit. Size, label count, source length and process time are
bounded. Existing application request limits apply to every game request.

Expiry does not automatically erase snapshots/history. Operational pruning, source-retention policy
and expired-room/race cleanup need an explicit reviewed schedule. The game adds no automatic
schoolwork deletion policy. Old IDs stop working if their source is deleted or the room expires.

## Frontend and hosted work still needed

No frontend was changed. The new game needs a diagram selector/study view, shared lobby/countdown,
SVG placeholder hit targets, keyboard-accessible card placement, progress/result screens and a
review/repeat flow. Slots are structured for alternate controls, but no screen-reader/nonvisual or
real-device UI acceptance is claimed. Current frontend proxy wiring must allow this API prefix,
forward the Bearer token and Idempotency-Key, and support PUT; its existing learning proxy does not do all of
these. Render the sanitized static SVG safely, without scripts or network.

Hosted migration/runtime setup, sandbox verification, real multi-device latency/disconnect testing,
live Supabase/transport acceptance and deployment remain separate operator work. AI generation,
legacy harness behavior, account activation and global ranked/anti-cheat/reward systems are untouched.

## Test coverage

API tests use saved synthetic source and frozen SVG fixtures; generation/checker calls fail if
invoked. They cover both memory and PostgreSQL, ownership/room/roster isolation, common start,
tray independence/fair order, wrong/perfect/tied results, stale/duplicate actions, forfeit/cancel,
discovery/repeat, host transfer, deadline, raw SQL permissions, restart and deletion races.

Real headless tests cover linear/branching flowcharts, edge and subgraph labels, repeated words,
Unicode and multiline labels. They compare all non-text geometry before/after removal. Unit tests
reject active SVG/external references and check metadata redaction, CSS colour preservation and
credential-free renderer subprocesses. CI adds a separate `diagram-renderer` job; existing AI and
frontend files are not changed.

### Completed local verification — 24 October 2026

Core suite: **686 passed, 25 skipped** on Python 3.13 and disposable PostgreSQL 16. The
dedicated real-browser check passed **15 tests** separately. Core skips include 18 memory
variants of database-only checks whose PostgreSQL cases ran, one opt-in paid safety-model
check, and six browser cases run in the dedicated renderer check. Diagram race tests cover
36 passing cases and one PostgreSQL-only memory skip. Lint passed; all 79 Python files passed
formatting. Node syntax and OpenAPI references passed. npm audit reported zero advisories for
the pinned new renderer dependency tree. No real model, SMTP, hosted migration or deployment
was used. Existing frontend and AI/harness source files are unchanged.

The renderer targets a managed Linux/macOS backend with POSIX process supervision. Hosted
sandbox/runtime and real multi-phone UI acceptance remain required before enabling it.


## First lifecycle/readiness fix batch — 25 October 2026 addition

The [lifecycle/readiness supplement](DIAGRAM_LIFECYCLE_READINESS.md) adds migration 009, terminal
result protection, atomic active-source invalidation, minimal current-host management and cached
synthetic renderer preflight. These rules supersede the earlier cancellation/source-readiness
descriptions without rewriting them. Explicit leave/rejoin and room-ready behavior are unchanged.
The new [OpenAPI supplement](diagram-lifecycle-readiness.openapi.json) has 15 diagram operations
and readiness. No frontend, AI/harness or hosted configuration changes are included.
