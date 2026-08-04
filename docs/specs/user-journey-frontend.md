# Spec: User Journey / Frontend / Report-View Design (v1)

Status: Draft — covers CLAUDE.md Next Steps item 1 (current main focus, v7).
Depends on: nothing blocking (does not require real league scoring values or roster — those are Next Steps items 2–3, still pending).
Feeds into: value engine (item 5), eval layer (item 8) — this spec defines the data shape those will eventually need to produce.

## 1. Problem / User Story

As the builder (single user, single league), every week I need one place to see: which of my rostered players to start or sit, which waiver-wire players are worth picking up, and — as a portfolio differentiator — whether my tool's own past recommendations were actually good, tracked separately for the two projection tiers it uses. Today none of this exists as a UI; there is no frontend code in this repo. This spec designs the interaction flow and the two views (weekly report, eval/track-record) against **mock data shaped like what the real value engine will eventually output**, so frontend work can start now without waiting on the commissioner (real scoring rules/roster) or the backend (value engine, not yet built).

**Assumption:** "single view" in CLAUDE.md's Suggested Architecture means a single-page app, not a single unified screen — the weekly report and eval/track-record view are two toggleable sections within one SPA, no login, no routing framework required beyond optional URL-hash sync for shareability.

## 2. Overall User Journey / Information Architecture

```
┌─────────────────────────────────────────────┐
│  Header: app name, current week indicator    │
│  Tabs: [ Weekly Report ]  [ Track Record ]    │
├─────────────────────────────────────────────┤
│                                               │
│   Weekly Report view (default on load)       │
│     OR                                       │
│   Track Record view                          │
│                                               │
└─────────────────────────────────────────────┘
```

- **Entry point:** loading the app shows the Weekly Report view for the current/most recent week by default.
- **Navigation:** two tabs, no nested routing. Tab state optionally reflected in the URL (`#report` / `#track-record`) for shareable links — nice-to-have, not required for acceptance.
- **Week selector:** Weekly Report view has a simple dropdown/stepper for week number, defaulting to the latest available week in the mock fixture. Track Record view shows season-to-date by default with an optional week filter on the history table.
- **No auth, no user switcher, no league switcher** — single user, single league, per CLAUDE.md.
- **Empty/loading states:** both views must render a loading skeleton while `getWeeklyReport()`/`getTrackRecord()` resolve, and a plain "no data for this week" state if the fixture has no entry for the selected week (relevant later once real weeks run out of data, e.g., preseason).

## 3. Data Model (mock JSON, stand-in for future real API/backend output)

Two top-level shapes, returned by two data-fetching functions described in Section 5.

### 3.1 Shared: Player Projection object

Used by both views. This is the object CLAUDE.md's two-tier engine will eventually populate for real.

```json
{
  "playerId": "p_00123",
  "name": "Josh Allen",
  "position": "QB",
  "team": "BUF",
  "opponent": "MIA",
  "rosterSlot": "QB",
  "projection": {
    "tier": "consensus",
    "points": 24.3,
    "tierLabel": "Consensus projection",
    "source": "FantasyPros"
  },
  "newsFlag": {
    "riskLevel": "low",
    "designation": "Questionable",
    "summary": "Limited practice reps Wednesday/Thursday, full participant Friday. Expected to play."
  }
}
```

- `projection.tier` is an enum: `"consensus"` (FantasyPros top-10/position) | `"in_house_estimate"` (rolling-average/matchup-adjusted, everyone else). This field is what drives the visible tier badge — **every player card must render a tier badge, never a bare number.**
- `projection.tierLabel` is the literal user-facing string CLAUDE.md mandates: `"Consensus projection"` for FantasyPros, `"Our estimate"` for in-house. Stored in the fixture (not hardcoded per-component) so copy changes don't require a code change.
- `rosterSlot` is a free-form string drawn from a **generic, swappable slot config** (see 3.3) — not a hardcoded final league roster, since the real roster is still pending (Next Steps item 2).
- `newsFlag` is nullable — omit or set `"riskLevel": "none"` when there's no relevant news. `riskLevel` enum: `"none" | "low" | "medium" | "high"`. This is the mock stand-in for the LLM summarization layer's eventual output (Next Steps item 6, not built yet).

### 3.2 Weekly Report fixture (`getWeeklyReport(week)`)

