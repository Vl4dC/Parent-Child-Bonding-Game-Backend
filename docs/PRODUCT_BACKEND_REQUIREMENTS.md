# Backend requirements moved from the product plan

For implemented API behavior, read [API_PLATFORM.md](API_PLATFORM.md). The source extracts below
are historical product requirements; their future-tense wording is not a current delivery inventory.

These requirements were recorded in the frontend product plan at `bad7e5a`. They describe
planned behavior, not completed endpoints. This relocation does not start feature implementation.
Use FastAPI and Supabase; confirm that each feature is finished and stable with the user before
implementing it. Review the current main frontend repository before defining contracts.

## Original requirements and decisions

g. **Stack:** Figma for design, Next.js with React/TypeScript and shadcn for the interface, Python/FastAPI for custom API logic, and Supabase for authentication, storage, and its PostgreSQL database. PostgreSQL is part of Supabase, not an additional database service;

a. Use **Supabase as its backend**;

c. Keep learner display and playback preferences in Supabase when account-based persistence is needed;

g. Apply daily and weekly quests across compatible game modes. Record completion, points, badge progress, earned item boxes, and avatar customisation. Named local players are not automatically authenticated accounts: define reward ownership before persisting individual or team rewards;

Use Supabase as the backend for the implemented subset of accounts, optional team links, reviewed content, sessions, quest progress, and reward inventory. Schema design, account ownership, access rules, and multiplayer synchronisation remain implementation decisions. Secret-role games must hide other players' cards during hand-off; future multi-device implementations must not expose other players' secrets.

e. One sample quest and an earned cosmetic-box flow, with reward ownership defined before persistent inventory is built;

f. Supabase persistence for the selected flow, including protection against granting the same completion reward repeatedly.

**Status:** Reviewed product plan. Supabase, flexible friends/family participation, the party-game catalogue, learning blocks, quests, and reward concepts are recorded. Demo selection, owners, deadlines, and reward parameters remain open. No application implementation or compliance is claimed.

Before implementation, select the initial subject and difficulty within the confirmed 10–18 audience; the reviewed-content owner; final demo games and accessibility paths; local-player versus account reward ownership; scoring and reward parameters; account/access model; and task owners with hackathon deadlines. The full catalogue needs mode-specific player counts and scoring rules before each additional mode is built.

The target age is now confirmed as 10–18. The upload-to-study-plan workflow and Figma/Next.js/FastAPI/Supabase stack are recorded. Remaining product decisions include module-to-game unlock rules, whether both partners must finish, casual versus ranked matches, and the content review owner. These are additions to the unresolved delivery and reward decisions above.

## Ownership boundary

Backend code, configuration, tests, CI, setup, API contracts, and persistence/access design belong
in this repository. Product journeys and frontend screens remain in the main repository.
Existing main-repository PR #2 contains the legacy backend/demo work already included here;
merging that version into the frontend unchanged would restore the removed backend directory.
