# Frontend integration

Main repository: https://github.com/RahulRohidasPatil/Parent-Child-Bonding-Game.
Frontend reference inspected on 24 October 2026: main commit
`bad7e5a` (fetch the current default branch before later work).

## Relevant source

| Main-repository path | Context to inspect before designing an API |
| --- | --- |
| `app/onboarding/page.tsx`, `lib/kay/data.ts` | Onboarding and person display data |
| `lib/kay/store.ts` | Local session, team, daily goal, accessibility preferences |
| `lib/kay/content.ts`, `app/(app)/learn/` | Learning modules, flashcards, quizzes |
| `lib/game/types.ts` | Player, settings, phases, secret cards, and room state |
| `lib/game/mock-room.ts`, `lib/game/rules.ts` | Local multiplayer simulation and scoring |
| `app/splash/`, `components/game/` | Room creation/join and game screens |
| `docs/PRODUCT_PLAN.md` | Intended product scope; distinguish plans from shipped code |

Frontend source stays in the main repository. This public backend repository links to it without
duplicating frontend assets. Access to the main repository is managed independently.

## Authentication integration

The backend's account types are `parent` and `learner`. The Splash profile uses `grownup` and `kid`;
map those display choices explicitly if an integration needs them. Neither value grants permissions
or establishes a verified guardian relationship. Frontend onboarding stores are prototype data,
not authenticated server identities.

Authenticate through `/auth/login`, retrieve identity with `/auth/me`, and serialize refresh calls.
Replace both tokens after refresh and clear the client session after logout or unrecoverable 401.
Supabase access JWTs may remain valid until expiry after refresh-session logout.

This API returns Bearer tokens and sets no cookies. Choose a secure browser integration deliberately;
a same-origin server-side session layer can keep refresh tokens out of browser storage. Do not copy
the prototype's localStorage session persistence for real credentials. CORS origins must match exactly.

## Future endpoint design

No room, profile, learning, progress, reward, or family-link API is implemented yet. Define each
contract from current frontend source and document authorization before adding persistence.

For multiplayer, the mock's `RoomState.secret` is server-only truth. A real API must project a
per-player view and send an imposter only the imposter card/category, never the secret word before
the result phase. Authenticate membership, derive actor identity from the session, validate phase
and turn transitions, and calculate votes/scores on the server. Do not expose the entire mock state.

Server-backed game storage and synchronization must support multiple processes before running
multiple workers. Existing demo authentication is deliberately single-process and is not such storage.