```json
{
  "week": 1,
  "leagueFormatAssumption": "half_ppr",
  "generatedAt": "2026-09-04T13:00:00Z",
  "startSit": {
    "start": [ /* Player Projection objects, rosterSlot = a starting slot */ ],
    "sit": [ /* Player Projection objects, rosterSlot = "BENCH" or similar */ ]
  },
  "waiverTargets": [
    {
      "playerId": "p_00456",
      "name": "Jaylen Warren",
      "position": "RB",
      "team": "PIT",
      "opponent": "CLE",
      "projection": {
        "tier": "in_house_estimate",
        "points": 9.8,
        "tierLabel": "Our estimate",
        "source": "In-house model"
      },
      "newsFlag": { "riskLevel": "none" },
      "rationale": "Lead back role trending up with committee-mate on the injury report."
    }
  ]
}
```

- `leagueFormatAssumption` is explicitly labeled "Assumption" in the fixture and rendered in a small footnote in the UI (e.g., "Projections shown assume half-PPR pending real league settings") — this keeps the mock honest about what's not yet confirmed, per CLAUDE.md's repeated instruction not to assume unconfirmed values.
- `waiverTargets` entries add a short `rationale` string (LLM-summarization stand-in), sorted by projected points descending.

### 3.3 Generic roster slot config (not final league truth)

```json
{
  "slots": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "DST", "K", "BENCH", "BENCH", "BENCH"]
}
```

Stored as a separate small config object, referenced by the UI to render slot groupings — **must be trivially editable** (single array) once the real roster is confirmed (Next Steps item 2). No component should hardcode slot names or counts inline.

### 3.4 Track Record fixture (`getTrackRecord(season)`)

```json
{
  "season": 2026,
  "asOfWeek": 3,
  "summary": {
    "in_house_estimate": {
      "tierLabel": "Our estimate",
      "predictionsScored": 42,
      "startSitHitRate": 0.71,
      "meanAbsoluteError": 4.2
    },
    "consensus": {
      "tierLabel": "Consensus projection",
      "predictionsScored": 18,
      "startSitHitRate": 0.83,
      "meanAbsoluteError": 3.1
    }
  },
  "history": [
    {
      "predictionId": "pred_0001",
      "week": 1,
      "player": { "playerId": "p_00123", "name": "Josh Allen", "position": "QB" },
      "tier": "consensus",
      "recommendationType": "start",
      "predictedPoints": 24.3,
      "actualPoints": 27.1,
      "outcomeCorrect": true
    },
    {
      "predictionId": "pred_0002",
      "week": 1,
      "player": { "playerId": "p_00456", "name": "Jaylen Warren", "position": "RB" },
      "tier": "in_house_estimate",
      "recommendationType": "waiver_add",
      "predictedPoints": 9.8,
      "actualPoints": null,
      "outcomeCorrect": null
    }
  ]
}
```

- `summary` is keyed by tier, matching CLAUDE.md's instruction to track accuracy separately per tier and to weight the in-house tier as "the one actually worth measuring closely."
- `actualPoints`/`outcomeCorrect` are nullable to represent pending outcomes (game not yet played) — the eval view must render a distinct "pending" state, not a blank or an error.
- `startSitHitRate` and `meanAbsoluteError` are placeholder metric names in the fixture; **this spec does not define how they're computed** — that's the value engine's/eval layer's job (Next Steps items 5, 8), out of scope here (see Non-Goals).

## 4. Weekly Report View

**Layout (top to bottom):**
1. Week selector + `leagueFormatAssumption` footnote.
2. **Start** section: player cards grouped by `rosterSlot`, each showing name/position/team, opponent, projected points **with tier badge**, and injury/news risk flag (colored dot or small tag: none/low/medium/high) with the `summary` text visible on hover/expand.
3. **Sit** section: same card format, visually de-emphasized (e.g., muted styling) relative to Start.
4. **Waiver Targets** section: ranked list, same card format plus the `rationale` line, sorted by projected points descending.

**Tier badge requirement (hard constraint, directly from CLAUDE.md):** every rendered projection number must be adjacent to a visible badge/label reading exactly `"Consensus projection"` or `"Our estimate"` (pulled from `projection.tierLabel`, never inferred/hardcoded per component) — the two tiers must never look like the same kind of number. This applies identically in Start, Sit, and Waiver Targets sections.

