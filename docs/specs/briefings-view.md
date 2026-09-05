# Spec: Briefings View (start/sit reports in the app)

Status: proposed, nothing built. Written 2026-09-05.

## 1. Problem / User Story

Two cloud routines produce a focused start/sit report three times a week
(`trig_01Mu28Hy89YUmxeHd54EcoBC` Mon & Sat 08:00 Budapest,
`trig_01YAnrFzrgrGbz4tpYqEFFRz` Wed 22:00). They are good — the Saturday
2026-09-05 run named its source and freshness, cross-checked the detected week
against the schedule, and flagged that "final designations" is a meaningless
frame six days out. **And none of it reaches the app.** It exists only as the
final message of a cloud session, read in a browser tab, gone from view once
another session scrolls past it.

> As the manager, I want the three-times-a-week start/sit briefing to appear in
> the app next to the weekly report, so the decision and the reasoning behind it
> live in one place and I can see what the tool told me last Wednesday.

This is deliberately a *different* artefact from the Weekly Report view
(`docs/specs/user-journey-frontend.md` §4). That view answers "what does the
model project for all 990 players this week". A briefing answers "what should I
do with my ten starters right now, and what changed since the last one". The
first is a ranked table; the second is a short, dated, opinionated note with a
lineup in it.

## 2. Where briefings come from

Today: nowhere. The routine prompts end with *"Do NOT commit, push, or open a
pull request"* — correct while testing, since a test run should not dirty the
repo, but it also means there is no delivery mechanism at all.

**Recommendation: the routine writes one JSON file per run and commits it.**

    frontend/public/mock/league-2/briefings/2026-09-05-sat.json

This is the same shape as every other data path in the project — precomputed
JSON on a CDN, read with `fetch()`, no server runtime — and it inherits the
2026-08-16 script-based decision rather than reopening it. It also means a
briefing is in git history, which is the property the shared-data-store note
argues the weekly fixtures wrongly lack.

Two alternatives considered:

- **Write to the Postgres store** (`briefings` table, phase 1). Better
  provenance and no commit noise, but the frontend would then need a serving
  path for it, which is exactly the phase-3 decision
  `docs/design/shared-data-store.md` says to argue separately. Revisit if
  briefings ever need to be queried rather than listed.
- **Leave them in the cloud session.** Zero work, and the status quo. Rejected
  because "the tool told me to bench Higgins on Wednesday" is unverifiable
  three weeks later, which undercuts the same eval-layer instinct the rest of
  the project is built around.

The routine prompts must change to permit exactly one commit path
(`.../briefings/*.json`) and nothing else. That is a narrowing of the current
blanket prohibition, not a removal of it.

## 3. Data model

One file per run. `slug` is `YYYY-MM-DD-{mon|wed|sat}`, which sorts
chronologically as a string and encodes which routine produced it.

```ts
type Briefing = {
  slug: string                 // "2026-09-05-sat"
  leagueId: string             // "league-2"
  season: number
  week: number
  kind: 'monday' | 'wednesday' | 'saturday'
  generatedAt: string          // ISO 8601, when the routine ran
  runUrl?: string              // claude.ai session, for the full transcript

  source: {                    // §"Source and freshness" — REQUIRED
    label: string              // "nflreadpy load_injuries" | "committed fixture"
    asOf: string               // ISO 8601 of the DATA, not of the run
    isLive: boolean            // false => the view must say so prominently
    note?: string              // "load_injuries(2026) not published yet"
  }

  lineup: Array<{
    slot: string               // QB | RB | WR | TE | FLEX | K | DEF
    playerId: string
    name: string
    position: string
    opponent: string
    expectedPoints: number     // P(play) x conditional. THE headline number.
    conditionalPoints: number  // the if-he-plays number
    playProbability: number
    designation?: string
    practice?: 'DNP' | 'Limited' | 'Full'   // Wednesday runs only
    reason?: string            // one line, why this player is in
  }>
  lineupTotal: number

  changes: Array<{             // may be empty; empty renders as "no changes"
    action: 'start' | 'bench'
    playerId: string
    name: string
    replacing?: string
    deltaExpectedPoints?: number
    reason: string
  }>

  watchList: Array<{ playerId: string; name: string; why: string; newsDue?: string }>
  opponentNotes: Array<{ playerId?: string; text: string }>
  byesAhead: Array<{ week: number; players: string[] }>

  caveats: string[]            // free text, rendered verbatim and un-truncated
  markdown?: string            // the routine's full prose, for "read the whole thing"
}
```

Notes on choices that are not obvious:

- **`source` is required, not optional.** The 2026-09-05 run's most valuable
  output was its opening line: the data was two days old and it said so. A
  briefing whose provenance is invisible is worse than no briefing, because the
  reader assumes it is live. `isLive: false` is a render-blocking condition for
  the banner in §4, not a footnote.
- **`expectedPoints` and `conditionalPoints` are both carried**, never one
  derived from the other in the view. Same reason the store migration comments
  say it: this is the single easiest pair to confuse, and Sleeper shows the
  conditional number, so a reader arrives primed for the wrong one.
