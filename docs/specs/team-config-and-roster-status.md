# Spec: Team Configuration, Universal Player Status, Waiver Exclusion (v1)

Status: Draft — builds on `docs/specs/user-journey-frontend.md` (frontend/journey spec, "v1").
Depends on: existing frontend (`frontend/src/`), existing mock fixtures (`frontend/public/mock/`).
Supersedes (fixture shape only): `user-journey-frontend.md` §3.2's `startSit.start`/`startSit.sit`
grouping — see §7 "Breaking changes from the prior spec" for why and exactly what changes.

## 1. Problem / User Story

As the builder, the app currently shows a hardcoded "start/sit" roster baked into each week's mock
fixture — it isn't *my* team, I have no way to tell it who's actually on my roster, and most players
show no status at all (only flagged players render a tag). I need to manually configure my actual
12-slot roster once (CLAUDE.md's documented v1 mechanism — pulling the live roster from ESPN's API is
explicitly out of scope), see that configured team reflected wherever the app shows "my players," see
an explicit status on every player card everywhere (not just the ones with news), and have Waiver
Targets automatically exclude anyone already on my configured team.

## 2. Assumptions (stated per instructions, not asked as questions)

- **No backend exists and none is being added.** Team configuration is pure client-side state,
  persisted to `localStorage`. This is consistent with CLAUDE.md's "single-user, single-league,
  no auth/multi-tenant scaffolding" instruction and with the parent spec's static-bundle constraint.
- **"My Team" becomes a third tab**, alongside Weekly Report and Track Record, rather than a modal or
  a settings icon. Team configuration is a persistent, revisitable piece of state the user will look
  at as often as the other two views (to check/update status, swap a bench player, etc.) — it earns
  top-level nav, not a buried settings panel.
- **My Team is NOT week-scoped.** It represents the persistent roster (who plays what slot), not a
  per-week snapshot. Weekly projections stay Weekly Report's job (see §4 for the join mechanism).
  This keeps My Team simple: no week selector, no projection numbers, just identity + status + slot
  assignment.
- **Seeding on first run:** before the user has ever touched My Team, the app seeds a default
  configuration from a new fixture, `default-team-config.json`, using the same players/slots the
  current hardcoded week-1 fixture already shows — so first-load behavior is visually unchanged from
  today until the user actively edits their team. This avoids an empty/broken-looking first
  impression while keeping the seed itself just mock data, clearly swappable later.
- **Position eligibility per slot is inferred generically from the slot name**, not hardcoded per
  slot instance, to stay consistent with the existing "generic, swappable slot list" design in
  `roster-slots.json` / `user-journey-frontend.md` §3.3:
  - `QB`, `RB`, `WR`, `TE`, `DST`, `K` slots → exact position match only.
  - `FLEX` → `RB`, `WR`, or `TE`.
  - `BENCH` → any position.
  - `IR` → any position (added 2026-09-03, 2 slots per league). A real IR slot constrains by
    **status**, not position — only an injured player may occupy one — and status eligibility is a
    concept this layer does not have; `eligiblePositions()` answers a position question. Enforcing
    it off `newsFlag.designation` was considered and rejected: that feed is a live scrape, empty for
    most of the pool pre-season, so a wrong "not eligible" would lock the user out of a slot on the
    strength of a missing field. Left unconstrained and documented.
  - If the slot config ever adds a new slot name not covered above, treat it as `BENCH`-style (any
    position) rather than rejecting all players — documented as a fallback rule, not expected to be
    hit with today's fixture.
- **A slot-array shape change must not be applied by index** (learned the hard way, 2026-09-05).
  `slotAssignments` maps to `slots` by index, so inserting a slot mid-array shifts every later
  assignment one place right and renders the wrong label against the right player — a D/ST under a
  FLEX heading, a kicker under DST, a bench QB under K. Two defences, both required:
  1. The storage key carries a version suffix; bump it on any shape change so stale configs are
     treated as absent and re-seeded (`v1` → `v2` on 2026-09-05, after two shape changes went out
     without it).
  2. From `v2`, a saved config is **stamped with the slot array it was written against**. On load,
     a stamp that no longer matches the current slots is re-mapped **by slot name**, not by index;
     a player whose slot no longer exists falls to a free `BENCH`, then `IR`, rather than
     vanishing. This is what makes the next shape change safe without another bump.
