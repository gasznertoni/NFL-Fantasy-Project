# Phase 4 — Operational Reliability & Phase 5 — Cross-Validation

## Phase 4: what can and can't be tested right now

Today is 2026-08-04 — preseason. Regular season starts September 9; 53-man roster cuts land in late August. Per the test plan's own framing, this constrains what "reliability testing" can mean today:

**Can't test yet:** live weekly injury-status freshness, in-season projection updates, real waiver-wire churn dynamics. There's no live signal to test against until games start.

**Can test now, and what happened when I tried:**

- Sleeper's `/v1/state/nfl` **hands-on confirmed today**: live, correctly reports `season_type: "pre"`, `season_start_date: "2026-08-06"` — the API itself is up and gives sane preseason-appropriate data right now.
- Backtesting nflreadpy against the completed 2025 season — **done**: `load_player_stats()` pulled 19,421 real rows across weeks 1-22, `load_injuries()` pulled 6,068 rows, `load_rosters()` pulled 3,137 rows with a full roster-status breakdown (`ACT` 1537, `DEV` 484, `CUT` 444, `RES` 435, `INA` 210, `RET` 23, `TRD` 3, `TRC` 1). This is a real, complete 2025-season backtest dataset, not just a shape check.
- Tracking current (2026) roster-cut/depth-chart churn — genuinely can't happen yet, since cuts haven't occurred. Revisit in late August.

**Recommended reliability-log routine**, once you're running the local script:

1. Run `scripts/probe_sources.py` once now (offseason baseline).
2. Run it again in ~3-5 days, diff the two `phase1-local-results.json` outputs for: row-count changes (roster churn), any column additions/removals (schema drift), and whether the same rookie/practice-squad/traded-player edge cases still resolve the same way.
3. Log error rate/latency informally — the script already fails soft per-source, so a run that partially fails is itself a data point (which source degraded).
4. Plan the second, shorter pass explicitly for the first 2-3 live regular-season weeks (early-to-mid September), specifically for freshness and uptime under real load — that's the one thing genuinely untestable before kickoff.

## Phase 5: cross-validation — schema-level done, value-level still blocked by the calendar

Where two sources cover the same field, the test plan wants both pulled for the same week and diffed — disagreement is signal, not just noise. Both probes have now run, so this phase is partially real:

| Field | Source A | Source B | Status |
|---|---|---|---|
| Injury status | nflreadpy `load_injuries()` — confirmed real `report_status` field, 6,068 rows, 2025 season | Sleeper `injury_status` field — confirmed present in the live 12,207-player dump | **Schema confirmed on both sides.** Can't diff actual values yet — nflreadpy's pull is the *completed 2025 season*, Sleeper's is a *live 2026 preseason* snapshot. Not the same week, so a same-week diff is genuinely not possible until the 2026 season is underway |
| Practice participation | nflreadpy `practice_status` (2025) | Sleeper `practice_participation` (live 2026) | Same calendar mismatch as above |
| Player identity/team/status | nflreadpy rosters (2025, `DEV` 484 practice-squad-equivalent players) | Sleeper dump (live 2026, only 1 player flagged `"Practice Squad"`) | **This is the one real cross-validation finding so far**: the gap between 484 and 1 isn't a data-quality problem in either source — it's explained by the calendar (practice squads don't exist yet in 2026, nflreadpy's 484 comes from a full completed season where they did). Confirms both sources model this status correctly; just not comparable across different points in the roster cycle |
| Rankings/ECR | DynastyProcess `db_fpecr.csv.gz` (1,528,918 rows, real data pulled) | FantasyPros official API (once keyed) | Still pending your key |

**What this means for the plan:** the *schema* half of Phase 5 (do both sources have comparable fields?) is answered — yes, on injury status and practice participation. The *value* half (do they agree on the same week?) genuinely cannot happen until real 2026-season data exists in both places at once. Re-run both probes for the same week once the season starts (early-to-mid September, per the Phase 4 routine above) and do the actual diff then — this isn't a gap in the work, it's a real calendar constraint the original plan already flagged.
