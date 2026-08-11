# Backend → Frontend Integration Plan (2026-08-11)

Status: planning only, nothing in this document has been built yet. Written
in response to the open question left at the end of the backend build: the
frontend (`frontend/`) is complete and works entirely off static JSON
fixtures in `frontend/public/mock/`; the backend (`backend/`) is a complete,
independently-tested set of pure-Python modules (`scoring.py`,
`projections.py`, `matchup.py`, `news.py`, `backtest.py`) that has never been
wired to anything. This plan is the missing piece between the two.

## What's already true (no decision needed here)

- The frontend was built anticipating exactly this swap. `frontend/src/lib/api.js`'s
  header comment says it directly: "Later they will call a real backend
  endpoint that returns the identical shape (WeeklyReport / TrackRecord) --
  no view-layer changes required at that swap." The five `getX()` functions
  in that file are the entire integration surface — nothing else in the
  frontend touches data-fetching.
- `news.py`'s `summarize_player_news()` output already matches the
  frontend's `newsFlag` shape exactly (`designation` / `riskLevel` /
  `summary`) — confirmed earlier in this build. No translation layer needed
  there.
- Team configuration (`slotAssignments`, which player sits in which roster
  slot) is explicitly client-side/localStorage-only per
  `docs/specs/team-config-and-roster-status.md` §6 — "No backend exists and
  none is being added." This plan does not touch `getRosterSlots()` or
  `getDefaultTeamConfig()` beyond noting that the *data* those fixtures
  contain (real roster slots, a real default lineup) will eventually need
  to come from the real league once ESPN access lands. That's a data
  problem for later, not a wiring problem this plan needs to solve.

## The architectural question, and a recommendation

`docs/specs/user-journey-frontend.md`'s acceptance criteria call for the app
to build to a static bundle, "deployable to Vercel with no server runtime
required for v1." `CLAUDE.md`'s Suggested Architecture section, on the other
hand, describes "a lightweight Node/Python service." Read literally these
sound like they're in tension. They aren't, once "service" is read as "the
thing that produces the data" rather than "an always-on API the deployed
frontend calls at request time."

**Recommended for v1: a generation script, not a live API.** Add
`backend/generate_report.py` that runs the full pipeline — pull current
player data, score, project, fetch and summarize news — and writes out JSON
files in the *exact* shape the frontend already expects, into
`frontend/public/mock/` (or a renamed `frontend/public/data/`, see the
open question below). This is run manually or on a schedule (a GitHub
Action once this is worth automating; by hand for now, similar to how
`backtest.py` is run today) before each deploy, or once a week during the
season. `api.js` needs zero code changes for this — its fixture-fetching
pattern was already written to tolerate exactly this kind of swap, per its
own doc comment quoted above. The static-bundle deployment story stays
intact: Vercel still serves a static site, it's just serving real generated
files instead of hand-written mock ones.

**Considered and deferred: a live API/serverless runtime.** A FastAPI
service (as Vercel serverless functions, or a small standalone deployment)
that `api.js` calls at request time instead of fetching static files. This
is the more "real" version of the architecture and is where this probably
ends up eventually — no manual regeneration step, always-fresh data on
load. It's deferred for now because it adds real infrastructure work this
project doesn't need yet for its current purpose (a portfolio piece
demoed against a given week's snapshot): hosting, CORS, and — importantly —
moving `ANTHROPIC_API_KEY` server-side instead of a local environment
variable. None of that blocks anything else in this plan; `generate_report.py`'s
internals (pull → score → project → summarize) are exactly what a future
API handler would call too, so building the script first is not wasted
work if the live-API version gets built later.

## Data contract mapping

What each frontend fetch function needs, and what's missing to produce it
for real, based on reading the actual fixture shapes in
`frontend/public/mock/*.json` (not just the specs — the shipped fixtures
are ground truth and the specs describe an older, partially-superseded
shape in places).