## 5. Eval / Track-Record View

**Design goal (from CLAUDE.md Context/Purpose and Success Criteria): legible in 10 seconds to a recruiter glancing at it.**

**Layout (top to bottom):**
1. **Headline summary block** — two side-by-side cards (or a two-column table), one per tier: tier label, predictions scored, hit rate, mean absolute error. This is the "10-second" artifact — large numbers, minimal text, tier clearly labeled (`"Our estimate"` vs `"Consensus projection"`).
2. **History table** below the summary — one row per logged prediction: week, player, tier badge, recommendation type, predicted vs actual points, correct/incorrect/pending indicator. Sortable by week and filterable by tier at minimum (column click or simple dropdown — no need for a full data-grid library).
3. Pending rows (no actual outcome yet) render distinctly (e.g., grey "pending" tag) rather than blank cells or a 0.

## 6. API Contract (data-fetching functions)

The frontend depends on two async functions; today they read local mock JSON fixtures, later they call a real backend endpoint returning the identical shape (no view-layer changes required at that swap):

```ts
async function getWeeklyReport(week?: number): Promise<WeeklyReport>
async function getTrackRecord(season?: number): Promise<TrackRecord>
```

- `WeeklyReport` and `TrackRecord` types match the JSON shapes in Section 3.2 and 3.4.
- v1 implementation: these functions `fetch()` static files (e.g., `/mock/weekly-report-week-{n}.json`, `/mock/track-record.json`) bundled with the static build — no server runtime required, consistent with a Vercel static deploy.
- Components must call these functions rather than importing fixture JSON directly, so the later swap to a real API is a one-file change.

## 7. Acceptance Criteria

- App renders as a single-page app with two tabs (Weekly Report, Track Record); switching tabs requires no full page reload.
- Weekly Report view groups players into Start / Sit / Waiver Targets sections, each player card showing name, position, team, opponent, projected points, and a tier badge reading exactly the fixture's `tierLabel` value.
- No projected-points number anywhere in the app renders without an adjacent, visible tier badge.
- Injury/news risk flags render on any player card whose fixture data includes a non-`"none"` `riskLevel`, with the summary text accessible (visible or on interaction).
- Track Record view's headline summary block is visually distinct and separated per tier (`in_house_estimate` vs `consensus`), each showing at minimum: predictions scored, hit rate, mean absolute error.
- Track Record view's history table renders one row per fixture entry, supports sorting/filtering by at least week and tier, and renders a distinct "pending" state for entries with `actualPoints: null`.
- Roster slot labels used for grouping are read from the generic slot config object (Section 3.3), not hardcoded in component code.
- All data rendered by both views is sourced through `getWeeklyReport()` / `getTrackRecord()`, backed by static mock JSON fixtures matching the shapes in Section 3 — no component imports fixture data directly.
- `leagueFormatAssumption` (or equivalent) is visibly rendered as a labeled assumption/footnote in the Weekly Report view, not presented as a confirmed league value.
- App builds to a static bundle (`npm run build` or equivalent) deployable to Vercel with no server runtime required for v1.
- No login screen, user switcher, or league switcher exists anywhere in the UI.

## 8. Non-Goals (explicitly out of scope for this spec)

- Building the real backend, value engine, or data pipeline — this spec covers frontend/mock-data only.
- Wiring real calls to FantasyPros, nflreadpy, Sleeper, or ESPN's news endpoint — deferred to Next Steps items 4–6.
- Implementing the LLM summarization/risk-flagging logic — `newsFlag`/`rationale` fields are mock stand-ins for that layer's future output.
- Computing real accuracy metrics (`startSitHitRate`, `meanAbsoluteError`) — the eval view displays whatever the fixture contains; the computation logic is Next Steps items 5 and 8.
- Finalizing the real league scoring rules or roster/slot structure — both are still pending from the commissioner (Next Steps items 2–3); this spec's slot config and scoring-format field are placeholders by design.
- Auth, user accounts, multi-league support — explicitly out of v1 scope per CLAUDE.md.
- Trade suggestions, fan sentiment signals — explicitly out of v1 scope per CLAUDE.md.
- Server-side rendering, complex routing/state-management libraries — not needed at this app's size.
