# Spec: Dual-League Support

**Status:** Draft — 2026-08-31

---

## Problem / User Story

The builder plays in two NFL fantasy leagues for the 2026 season. The tool produces one weekly report keyed to one league's scoring rules and one team roster. Running both leagues from a single invocation — with correct per-league point math and separate start/sit/waiver recommendations for each roster — removes the need to maintain a second config fork or run the script twice with hand-edited arguments. The data pull (nflreadpy, FantasyPros, ESPN news) is expensive enough to share; the scoring loop is cheap enough to run twice.

---

## Scope Flag

CLAUDE.md v15 lists "Multi-league support, user accounts/auth" as explicitly v1 out of scope. This spec implements a deliberately narrow subset — two leagues, one user, no auth, no user accounts — that does not build a platform. The guard on the original out-of-scope line (don't build multi-tenant infrastructure) still holds.

---

## Assumptions

1. Both leagues use the same NFL season, the same week number, and the same underlying player/stat universe (nflreadpy, FantasyPros, ESPN news). No per-league week offsets.
2. League 2's scoring settings are not yet confirmed. Its config ships as a placeholder with `"_PLACEHOLDER": true`. The tool emits a visible warning at generation time but does not abort — a placeholder config still produces a usable report.
3. League IDs are `"league-1"` (the existing ESPN 14-team full-PPR league) and `"league-2"` (second league, not yet confirmed). These strings are arbitrary but stable — they key output directories and localStorage.
4. The data pull happens once per `generate_report.py` run; scoring and output happen once per league per run.
5. The frontend adds a league selector (dropdown in the existing header), not separate routes or deployed URLs.
6. Auth, user accounts, and support for more than two leagues are not in scope.

---

## Data Model Changes

### 1. Leagues manifest: `backend/leagues.json` (new file)

```json
{
  "leagues": [
    {
      "leagueId": "league-1",
      "displayName": "ESPN 14-team PPR",
      "teamCount": 14,
      "leagueFormat": "ppr",
      "scoringConfigPath": "leagues/league-1/scoring-config.json",
      "outDir": "../frontend/public/mock/league-1"
    },
    {
      "leagueId": "league-2",
      "displayName": "Second League",
      "teamCount": 12,
      "leagueFormat": "half_ppr",
      "_placeholder": true,
      "scoringConfigPath": "leagues/league-2/scoring-config.json",
      "outDir": "../frontend/public/mock/league-2"
    }
  ]
}
```

All paths in `scoringConfigPath` and `outDir` are relative to `backend/` (the generate scripts' working directory). `leagueFormat` must be one of `"ppr"`, `"half_ppr"`, `"standard"` — these are the existing recognized keys in `WeeklyReportView.jsx`'s `FORMAT_LABEL` map. `teamCount` drives the waiver-target roster cutoffs (see below).

### 2. Per-league scoring configs

Migration (not a copy):

| Before | After |
|---|---|
| `backend/scoring_config.placeholder.json` | `backend/leagues/league-1/scoring-config.json` |
| (does not exist) | `backend/leagues/league-2/scoring-config.json` |

`scoring_config.placeholder.json` content migrates verbatim to `league-1/scoring-config.json`. The old path at `backend/scoring_config.placeholder.json` should be removed to prevent stale reads; references in tests and `DEFAULT_SCORING_CONFIG_PATH` constants need updating to the new path.

`league-2/scoring-config.json` ships as a minimal placeholder containing the same `linear`/`milestones`/`tiers` structure with `"_PLACEHOLDER": true` and a `_note` explaining what to fill in. The format is identical to the existing placeholder — no schema change.

### 3. Output file layout

```
frontend/public/mock/
  roster-slots.json             (unchanged, shared)
  league-1/
    weekly-report-week-{N}.json (was mock/weekly-report-week-{N}.json)
    player-pool.json            (was mock/player-pool.json)
    track-record.json           (was mock/track-record.json)
    default-team-config.json    (was mock/default-team-config.json)
  league-2/
    weekly-report-week-{N}.json
    player-pool.json
    track-record.json
    default-team-config.json
```

`roster-slots.json` stays flat and shared — both leagues use the same slot structure (QB/RB/WR/TE/DST/K/FLEX/BENCH). The old flat-path files (`mock/weekly-report-week-{N}.json`, `mock/player-pool.json`, `mock/track-record.json`) are removed after migration to avoid stale reads.

### 4. `weekly-report-week-N.json` shape

One new top-level field:

```json
{
  "week": 12,
  "leagueId": "league-1",
  "leagueFormatAssumption": "ppr",
  "generatedAt": "...",
  "projections": [...],
  "waiverTargets": [...]
}
```

`leagueId` is added so the track-record layer and any future consumer can identify which league a report file belongs to without depending on its directory path.

### 5. Waiver-target roster cutoff derivation

`waiver_targets.py`'s `DEFAULT_ROSTERED_RANK_CUTOFF` currently hardcodes 14 (the team count for league-1). For a league with `teamCount = N`, the per-position cutoff is:

```python
{"QB": N, "RB": N * 2, "WR": N * 2, "TE": N, "DST": N, "K": N}
```

This formula reproduces the existing default for N=14. Both `waiver_eligible_candidates()` and `select_waiver_targets()` already accept `rostered_rank_cutoff` as an overrideable keyword argument — no signature change needed in `waiver_targets.py`. The caller derives and passes the correct dict.

---

## API / Function Signature Changes

### `generate_report.py`

**New CLI argument:** `--leagues-config` (path to `leagues.json`). When present, the script:

1. Loads the manifest.
2. Pulls shared data once: player pool, schedule, game logs, DST pool and logs (nflreadpy); FantasyPros consensus; ESPN news.
3. Loops over leagues: loads each scoring config, derives the waiver cutoff dict from `teamCount`, calls `build_weekly_report_and_pool()`, writes `{outDir}/weekly-report-week-{N}.json` and `{outDir}/player-pool.json`.
4. If a league's scoring config has `"_PLACEHOLDER": true`, prints `WARNING: <leagueId> scoring config is still a placeholder — projections will be inaccurate until real values are entered` but does not exit.

**Backward-compat rule:** `--scoring-config` and `--out-dir` remain valid for single-league operation. `--leagues-config` and `--scoring-config` are mutually exclusive; passing both is an `argparse` error.

**`assemble_weekly_report()` — two new keyword arguments:**

```python
def assemble_weekly_report(
    week: int,
    generated_at: str,
    projection_entries: list[dict[str, Any]],
    waiver_entries: list[dict[str, Any]],
    league_id: str = "league-1",
    league_format: str = "ppr",
) -> dict[str, Any]:
```

The returned dict gains `"leagueId": league_id`. `LEAGUE_FORMAT_ASSUMPTION` is kept as the module-level constant (still `"ppr"`) for backward compatibility with existing tests that call `assemble_weekly_report()` without the new kwargs. In the multi-league path, the manifest's `leagueFormat` is passed explicitly and the constant is not used.

**`build_weekly_report_and_pool()` — three new keyword arguments:**

```python
def build_weekly_report_and_pool(
    season: int,
    week: int,
    scoring_config: dict[str, Any],
    pool: list[dict[str, Any]],
    schedule_games: list[dict[str, Any]],
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    news_flags_by_player: dict[str, dict[str, Any]],
    window: int = DEFAULT_WINDOW,
    generated_at: Optional[str] = None,
    consensus_projections: Optional[dict[str, dict[str, Any]]] = None,
    league_id: str = "league-1",
    league_format: str = "ppr",
    rostered_rank_cutoff: Optional[dict[str, int]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
```

`rostered_rank_cutoff=None` means the function falls back to `DEFAULT_ROSTERED_RANK_CUTOFF` from `waiver_targets.py` (same 14-team default as today). The multi-league caller passes the derived dict.

### `generate_track_record.py`

**New CLI argument:** `--leagues-config` (path to `leagues.json`). When present, the script iterates over all leagues and for each: uses `{outDir}` as `reports_dir`, reads weekly-report snapshots from `{outDir}/weekly-report-week-{N}.json`, loads the league's scoring config for computing actuals, writes `{outDir}/track-record.json`.

The existing `--reports-dir`, `--scoring-config`, and `--out-path` arguments remain valid. `--leagues-config` and `--reports-dir` are mutually exclusive; passing both is an `argparse` error.

**`DEFAULT_REPORTS_DIR` and `DEFAULT_SCORING_CONFIG_PATH`** in `generate_track_record.py` need updating to point at the new `league-1/` paths:

```python
DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public" / "mock" / "league-1"
DEFAULT_SCORING_CONFIG_PATH = Path(__file__).resolve().parent / "leagues" / "league-1" / "scoring-config.json"
```

### `.github/workflows/weekly-report.yml`

"Generate weekly report" step:

```yaml
run: |
  python3 generate_report.py \
    --season "${{ steps.vars.outputs.season }}" \
    --week "${{ steps.vars.outputs.week }}" \
    --leagues-config ../leagues.json
```

"Generate track record" step:

```yaml
run: |
  PRIOR_WEEK=$(( ${{ steps.vars.outputs.week }} - 1 ))
  if [ "$PRIOR_WEEK" -ge 1 ]; then
    python3 generate_track_record.py \
      --season "${{ steps.vars.outputs.season }}" \
      --as-of-week "$PRIOR_WEEK" \
      --leagues-config ../leagues.json
  fi
```

The "Commit refreshed fixtures" step's `git add frontend/public/mock/` already covers both subdirectories — no change needed.

### `scoring.py` — no changes required

`compute_league_points(stat_line, scoring_config)` is already stateless and takes `scoring_config` as a pure parameter. The dual-league path calls it (indirectly, through `project_player()` and `build_weekly_report_and_pool()`) twice with different configs. The function is confirmed config-driven end-to-end: it reads only from the passed-in dict, holds no module-level state, and produces a `ScoringResult` from inputs alone.

---

## Frontend Impact

### League selector

Add a `<select>` dropdown to `App.jsx`'s header block, between the brand block and the tab `<nav>`. Each `<option>` is labeled with the league's `displayName`. For two leagues, a dropdown is sufficient — do not add a third tab or a separate route.

`activeLeagueId` is a `useState` in `App.jsx`, defaulting to `"league-1"`. It is persisted to localStorage under the key `nfl-fantasy-assistant:active-league`. On initial load, read the stored value; default to `"league-1"` if absent. On change, the state update causes all three views to re-fetch their data.

`activeLeagueId` is passed as a prop to `WeeklyReportView`, `TeamConfigView`, and `TrackRecordView`. No React context or global store is needed for two leagues.

League `displayName` values are baked into the frontend build — same pattern as `LATEST_AVAILABLE_WEEK` in `api.js`. They are not fetched from `leagues.json` at runtime.

### `api.js` changes

Four functions gain a `leagueId` parameter. The `"league-1"` default preserves existing behavior for any caller that does not pass the argument:

```js
export async function getWeeklyReport(week = LATEST_AVAILABLE_WEEK, leagueId = 'league-1') {
  return fetchJsonFixture(`${import.meta.env.BASE_URL}mock/${leagueId}/weekly-report-week-${week}.json`);
}

export async function getTrackRecord(season = DEFAULT_SEASON, leagueId = 'league-1') {
  const data = await fetchJsonFixture(`${import.meta.env.BASE_URL}mock/${leagueId}/track-record.json`);
  if (!data || data.season !== season) return null;
  return data;
}

export async function getPlayerPool(leagueId = 'league-1') {
  const data = await fetchJsonFixture(`${import.meta.env.BASE_URL}mock/${leagueId}/player-pool.json`);
  return data || { players: [] };
}

export async function getDefaultTeamConfig(leagueId = 'league-1') {
  return fetchJsonFixture(`${import.meta.env.BASE_URL}mock/${leagueId}/default-team-config.json`);
}
```

`getRosterSlots()` does not change — it reads the shared `mock/roster-slots.json`.

`LATEST_AVAILABLE_WEEK`, `MIN_SELECTABLE_WEEK`, and `MAX_SELECTABLE_WEEK` remain single shared constants — both leagues generate the same week simultaneously.

### `teamConfig.js` changes

Replace the module-level constant with a key-derivation function:

```js
// Before
const STORAGE_KEY = 'nfl-fantasy-assistant:team-config:v1'

// After
function storageKey(leagueId) {
  return `nfl-fantasy-assistant:team-config:${leagueId}:v1`
}
```

`useTeamConfig` gains a `leagueId` parameter (default `'league-1'`). It reads and writes using `storageKey(leagueId)`. When `leagueId` changes (user switches leagues), the hook re-reads from the new key — either loading that league's previously saved roster or falling back to the default team config for that league.

One-time migration on first load: if the old key `nfl-fantasy-assistant:team-config:v1` exists in localStorage and the new `nfl-fantasy-assistant:team-config:league-1:v1` key does not, copy the old value to the new key and delete the old key. This prevents loss of a previously configured league-1 roster.

---

## Acceptance Criteria

- `python3 generate_report.py --season 2026 --week 12 --leagues-config ../leagues.json` (run from `backend/`) produces exactly four files: `frontend/public/mock/league-1/weekly-report-week-12.json`, `frontend/public/mock/league-1/player-pool.json`, `frontend/public/mock/league-2/weekly-report-week-12.json`, `frontend/public/mock/league-2/player-pool.json`.
- The two `weekly-report-week-12.json` files contain different `"projections"` point values for the same player when the two scoring configs differ. Verifiable by calling `build_weekly_report_and_pool()` in a test with two synthetic configs that differ only in `pass_td` (e.g., 6 vs. 4): a QB with 2 projected TDs should score 12.0 under config A and 8.0 under config B.
- Each `weekly-report-week-12.json` contains `"leagueId"` matching its directory (`"league-1"` or `"league-2"`).
- Each `weekly-report-week-12.json` contains `"leagueFormatAssumption"` matching the manifest's `leagueFormat` for that league, not the module-level `LEAGUE_FORMAT_ASSUMPTION` constant.
- Running with `--leagues-config` when league-2's scoring config carries `"_PLACEHOLDER": true` prints a line beginning with `WARNING:` to stdout and exits with code 0 (does not raise).
- Running `python3 generate_report.py --season 2026 --week 12` without any `--leagues-config` or `--scoring-config` flag works exactly as before (uses `DEFAULT_SCORING_CONFIG_PATH` and `DEFAULT_OUT_DIR`). All existing tests pass without modification.
- Running `python3 generate_report.py --leagues-config ../leagues.json --scoring-config ../leagues/league-1/scoring-config.json` exits with a non-zero code and prints an `argparse` error (mutually exclusive arguments).
- `python3 generate_track_record.py --season 2026 --as-of-week 11 --leagues-config ../leagues.json` writes `frontend/public/mock/league-1/track-record.json` and `frontend/public/mock/league-2/track-record.json`, each with `"season"` and `"history"` fields sourced from that league's own `weekly-report-week-{N}.json` snapshots.
- In the frontend: switching the league selector from league-1 to league-2 causes `WeeklyReportView` to call `getWeeklyReport(week, 'league-2')`, which fetches from `mock/league-2/weekly-report-week-{N}.json`. The header reflects the league-2 `displayName`.
- In the frontend: saving a roster assignment while league-2 is active writes to localStorage key `nfl-fantasy-assistant:team-config:league-2:v1`. Switching back to league-1 and saving writes to `nfl-fantasy-assistant:team-config:league-1:v1`. The two rosters are independent.
- On first load, if `nfl-fantasy-assistant:team-config:v1` exists in localStorage and `nfl-fantasy-assistant:team-config:league-1:v1` does not, the old value is copied to the new key and the old key is deleted.
- `scoring.py`'s existing unit tests pass without modification — no change to that module.
- The GitHub Actions workflow (`weekly-report.yml`) commits all output files under `frontend/public/mock/league-1/` and `frontend/public/mock/league-2/` in a single commit.

---

## Non-Goals

- More than two leagues. The manifest schema accommodates N entries, but the acceptance criteria and UI are only validated for two.
- Pulling either league's roster or scoring config live from ESPN's or Yahoo's private league API. Manual entry via the scoring-config JSON files remains the v1 mechanism, per CLAUDE.md.
- Per-league week offsets. Both leagues use the same `--week` value.
- Separate deployed URLs per league. One URL, one build, one dropdown.
- Comparing or sharing players across leagues in the UI.
- Retroactively backfilling track-record history for league-2. Accumulation begins from the first week generated after this spec is implemented.
- Changes to `backend/backtest.py` — it operates on past season data, not live reports, and is not per-league.

---

## Implementation Order

For a dev agent building against this spec, touch files in this order to minimize broken intermediate states:

1. `backend/leagues/league-1/scoring-config.json` — migrate from `backend/scoring_config.placeholder.json`
2. `backend/leagues/league-2/scoring-config.json` — new placeholder
3. `backend/leagues.json` — new manifest
4. `backend/generate_report.py` — `assemble_weekly_report()`, `build_weekly_report_and_pool()`, `main()` CLI
5. `backend/generate_track_record.py` — `main()` CLI, `DEFAULT_REPORTS_DIR`, `DEFAULT_SCORING_CONFIG_PATH`
6. `frontend/public/mock/league-1/` — migrate existing fixture files; remove old flat-path fixtures
7. `frontend/src/lib/api.js` — `leagueId` parameter on four functions
8. `frontend/src/lib/teamConfig.js` — `storageKey()` function, `useTeamConfig(leagueId)` hook, migration logic
9. `frontend/src/App.jsx` — league selector `<select>`, `activeLeagueId` state, prop threading
10. `.github/workflows/weekly-report.yml` — `--leagues-config` flag on both steps
11. `backend/tests/test_generate_report.py` — update `DEFAULT_SCORING_CONFIG_PATH` references; add at least one test for the two-config scoring-isolation property described in acceptance criteria bullet 2