**`getWeeklyReport(week)`** → `weekly-report-week-N.json`. Top-level:
`week`, `leagueFormatAssumption`, `generatedAt`, `projections[]`,
`waiverTargets[]`. Each `projections[]` entry needs `playerId`, `name`,
`position`, `team`, `opponent`, a `projection` object (`tier`, `points`,
`tierLabel`, `source`), and `newsFlag`. `scoring.py` + `projections.py` +
`news.py` cover the math and the news object. Two real gaps: (1) nothing
decides *which* players get the `"consensus"`/FantasyPros tier versus the
`"in_house_estimate"` tier — that's the "top-10 at the position use
FantasyPros, everyone else uses the in-house model" rule from `CLAUDE.md`,
and it needs both a selection function and an actual FantasyPros data pull,
neither of which exist in `backend/` yet. (2) `waiverTargets[]` — which
players get surfaced, and the free-text `rationale` field — has no
selection or generation logic anywhere yet.

**`getTrackRecord(season)`** → `track-record.json`. Top-level: `season`,
`asOfWeek`, `summary` (per-tier `predictionsScored` / `startSitHitRate` /
`meanAbsoluteError`), `history[]` (per-prediction records with
`predictionId`, `tier`, `recommendationType`, `predictedPoints`,
`actualPoints`, `outcomeCorrect`). `backtest.py` already computes very
similar metrics (`aggregate_metrics()`), but for a *historical*
train/holdout split, not a running in-season "how did we do so far this
year" view, and its internal shapes don't match `TrackRecord`'s schema
(no `predictionId`/`recommendationType`/`outcomeCorrect` concept exists in
`backtest.py` today). This needs a new function — reusing `backtest.py`'s
scoring/comparison logic, restricted to completed weeks of the *current*
season, reshaped into the fixture's exact keys.

**`getRosterSlots()` / `getDefaultTeamConfig()`** → not touched by this
plan; client-side/localStorage per the spec. The only future work here is
sourcing real slot names and a real default lineup once the league's
actual roster settings are known — a data-content change to those two
fixtures, unrelated to backend wiring.

**`getPlayerPool()`** → `player-pool.json`, `{"players": [...]}`, each with
`playerId`, `name`, `position`, `team`, `newsFlag`. Broader than one week's
`projections[]` — this is the full-roster identity + current-status list
used for team-config's picker and as the Weekly Report's fallback lookup.
Producible by running `news.py` over the full player pool and joining
identity fields from `nflreadpy`'s roster data; no new logic needed beyond
generation-script plumbing, once the player-ID question below is settled.

## The player-ID gap

The mock fixtures use an invented, opaque ID scheme (`p_00123`, `p_00456`,
...). Real backend code uses `nflreadpy`'s `player_id` (gsis-style IDs like
`00-0033280`) — a different format with no existing mapping between the
two. This matters because `p_00123`-style IDs are also what gets written
into a user's localStorage-saved `slotAssignments` on their own machine.

Two ways to close this gap: maintain a permanent crosswalk table
(`p_00123` → real `player_id`) inside `generate_report.py`, so the
frontend-facing ID scheme never changes and any already-saved localStorage
config keeps working; or drop the invented scheme and make the real
`nflreadpy` `player_id` (or ESPN athlete ID) the frontend's player identity
going forward. Note that `db_playerids.csv`, mentioned in `backend/README.md`,
is a *different* crosswalk — vendor-to-vendor (ESPN ID / nflreadpy ID /
FantasyPros ID) — and doesn't solve this problem on its own; it would only
help populate a mapping table, not replace the need for one.

Recommendation: switch to real IDs directly rather than build and maintain
a permanent mapping layer. This project has no real users with saved data
yet (still pre-launch, blocked on the commissioner), and
`team-config-and-roster-status.md` §7 already establishes precedent for
documented breaking changes between iterations. A one-time "player IDs
changed, your saved lineup will need to be re-picked" is a much smaller
cost right now than carrying a translation table indefinitely. This should
be confirmed as a decision (not just assumed) before `generate_report.py`
is written, since it changes what the script's core join key is.

## Orchestration script shape (`backend/generate_report.py`)