- **`practice` is nullable and Wednesday-only.** It is the whole reason the
  Wednesday run fires at 22:00 Budapest, and Questionable splits 0.511 / 0.702 /
  0.790 by DNP / Limited / Full. Where present it must be visible, not hidden
  behind a tooltip.
- **`markdown` is a fallback, not the primary render.** The structured fields
  drive the UI; the prose is there so nothing the routine said is lost when the
  schema does not have a field for it.

## 4. The view

A fifth tab, `Briefings`, beside Weekly Report / Explore / My Team / Track
Record.

**List (default).** Reverse-chronological cards, one per briefing: date, kind
badge (Mon/Wed/Sat), lineup total, a one-line summary of `changes` ("Bench
Higgins, start Fannin"), and a staleness chip when `source.isLive` is false.
Grouped by week, most recent week open.

**Detail.** In order:

1. **Source banner.** Always rendered. When `source.isLive` is false it is the
   most prominent element on the page, above the lineup, stating the label and
   `asOf` age in plain words ("built from a fixture generated 2 days ago").
2. **Changes.** First, because it is the only part that asks the reader to *do*
   something. Empty state reads "No changes since the last briefing", which is
   information, not an absence.
3. **Lineup.** Ten rows, slot-ordered. Each shows expected points as the
   headline with the conditional number and P(play) beside it, visually
   subordinate. Reuse `PlayerCard`'s existing treatment for the
   "82% likely to play · 4.8 if he does" line rather than inventing a second
   visual language for the same idea.
4. **Practice report.** Wednesday briefings only, and above the watch list —
   it is why that run exists.
5. **Watch list, opponent notes, byes ahead.** Collapsible, closed by default.
6. **Caveats**, verbatim.
7. **"Read the full briefing"** disclosure rendering `markdown`, plus a link to
   `runUrl`.

## 5. API contract

Consistent with §6 of the user-journey spec — components never import fixtures
directly:

```ts
async function listBriefings(leagueId: string): Promise<BriefingSummary[]>
async function getBriefing(leagueId: string, slug: string): Promise<Briefing>
```

v1 reads `frontend/public/mock/{leagueId}/briefings/index.json` (a manifest the
routine appends to) and the per-slug files. Both degrade to an empty list on
anything that is not JSON, matching `api.js`'s existing behaviour.

## 6. Acceptance criteria

- A fifth tab, `Briefings`, exists and deep-links by hash like the others.
- The list renders newest first, grouped by week, and shows a visible staleness
  chip on every briefing whose `source.isLive` is false.
- **No briefing renders anywhere without its `source` label and `asOf` age
  visible.** A missing `source` renders an error state, never a silent default.
- Expected points is the headline number on every lineup row, with the
  conditional number and P(play) visible and visually subordinate. No row shows
  a conditional number alone.
- Wednesday briefings render the practice bucket for every flagged player;
  Monday and Saturday briefings omit the section entirely rather than showing it
  empty.
- `changes` renders an explicit "no changes" state when empty.
- The full `markdown` is reachable in two clicks or fewer, and is never the
  only place a lineup appears.
- The view works with zero briefings present (empty state, no error).
- Nothing in the view fetches at request time; all data comes through
  `listBriefings()` / `getBriefing()` over static JSON.

## 7. Non-goals

- **Editing a lineup from the briefing.** It is a record of advice, not a
  control surface. Roster changes stay in My Team.
- **Push/email delivery.** The routine already reaches the reader through
  Claude; this is the durable record, not a second notification channel.
- **Grading briefings against outcomes.** That is the eval layer's job and it
  needs the append-only `projections` table
  (`docs/design/shared-data-store.md` phase 2), not a view.
- **Cross-league briefings.** league-2 only for now; the routines only cover
  that roster.
- **Rendering arbitrary routine output.** If a routine emits fields this schema
  does not have, they land in `markdown` and the schema is revised
  deliberately.

## 8. Open questions

1. ~~**Does the routine commit, or does a human?**~~ **Settled 2026-09-05: the
   routine commits.** Both prompts now carry a `WRITING THE BRIEFING` section
   granting exactly one write path (`frontend/public/mock/league-2/briefings/`)
   and forbidding everything else — no `git add -A`, no `git add .`, no force
   push, no other branch, and an explicit instruction to leave alone any change
   in `git status` it did not make and to say so in the report. A rejected push
   gets one rebase and one retry, then stops: a failed push is not worth an
   unattended agent's second guess at the repo state. The report itself remains
   the deliverable if any of it fails.

   Worth being clear-eyed about what this is: a scheduled agent with write
   access to a repository, running unattended three times a week. The
   narrowness of the grant is the control, and the blast radius if a run
   misbehaves is one directory of generated JSON with full git history behind
   it. The platform-level `allowed_push_branches` is not settable through the
   routine API, so the prompt and the GitHub App's own scope are what enforce
   this — review the first few briefing commits rather than assuming.
2. **Retention.** One file per run is ~156 files over a season across two
   routines. Small, but the manifest should probably paginate rather than list
   all of them. Settle before the first real season week.
3. **Should the Weekly Report view link to the latest briefing?** Probably yes,
   as a banner — the briefing is the actionable subset of what that view shows
   in full. Left out of the acceptance criteria above until §1's framing is
   confirmed as right.
