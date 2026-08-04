# In-House Projection Model — Build Spec (Next Steps item 4)

Design instructions for the bench/waiver-tier projection model referenced in `CLAUDE.md`. This is the tier that covers every player FantasyPros' free-tier API doesn't (i.e., everyone outside the top 10 per position) — most of a fantasy roster in practice. Read `CLAUDE.md`'s Data Sources and Next Steps sections before building; this doc doesn't repeat the architecture decisions made there, only the how.

## 1. Scope check before building

This is listed as v1 in-scope, but flagging two real constraints before writing code:

- **The scoring formula this model needs to produce points against is not finalized.** The real league's scoring values are still blocked on the commissioner (CLAUDE.md Next Steps item 2), and four schema questions are still open (item 3). Building this model with a hardcoded scoring formula would mean rebuilding it once real values land. Treat the scoring formula as a config object passed in, not a constant — see §4.
- **A full defense-vs-position matchup model is a meaningfully separate piece of work from the rolling-average projection**, and nobody has validated it has real predictive value for this league yet. Recommendation below is to ship the rolling average alone first, wire it into the report view, and treat the matchup adjustment as an additive v1.1 step rather than a blocking dependency — see §3.4. If the 2-week build target is tight, this is the piece to cut or simplify first.

## 2. Inputs

All inputs come from `nflreadpy`, confirmed available in earlier research (`docs/research/phase1-probe-results.md`, `coverage-scorecard.md`):

- `load_player_stats()` — weekly per-player stat lines (raw counting stats: pass/rush/rec yards, TDs, receptions, fumbles, etc. — the specific raw columns needed depend on the final scoring config, see §4). Also carries `target_share`, `air_yards_share`, `wopr`, `racr`, and `opponent_team`. Confirm the exact snap-count/snap-% column name against the current `nflreadpy` schema before relying on it — it wasn't pinned down in the probe results and column names shift between library versions.
- `load_rosters()` — position, team, active/inactive status.
- `load_injuries()` — report status, practice participation (for excluding or downweighting players who didn't play/practice).
- `load_schedules()` — game-level home/away and week mapping, needed to build the opponent-history table in §3.4.

No external API call is needed for this tier — that's the point of it existing.

## 3. Algorithm

### 3.1 As-of discipline (read this before anything else)

Every number this model produces for "week N" must be computable using only data available before week N kicked off. Concretely: when projecting week N, filter `load_player_stats()` to `week < N` (same season), or `season < current_season` for week 1. This applies to the rolling average, the matchup-difficulty table, and any injury/practice-status lookups. It's easy to accidentally leak future information (e.g., pulling a player's full-season average that includes the target week, or using a final injury designation that wasn't public until after the projection would have been made) — this was flagged earlier in the project's own reliability notes (`operational-reliability-and-crossvalidation.md`) and it applies just as much here. Write the data-loading function so "as of week N" is a required parameter, not an implicit assumption — that also makes the eventual manual sanity check (Next Steps item 7) actually testable, since you can rerun a past week's projection exactly as it would have looked in real time.

### 3.2 Rolling window

Take the trailing N games of the player's `season, week < N` stat lines (not calendar weeks — a bye week or missed game shouldn't count as a zero, it should just not be in the window). Recommended starting point: **N = 4 games played**, unweighted mean. Decisions to make explicit in code/config rather than leaving implicit:

- **Games played, not weeks elapsed.** A player coming off a bye or a missed game due to injury should have their window built from their last 4 games *played*, pulled from as far back as necessary within the current season. Don't reach into the prior season by default — a player's role can change completely between seasons (see cold-start below).
- **Equal-weight average vs. recency-weighted.** Start with a simple unweighted mean for v1 — it's easier to explain in the case study and easier to sanity-check by hand. A recency-weighted (e.g., exponential decay) version is a reasonable v1.1 experiment once you have a baseline to compare it against, not a day-one requirement.
- **Cold start (rookies, players with <4 games played this season).** Don't average over fewer games silently — that produces noisy projections early in a player's sample (e.g., one huge or one zero game dominates a 1-game average) without signaling that it's noisy. Compute the average over whatever games exist, but tag the projection's confidence explicitly (§5) so the report view can show "based on 1 game" rather than presenting it with the same visual weight as a 4-game average.
- **Role-change discontinuity.** A backfield committee back who just became the lead back after an injury to the starter ahead of him will have a rolling average dominated by his old, smaller role — the average will understate him. This is a real, known failure mode of rate-based rolling averages and there's no clean fix without manual override capability. Don't try to solve this algorithmically for v1; instead, make sure the report view surfaces the underlying game log next to the projection, so the human using the tool can catch this and self-correct. Flagging this now so it isn't treated as a bug later — it's an inherent limitation of the approach, not a defect.