1. Load the current player pool (once ESPN roster access exists; until
   then, `nflreadpy.load_rosters()` as an interim stand-in source — real
   ESPN league membership isn't required to produce projections for the
   general player pool, only for whichever roster slots are the specific
   league's).
2. Per player: pull recent game log (`projections.load_recent_games_nflreadpy`),
   compute the rolling projection (`projections.project_player`), decide
   tier (new logic, gap #1 above), and run `scoring.compute_league_points`
   under `scoring_config.placeholder.json` (swap-in-place once real config
   lands — no script change needed there, by design).
3. Assemble `projections[]` and select+write `waiverTargets[]` (new logic,
   gap #2 above).
4. Fetch and summarize news for the full pool (`news.fetch_espn_news` +
   `summarize_player_news`) — needs `ANTHROPIC_API_KEY` set locally, same
   as today.
5. Write `weekly-report-week-N.json` and `player-pool.json` in the exact
   shapes confirmed above, into `frontend/public/mock/` (name TBD — could
   stay `mock/` or move to `data/`; either works, `api.js` just needs its
   path constants updated to match whichever is chosen).
6. Separately (can run on the same cadence or independently): a live-season
   track-record aggregation step producing `track-record.json`, once that
   logic (gap above) exists.

## Explicitly out of scope here

- `matchup.py`'s opponent multiplier — confirmed via backtest to add no
  accuracy value and a real bias cost; stays built, tested, and unwired.
- DST scoring — not implemented; a separate, already-scoped chunk of work
  (`docs/research/dst-scoring-fields.md`).
- Team config / roster slot assignment — stays entirely client-side per
  spec; this plan adds nothing there.
- The real league scoring config — still a placeholder pending the
  commissioner. Swapping `scoring_config.placeholder.json` for real values
  is a one-file change that's fully independent of everything in this
  plan and can happen whenever the commissioner responds without touching
  any orchestration code.

## Gap list — new work this plan requires before it can run end-to-end

1. Player-identity decision (crosswalk vs. switch to real IDs) — blocks
   everything downstream, should be decided first.
2. Tier-selection logic + an actual FantasyPros data pull (nothing in
   `backend/` talks to FantasyPros today).
3. Waiver-target selection + `rationale` text generation.
4. Live-season track-record aggregation (distinct from `backtest.py`'s
   historical train/holdout mode).
5. `generate_report.py` itself — the script tying the above and the
   existing scoring/projections/news modules together.
6. A full player-pool/roster source — `nflreadpy` rosters as an interim
   stand-in until real ESPN league access exists.

## Suggested build order

1. Settle the player-ID question (recommendation above: switch to real
   IDs).
2. `generate_report.py` v0: wire existing `scoring.py` / `projections.py`
   / `news.py` end-to-end for `getPlayerPool()` and a `projections[]` list
   with **everyone in the `in_house_estimate` tier** — deliberately skip
   FantasyPros integration for this first pass. The backtest already
   validated the in-house model specifically (window=4, no matchup
   multiplier); shipping 100% in-house first is a reasonable simplification
   that avoids blocking the whole integration on a new data source, and
   can be narrowed later once tier-selection logic (gap #2) is built.
3. Waiver-target selection + rationale (gap #3) — can ship as a simple
   templated rationale ("trending up in target share" style) before any
   LLM-generated version, if that's faster to get working end-to-end.
4. Live-season track-record aggregation (gap #4).
5. Run `generate_report.py` once locally, manually diff its output against
   an existing mock fixture's shape (keys, types, nesting) to catch drift,
   then replace one real week's fixture as a trial before trusting it for
   all weeks.
6. v2, later: revisit the static-generation vs. live-API decision now that
   the pipeline exists as reusable, tested code — a live endpoint at that
   point is mostly a thin handler around what `generate_report.py` already
   does.

## Open question to confirm before starting

Frontend-facing player-ID scheme: keep `p_00123`-style invented IDs behind
a permanent crosswalk, or switch to real `nflreadpy`/ESPN IDs directly and
treat existing mock-fixture and any already-saved localStorage data as
disposable? This plan recommends the latter, but it's a real product
decision (not just a technical one) worth confirming explicitly before
`generate_report.py`'s join logic gets built around it.