- **`BENCH` and `IR` are the non-starting slots** (`NON_STARTING_SLOTS` in `lib/teamConfig.js`).
  Weekly Report must exclude both from its starting-slot groups, and must not merge them into one
  another: a bench player is startable and simply is not started this week, an IR player cannot be
  started at all. IR gets its own de-emphasized section, rendered only when an IR slot is occupied.
- **Slots support a swap (`Move`), not just assign/clear** (added 2026-09-03). `swapSlots(a, b)`
  exchanges two slots' contents and persists once; an empty target makes it a move.
  `canSwapSlots(slots, assignments, poolById, a, b)` gates it and checks eligibility in **both
  directions** — moving a WR into FLEX is legal, but the RB coming back the other way must be legal
  for the WR slot he lands in. A one-way check would make swap a hole through which any player
  reaches any slot. A player assigned but absent from the pool has no known position, so the swap
  is refused rather than guessed.
- **Track Record is explicitly NOT changed by this spec.** "if applicable" (from the task) resolves
  to *not applicable*: Track Record's `history` rows are a log of past predictions vs. outcomes
  (predicted points, actual points, correct/incorrect). Attaching a *current* player status to a
  historical row would conflate "how healthy is this player today" with "what happened that week" —
  two different points in time — and adds noise to the "10-second legible" summary view without
  answering a question anyone asks of a track record. See Non-Goals.
- **No drag/drop or one-step "swap" UX.** Moving a player between slots is remove-then-assign. Stated
  explicitly as a scope boundary, not an oversight.

## 3. Data Model

### 3.1 Universal player status (extends the existing `newsFlag` shape, does not replace it)

Today `newsFlag` is nullable/omittable and only present for players with elevated risk; `RiskFlag.jsx`
renders nothing when absent. This spec makes `newsFlag` **mandatory on every player object everywhere**
(weekly projections, waiver targets, player pool) and mandates a `designation` value even in the
default/healthy case — closing the gap without introducing a parallel field.

```json
"newsFlag": {
  "designation": "Healthy",
  "riskLevel": "none",
  "summary": null
}
```

- `designation` enum (closed set, v1): `"Healthy" | "Questionable" | "Doubtful" | "Out" | "IR"`.
  `"Healthy"` is the required default when there is no elevated risk — never omit the field, never
  leave `designation` unset.
- `riskLevel` enum unchanged: `"none" | "low" | "medium" | "high"`. Mapping is illustrative, not
  mechanically 1:1 (existing fixtures already use both `low` and `medium` under `"Questionable"` to
  express finer severity — preserved as-is): `Healthy → none`; `Questionable → low` or `medium`;
  `Doubtful → high`; `Out → high`; `IR → high`.
- `summary` stays optional/nullable — only present when there's a real news blurb (Healthy players
  normally won't have one).
