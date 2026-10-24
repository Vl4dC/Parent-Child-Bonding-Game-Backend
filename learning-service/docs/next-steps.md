# Historical learning-service backlog

This imported backlog predates integration into the API repository. Its OneDrive, missing-git,
SQLite, and nickname-only identity statements do not describe the current application. Use the root
README, docs/API_PLATFORM.md, and docs/DATABASE.md for implemented behavior and remaining decisions.
The original notes below remain as historical context, not authorization to implement every idea.

# Next steps

As of 2026-10-24. Sizes are rough: **S** small, **M** medium, **L** large. **?** means I need a decision from you first.

## Recommended order
1. **A. Protect what exists.** Before more people use it.
2. **B. Tournament.** It is the product's headline and is still a placeholder.
3. **C1. Re-teach before re-testing.** The biggest learning gain for the least work.

## A. Protect what exists (do first)
- [ ] **S** Put the project under git with a `.gitignore` (`.env`, `.venv`, caches). It isn't a repository, so nothing can be undone.
- [ ] **S** Set a spending limit on the OpenRouter key. The key sits in `.env` inside a OneDrive folder, so it is probably synced to the cloud: move the project (or just `.env`) out of OneDrive. That also fixes `--reload` and the slow `.venv` sync.
- [ ] **M** Access control before sharing wider: an access code plus a per-address rate limit on the model-calling and upload endpoints. Today anyone on the Wi-Fi can spend the key.
- [ ] **M** Persistence (SQLite): lessons, quizzes, decks, uploads and answers survive a restart. Today a restart wipes every learner's session.
- [ ] **S** Bundle the libraries the page loads from a CDN (Swagger UI, Mermaid, Vega). Flaky venue Wi-Fi currently breaks diagrams and `/docs`.
- [ ] **S** Move the browser test scripts into the project (they live in a temp folder), add one command that runs everything, and a real-model smoke test.
- [ ] **S** Find out why the very first browser test run stopped silently (never explained; three later runs passed).

## B. Tournament and gamification (?)
- [ ] **?** Decide the rules: solo vs head-to-head vs whole class, scoring, timers, streaks, power-ups, what the leaderboard shows.
- [ ] **M** Learner identity: nickname plus a code, no passwords. Needed for leaderboards and progress.
- [ ] **M** Record tournament answers so they feed weak spots like practice quizzes do (they don't today).
- [ ] **M** Scoring engine (points, speed bonus, streak multiplier) with tests.
- [ ] **M** Live leaderboard (polling or server-sent events).
- [ ] **L** Tournament screens. Rules first, since the UI will be redesigned.

## C. Better learning
- [ ] **M** **C1.** Re-teach before re-testing: a short mini-lesson (with a picture) for each weak spot, then the follow-up quiz. Today we only re-quiz.
- [ ] **M** Spaced repetition across days for flashcards (needs A persistence and B identity).
- [ ] **M** Adaptive difficulty: the next quiz gets harder or easier from past results.
- [ ] **M** A visual checker pass: confirm a widget's logic matches its brief (the lever example showed a widget that runs but teaches wrong physics).
- [ ] **S** Book-based questions cite the page ("see page 42"), and the quiz sees the original pages, not just the lesson summary.
- [ ] **M** (?) Teach in the learner's language, for books that aren't in English.

## D. Uploads, round 2
- [ ] **M** Big scanned books: read only the pages a request needs (today the first 100 picture pages are read).
- [ ] **M** EPUB books.
- [ ] **L** Rebuild a book's figures as diagrams so a box can be hidden (declined for v1).
- [ ] **S** HEIC photos (iPhone) and password-protected PDFs (ask for the password).

## E. Students' safety and trust (?)
- [x] **M** Age-appropriate guardrails on what the AI writes for under-18s (`learning_service/safety.py`, see `learning-service/docs/superpowers/specs/2026-10-24-child-safety-guardrails.md`). Still open: a bigger red-team set (about 30 prompts per age band), checking text inside finished widgets, and a real age from the wider app instead of a typed number.
- [ ] **S** Privacy notice: book text and photos go to the model provider; consent for under-18s; "your own copy only".
- [ ] **S** Retention: once there is persistence, add delete-my-data and auto-expiry.

## F. Polish and later
- [ ] **L** UI redesign: mobile, accessibility, auto-sized diagrams, readable wide diagrams.
- [ ] **L** Teacher or parent view: class progress and the most common weak spots.
- [ ] **M** Show what each lesson or learner costs, and log model calls.
- [ ] **S** Show progress ("reading pages", "writing lesson") instead of a 20-40 second spinner.