### 3.3 Computing points from raw stats

The rolling average must be an average of **points computed via the real league scoring formula from raw stat columns**, not `nflreadpy`'s own `fantasy_points`/`fantasy_points_ppr` fields. Those fields use standard/PPR presets and will misvalue this league specifically (6-point passing TDs, granular DST scoring, and whatever the four still-open schema questions resolve to). This means: for each of the player's last N games, compute per-game points via a shared scoring function (see §4), then average those.

### 3.4 Matchup difficulty (defense vs. position) — build this second, and simpler than it sounds

No source has this as a field (confirmed in CLAUDE.md's Data Sources table), so it has to be computed from the same raw player-stat data, as-of the target week:

1. For each team, for each position, for each week already played this season: sum the league-scoring points that *opposing* players at that position scored against that team, using `opponent_team` to attribute games.
2. Average that across the team's games played so far this season to get "points allowed to position X, trailing average."
3. Compare that to the league-wide average points allowed to position X over the same window, and express the defense as a multiplier (e.g., 1.15 = allows 15% more than league average to that position; 0.85 = 15% less).
4. Apply that multiplier to the player's rolling average from §3.2 to get the final projection: `projection = rolling_avg * opponent_multiplier`.

Two things to watch: early-season defense samples are small and noisy (same cold-start problem as §3.2 — a defense's week-1-4 "points allowed" is not very predictive yet, regress toward 1.0 when the sample is thin, e.g., blend with league average weighted by games played), and this whole step is genuinely optional for a first working version — a rolling average alone is already a legitimate, explainable projection. Consider shipping without §3.4 first, get the value engine and report view working end to end, then add the multiplier as a visible second step once there's something to compare it against.

## 4. Scoring engine — build this as a shared, standalone function first

Both this tier and the FantasyPros tier need to turn raw stat lines into league-specific points (CLAUDE.md: "never trust a vendor's precomputed points field directly"). Build one function, `compute_league_points(stat_line: dict, scoring_config: dict) -> float`, used by both. `scoring_config` should be a plain data structure (JSON/dict) mapping stat category → point value, matching whatever the configurable schema (Next Steps item 3) ends up defining — e.g. `{"pass_td": 6, "pass_yd": 0.04, "reception": 1, "fumble_lost": -2, ...}`. Since the real league's values aren't final, wire this up against a reasonable placeholder config (the 2025 screenshotted rules are a fine placeholder) and unit-test it independently of the projection logic — that way, when the real values land, updating the config doesn't require touching the projection code at all. This also directly de-risks Next Steps item 3: once you have this function, testing a candidate scoring config against known historical outcomes is exactly the sanity check in item 7.

## 5. Output contract

Whatever function/module this becomes should return, per player per week, at minimum: `player_id`, `week`, `projected_points`, `games_used` (how many games the rolling average was built from — this is the confidence signal from §3.2), and `opponent_multiplier` (if §3.4 is implemented, else omit or default to 1.0). Keep `games_used` in the output even if the report view doesn't display it yet — the eval layer (Next Steps item 8) will want it to break down accuracy by confidence tier later (e.g., "how accurate are 4-game-average projections vs. 1-game-average projections" is a genuinely interesting thing to show in the case study).

## 6. Testing

- Unit-test `compute_league_points()` against hand-built stat lines with a known expected output — this is cheap and catches scoring-formula bugs before they propagate into every projection.
- Unit-test the rolling-window logic against a small synthetic game log (including a bye week and a rookie's first game) to confirm the "games played, not weeks elapsed" and cold-start behavior actually work as described in §3.2.
- Defer full-scale validation to the manual sanity check already planned in CLAUDE.md Next Steps item 7 (2–3 real historical weeks, hand-computed) — that's the right point to check whether the rolling average actually tracks reality, not before the model exists.

## 7. Open questions for you, not decided here

- Window size (N=4 games) is a starting guess, not a researched number — worth a quick gut-check against how volatile week-to-week fantasy output actually is for a few real players once you have data to look at, but not worth researching in the abstract before building.
- Whether §3.4 (matchup difficulty) ships in the same pass as §3.2/§3.3 or gets deferred — see §1 and §3.4's closing paragraph. My recommendation is defer it; wanted to flag it as a decision rather than assume.
- How much of §3.2's role-change discontinuity problem is worth solving with more than "surface the game log to the human" — e.g., a manual override/flag mechanism in the report view. Worth deciding once you're doing the frontend/journey work (Next Steps item 1), since it's really a UX question more than a modeling one.