- **Component impact:** `RiskFlag.jsx` must stop returning `null` for the no-risk case. Recommend
  renaming it `StatusTag.jsx` (the component's job is no longer "flag risk," it's "always show
  status") and updating its one caller (`PlayerCard.jsx`) accordingly — a naming recommendation for
  the dev agent, not a hard requirement, but the *always-render* behavior change is required. Healthy
  status renders as a plain, low-emphasis tag with no `<details>` disclosure (no summary text exists
  for it); Questionable/Doubtful/Out/IR keep the existing expandable-summary pattern.

### 3.2 New fixture: `public/mock/player-pool.json`

A broader, week-independent player list — the picker source for team configuration. Deliberately
deeper than the ~15 players per week the current weekly-report fixtures contain, per the task's
explicit ask ("this requires introducing a broader mock player-pool fixture"). No `opponent` or
`projection` fields — pool entries are identity + status only, since projections are inherently
week-scoped and My Team isn't.

```json
{
  "players": [
    {
      "playerId": "p_00123",
      "name": "Josh Allen",
      "position": "QB",
      "team": "BUF",
      "newsFlag": { "designation": "Healthy", "riskLevel": "none", "summary": null }
    },
    {
      "playerId": "p_00512",
      "name": "Zach Charbonnet",
      "position": "RB",
      "team": "SEA",
      "newsFlag": { "designation": "Healthy", "riskLevel": "none", "summary": null }
    }
  ]
}
```

- Should reuse the `playerId`s already used in the weekly-report/track-record fixtures for any player
  that appears in both places (identity is shared across fixtures), plus enough new `playerId`s per
  position to give the picker real depth (roughly 8–10 per offensive position, fewer for DST/K is
  fine).
- **Precedence rule (explicit, to avoid ambiguity):** pool `newsFlag` is illustrative "current status"
  only, shown in My Team. It is never used to override a given week's own `newsFlag` in Weekly
  Report — that week's fixture entry is always the source of truth there. If a player is assigned to
  a team slot but has no entry in a given week's projections (see §3.3), Weekly Report falls back to
  the pool entry's identity/status with an explicit "no projection this week" note (see §4).

### 3.3 Restructured weekly-report fixture (`weekly-report-week-{n}.json`)

`startSit: { start: [...], sit: [...] }` is replaced with a flat `projections` array, and each
entry's `rosterSlot` field is dropped — slot assignment is no longer fixture-baked, it comes from the
user's team config (see §7 for why this is a deliberate breaking change from the prior spec).

```json
{
  "week": 1,
  "leagueFormatAssumption": "half_ppr",
  "generatedAt": "2026-09-04T13:00:00Z",
  "projections": [
    {
      "playerId": "p_00123",
      "name": "Josh Allen",
      "position": "QB",
      "team": "BUF",
      "opponent": "MIA",
      "projection": { "tier": "consensus", "points": 24.3, "tierLabel": "Consensus projection", "source": "FantasyPros" },
      "newsFlag": { "designation": "Questionable", "riskLevel": "low", "summary": "Limited practice reps..." }
    }
  ],
  "waiverTargets": [
    {
      "playerId": "p_00456",
      "name": "Jaylen Warren",
      "position": "RB",
      "team": "PIT",
      "opponent": "CLE",
      "projection": { "tier": "in_house_estimate", "points": 9.8, "tierLabel": "Our estimate", "source": "In-house model" },
      "newsFlag": { "designation": "Healthy", "riskLevel": "none", "summary": null },
      "rationale": "Lead back role trending up with committee-mate on the injury report."
    }
  ]
}
```

- `projections` must cover, at minimum, every player in `default-team-config.json`'s seed roster for
  every week (so the seeded default team always has real numbers), plus enough additional pool
  players to make swapping meaningful in the demo. It does **not** need to cover 100% of
  `player-pool.json` every week — the app must handle the gap gracefully (§4), so full coverage is a
  content nice-to-have, not a functional requirement.
- `waiverTargets` shape is unchanged except `newsFlag.designation` must now always be populated
  (no more bare `{ "riskLevel": "none" }`).

### 3.4 New fixture: `public/mock/default-team-config.json`

One-time seed, fetched only when `localStorage` has no saved config yet.

```json
{
  "slotAssignments": [
    "p_00123", "p_00201", "p_00202", "p_00301", "p_00302",
    "p_00401", "p_00303", "p_00501", "p_00601",
    "p_00203", "p_00304", "p_00402"
  ]
}
```

- `slotAssignments[i]` corresponds to the league's `roster-slots.json` `slots[i]` **by array index, not by slot
  name** — this is the load-bearing design decision that resolves the ambiguity of repeated slot names
  (`RB` appears twice, `BENCH` three times; name alone can't identify which one). `null` means the
  slot is unassigned. Array length always equals `slots.length`.
- Seed values above are exactly today's week-1 fixture's start (9) + sit (3) players, in slot order —
  chosen so first-load looks identical to current behavior until the user edits their team.

### 3.5 `localStorage` schema

Key: `nfl-fantasy-assistant:team-config:v1` (versioned key so a future shape change doesn't need a
migration path — just bump the suffix and treat the old key as absent).

```json
{ "slotAssignments": ["p_00123", "p_00201", null, "..."] }
```

## 4. Weekly Report join mechanism (how Start/Sit is derived — the core of requirement 2)

**Decision: rework, not a separate curated fixture.** Start/Sit is computed client-side by joining
the team config with that week's `projections` array — it is never a second, independently-curated
roster. Reasoning: the task explicitly asked for this ("reflects the *actual configured team*"), and
keeping two disconnected rosters (a "real" configured one and a "fixture" one) would make the team
config feature cosmetic — configuring a team that Weekly Report ignores defeats the point of building
it. The cost is real (§3.3's fixture restructuring, described as a breaking change in §7) but it's the
only version of this feature where configuring your team visibly does something.

Join logic (`WeeklyReportView.jsx`, replacing the current `report.startSit.start.filter(...)` logic):

1. Load `roster-slots.json` (`slots[]`), the current team config (`slotAssignments[]`), and the
   selected week's `projections[]` (as a `Map<playerId, entry>`), and `player-pool.json` (as a
   `Map<playerId, poolEntry>` for fallback identity/status).
2. For each index `i` in `slots`:
   - If `slotAssignments[i]` is `null` → render an **empty-slot placeholder** card: slot label +
     "Empty — assign a player" with a link/action to the My Team tab. Never a blank grid cell.
   - Else look up `slotAssignments[i]` in the week's `projections` map:
     - **Found** → render the normal `PlayerCard` (name, position, team, opponent, tier badge,
       points, status tag) exactly as today.
     - **Not found** (player assigned but this mock week has no projection entry for them) → render
       a degraded card using the `player-pool.json` fallback entry (name, position, team, status tag)
       plus an explicit **"No projection available for this player this week"** note — never a crash,
       never a silently-dropped card.
3. Group by `slots[i]`: any non-`"BENCH"` slot → Start section; `"BENCH"` → Sit section. (Grouping
   logic itself is unchanged from today — only the *source* of each player's slot assignment changes,
   from fixture `rosterSlot` to team config index.)

## 5. Waiver Targets exclusion (requirement 4)

- Runs **client-side**, in `WeeklyReportView.jsx` (or a small shared helper, e.g.
  `lib/teamConfig.js`'s `isPlayerRostered(playerId, config)`), after both the week's `waiverTargets`
  fixture array and the current team config have loaded.
- Filter rule: exclude any waiver-target entry whose `playerId` appears **anywhere** in
  `slotAssignments` — starting slots and bench slots alike (a benched player is still "on my team,"
  obviously not a waiver target).
- **Empty case:** if filtering removes every candidate for the selected week, the Waiver Targets
  section renders the existing `EmptyState` component (already used elsewhere for "no data") with a
  distinct message, e.g. *"No waiver targets available — this week's top candidates are already on
  your team."* — scoped to just that section, not replacing the whole Weekly Report view.

## 6. My Team view (new tab)

**Layout:**
1. Header: "My Team" + a "Reset to default" action (clears the saved `localStorage` config; next load
   re-seeds from `default-team-config.json`).
2. One row per slot, in `roster-slots.json` order:
   - Slot label (`QB`, `RB`, …, `BENCH`).
   - If assigned: a compact player row (name, position, team, status tag via the same `StatusTag`
     component used everywhere else) + "Change" and "Remove" actions.
   - If unassigned: "Empty" state + "Assign" action.
3. "Assign"/"Change" opens a player picker (inline expand or simple modal — implementation detail,
   not prescribed) listing `player-pool.json` entries filtered to:
   - positions eligible for that slot (§2 eligibility rule), and
   - players **not already assigned to a different slot** in the current config (prevents duplicate
     rostering; moving a player requires removing them from their current slot first — see §2's
     no-drag/drop non-goal).
   - Each picker row shows name/position/team/status tag (via the shared component), plus a search/
     filter-by-name input — no projection numbers shown here (pool is week-independent, per §2).
4. Every assignment/removal **auto-saves to `localStorage` immediately** — no separate Save/Cancel
   step. Chosen deliberately for simplicity: each action is a single, individually reversible edit
   (remove/reassign), so a staged-draft-plus-save flow would add UI complexity without a real
   corresponding risk to protect against, for a single-user local tool.

## 7. Breaking changes from the prior spec (`user-journey-frontend.md`), stated explicitly

- §3.2's `startSit: { start: [...], sit: [...] }` shape is replaced by a flat `projections` array
  (§3.3 above). `WeeklyReportView.jsx`'s current slot-grouping logic (filtering
  `report.startSit.start` by `p.rosterSlot === slot`) is replaced by the join described in §4.
- The `rosterSlot` field is removed from per-player entries in the weekly-report fixture — slot
  identity now lives only in the team-config `slotAssignments` array, keyed by index into
  `roster-slots.json`.
- `newsFlag` goes from optional/nullable to mandatory-with-default (§3.1) across all three fixture
  families (weekly-report `projections` + `waiverTargets`, new `player-pool.json`). All three
  existing `weekly-report-week-{1,2,3}.json` files need every `newsFlag` entry that currently omits
  `designation` (i.e., every `{ "riskLevel": "none" }`) updated to
  `{ "designation": "Healthy", "riskLevel": "none", "summary": null }`.
- `RiskFlag.jsx` changes from "render nothing when there's no risk" to "always render a status tag"
  (§3.1) — a behavior change to an existing shipped component, not just new code.

## 8. Component / file impact list

- **New:** `frontend/src/components/TeamConfigView.jsx` (My Team tab content, §6).
- **New:** `frontend/src/components/PlayerPickerRow.jsx` (or similar — compact identity+status row,
  shared by the picker and the slot list; distinct from the full `PlayerCard.jsx`, which assumes
  week-scoped `projection` data that pool entries don't have).
- **Changed → recommended rename:** `RiskFlag.jsx` → `StatusTag.jsx`; always renders; used by both
  `PlayerCard.jsx` and `PlayerPickerRow.jsx`.
- **Changed:** `PlayerCard.jsx` — no structural change required beyond the `StatusTag` behavior change
  (it already passes `newsFlag` through).
- **Changed:** `WeeklyReportView.jsx` — Start/Sit grouping reworked per §4; Waiver Targets filtering
  added per §5; both now depend on team config + player pool in addition to the week's report.
- **Changed:** `App.jsx` — add a third tab, `{ id: 'my-team', label: 'My Team' }`.
- **New:** `frontend/src/lib/teamConfig.js` — `localStorage` read/write + slot-eligibility helpers
  (see §9 for signatures).
- **Changed:** `frontend/src/lib/api.js` — add `getPlayerPool()` and `getDefaultTeamConfig()`,
  following the existing `fetchJsonFixture` pattern.
- **New fixtures:** `public/mock/player-pool.json`, `public/mock/default-team-config.json`.
- **Changed fixtures:** `public/mock/weekly-report-week-{1,2,3}.json` (restructured per §3.3 and 7).
- **Unchanged:** `TrackRecordView.jsx`, `HistoryTable.jsx`, `SummaryCard.jsx`, `track-record.json`,
  `WeekSelector.jsx`, `roster-slots.json` (still the generic slot source of truth), `TierBadge.jsx`.

## 9. API / function signatures

```js
// lib/api.js additions
async function getPlayerPool(): Promise<{ players: PoolPlayer[] }>
async function getDefaultTeamConfig(): Promise<{ slotAssignments: (string|null)[] }>

// lib/teamConfig.js
function loadTeamConfig(slotCount: number): { slotAssignments: (string|null)[] }
  // Sync localStorage read. Returns a null-filled array of length slotCount if nothing saved yet
  // (caller is responsible for triggering the one-time seed from getDefaultTeamConfig() on first run).
function saveTeamConfig(config: { slotAssignments }): void
function assignPlayerToSlot(slotIndex: number, playerId: string): { slotAssignments }
function clearSlot(slotIndex: number): { slotAssignments }
function resetTeamConfig(): void
function eligiblePositions(slotName: string): string[]   // §2 eligibility rule
function isPlayerRostered(playerId: string, config: { slotAssignments }): boolean  // used by §5 filter
```

## 10. Acceptance Criteria

- A third tab, "My Team," appears in the tab nav; switching to/from it requires no full page reload.
- My Team lists every slot from the league's `roster-slots.json`, in order; each shows either an assigned
  player's name/position/team/status tag, or an explicit "Empty — assign a player" state.
- Each slot has an action opening a picker restricted to `player-pool.json` entries eligible for that
  slot's position rule (exact match for `QB/RB/WR/TE/DST/K`; `RB/WR/TE` for `FLEX`; any position for
  `BENCH`).
- The picker excludes players already assigned to a *different* slot in the current configuration.
- Assigning or removing a player persists to `localStorage` immediately; a full page reload preserves
  the configuration exactly (no other app state is lost).
- "Reset to default" clears the saved configuration; the next load re-seeds from
  `default-team-config.json`.
- Weekly Report's Start section renders one card per non-`BENCH` slot with an assignment, sourced
  from the current team config joined against the selected week's `projections` — never from a
  fixture-baked `rosterSlot` field (removed).
- Weekly Report's Sit section does the same for `BENCH` slots.
- An unassigned slot renders an explicit empty-slot placeholder in Weekly Report, not a blank grid gap.
- An assigned player with no projection entry for the selected week renders a degraded card
  (name/position/team/status from `player-pool.json`) with an explicit "no projection available this
  week" note, not a crash or a silent omission.
- Every player card anywhere in the app (My Team, Weekly Report Start/Sit, Waiver Targets) renders a
  visible, non-blank status tag; the default (no elevated risk) reads exactly "Healthy."
- Status `designation` values are limited to the closed set: Healthy, Questionable, Doubtful, Out, IR.
- Waiver Targets for the selected week excludes any player whose `playerId` is present anywhere in
  the current team config's `slotAssignments` (starting or bench).
- If filtering removes every waiver target for a week, that section renders a distinct "no available
  targets" message rather than an empty grid.
- No component fetches `player-pool.json` / `default-team-config.json` or reads/writes
  `localStorage` directly, bypassing `lib/api.js` / `lib/teamConfig.js`.
- Track Record view renders with no changes — no status tags added to its history rows.
- Every projected-points number in the app (including any newly-touched views) still renders with an
  adjacent tier badge reading exactly `projection.tierLabel` — the existing hard constraint from
  `user-journey-frontend.md` is preserved, not weakened by this spec.
- App still builds to a static bundle with no server runtime required; `localStorage` is the only
  persistence mechanism introduced.

## 11. Non-Goals (explicit, to prevent scope creep)

- Pulling the roster automatically from ESPN's Fantasy API — explicitly out of v1 scope per CLAUDE.md;
  manual entry (this spec) is the documented v1 mechanism.
- Drag-and-drop or single-step "swap players between slots" UX — v1 is remove-then-assign.
- Cross-device / cross-browser sync of team configuration — `localStorage` is local to one browser.
- Any start/sit *optimizer* logic — the app never decides who *should* start based on projections;
  the user assigns players to slots manually, the app only displays projections for whatever's
  assigned. This is a deliberate, permanent scope boundary (distinct from the not-yet-built value
  engine), not a v1-only limitation.
- Roster legality validation beyond per-slot position eligibility (e.g., real-world NFL constraints,
  max-per-team limits, bye-week modeling) — not modeled.
- Adding live player status to the Track Record view — explicit decision, see §2.
- Full per-week projection coverage of every `player-pool.json` entry — fixtures only need to cover
  the seeded default team plus enough extra players to demo swapping; gaps are handled gracefully by
  the app (§4), not eliminated by exhaustive fixture authoring.
- Wiring any of this to a real backend/value engine/LLM layer — still mock-data-only, matching
  `user-journey-frontend.md`'s existing non-goals.
