# Game backend roadmap

Private lobby infrastructure exists. Active gameplay, scoring, rewards, and public matchmaking
remain pending rules and implementation. This document records requirements; it does not advertise
implemented endpoints. The current optimization work is server-only and preserves the API contract.

## Forbidden Clues / Taboo + heads-up

Use Fachbegriffe from reviewed school material. The guessing player holds a phone horizontally
against their forehead with its screen facing a peer. The peer sees the active term and forbidden
words, then explains it while avoiding those words. The player guesses the term.

Proposed controls: tilt down means correct; tilt up means skip. The motion/orientation sensor work
belongs to a future frontend change. The backend receives semantic actions, not raw sensor streams.
Peer confirmation supports casual correctness/forbidden-word judgments. A tilt is not evidence of a
correct answer, and automatic speech/AI judging is not included.

### Pending server contract

Define and freeze the game rules before implementing active gameplay. Each proposed action carries:

- A unique action/idempotency ID.
- Match, active round, and active card IDs.
- The semantic action `correct` or `skip`.
- A state version if required by the finalized concurrency contract.

Derive the actor from a verified session and a server-issued device/player binding. In one atomic
operation, verify membership, the active guessing device/player, phase, card identity, and the
server-owned deadline. A client timestamp cannot extend a round. Record an accepted card result
once, then return the next authorized current-card view/state. Do not distribute future cards or
secret terms in shared room snapshots.

Duplicate action IDs must never advance or score twice. Proposed retry semantics: return the
recorded response for an exact replay, reject a conflicting replay, and reject stale card/round
IDs. Final status codes and retry semantics must be confirmed with the game rules.

The current card is intentionally displayed on the guessing phone for the peer to read. This is
casual play with peer judgment, not a trusted ranked correctness or anti-cheat mechanism.

### Required API tests before delivery

1. Exact duplicate actions do not create another result or advance another card.
2. A reused ID with changed action/card data is rejected.
3. Stale cards/rounds, expired deadlines, and incorrect phases are rejected.
4. A nonmember or wrong guessing device/player cannot submit the action.
5. Concurrent correct/skip actions for one active card produce only one accepted result.
6. Replies expose only the authorized current card; shared snapshots contain no future secrets.
7. Peer-reported correctness and violations are recorded according to the confirmed casual rules.

### Frontend handoff context — future work only

Use motion/orientation sensors (accelerometer/gyroscope), not compass heading. Start permission
requests from a user Start tap over HTTPS. Calibrate a neutral forehead position, account for
landscape orientation, require a return to neutral before rearming, and debounce/dwell gestures.
Always offer Correct/Skip buttons when motion is denied, unavailable, or uncomfortable.

An initial tuning suggestion is about 50 degrees held for 200 ms with a cooldown. This is not a
frozen threshold: validate on real Safari/iPhone and Chrome/Android devices before selecting it.
No frontend sensor code or device validation is included in this server work.

## Decisions still required

Choose term/forbidden-word sourcing and review, players/teams, peer-confirmation rules, skip and
violation behavior, round length, per-card transitions, scoring, disconnect handling, and the demo
subset. Guardian access, rewards, purchases, rankings, and public matchmaking remain separate.
