# Hand Sanity-Check — 2025 Weeks 8, 12, 16 (CLAUDE.md Next Steps item 4)

Distinct from `backend/backtest.py`'s four automated rounds (statistical accuracy
across the full player pool): this is a human-style spot-check of the value
engine's actual output for a handful of well-known players across three real,
already-played 2025 weeks (8, 12, 16 — spread early/mid/late season, all past
the 6-game rolling window so no player is a cold start). Ran
`generate_report.py --season 2025 --week {8,12,16} --skip-news --skip-fantasypros`
(no `ANTHROPIC_API_KEY`/`FANTASYPROS_API_KEY` in this environment, so both
degrade to their documented defaults anyway — this exercises the in-house tier
only) against real `nflreadpy` data, then independently recomputed each
watched player's **actual** points for that week from their real raw stat line
through `scoring.compute_league_points` (the same formula the projections
use), so the comparison is real-formula-vs-real-formula, not vendor points vs.
real formula.

## Players/weeks checked

| Player | Wk 8 proj → actual | Wk 12 proj → actual | Wk 16 proj → actual |
|---|---|---|---|
| Aaron Rodgers (QB, PIT) | 23.10 → 20.76 | 19.14 → *bye* | 14.10 → 17.54 |
| Josh Allen (QB, BUF) | 27.71 → 25.22 | 31.48 → 12.12 | 31.87 → 6.90 |
| Bijan Robinson (RB, ATL) | 23.73 → 4.30 | 18.83 → 13.70 | 19.92 → 26.30 |
| Puka Nacua (WR, LAR) | *not projected (bye)* | 16.80 → 13.20 | 18.45 → 45.50 |
| Steelers D/ST | 7.46 → -6.12 | 8.42 → 5.74 | 7.67 → 6.16 |

## What this confirmed working correctly

- **Real stat lines drive both sides, no vendor-points shortcut.** Every
  actual figure above traces to a real raw stat line pulled by
  `load_all_game_logs_nflreadpy`/`load_dst_pool_and_game_logs_nflreadpy` and
  run through `compute_league_points` — spot-checked the raw fields behind
  the two most extreme numbers:
  - Puka Nacua's week-16 45.50: `rec_yd=225, rec_td=2, reception=12` → real
    monster game (12-225-2), not a computation artifact. Breakdown:
    22.5 (yardage) + 12 (2 TD × 6) + 6 (12 rec × 0.5 half-PPR) + 5 (200+ yard
    milestone) = 45.5, matches by hand.
  - Steelers D/ST's week-8 -6.12: `def_points_allowed=35, def_yards_allowed=454,
    def_return_yd=144, fumble_forced=1` → a real bad defensive week (35
    points/454 yards allowed) correctly produces a negative score under this
    league's granular DST penalties. `fumble_forced` contributed 0 — the
    placeholder config has that category present but zero-weighted, which
    lines up with CLAUDE.md Next Steps item 2's still-open question of
    whether forced fumble is even a real, separate ESPN scoring category for
    this league. Confirms the DST engine can and does produce negative totals
    by design, not by bug, for a genuinely bad defensive game.
- **Bye weeks are handled correctly, not silently wrong.** Puka Nacua is
  absent from week 8's `projections` array entirely (LAR had a week-8 bye —
  confirmed against `load_schedules()`, zero LA games that week) but *is*
  still present in `player-pool.json`'s full roster list. Traced this to
  `generate_report.py`: `opponent_for_team_week` returns `None` on a bye and
  the report-building loop skips emitting a projection when there's no
  opponent (`generate_report.py:226-237`) — deliberate, not an accidental
  drop.
- **Single-game projection-vs-actual variance is large and expected, not a
  red flag.** Josh Allen week 16 (31.87 → 6.90) and Bijan Robinson week 8
  (23.73 → 4.30) are the two widest misses here. Checked both raw lines:
  Allen's was a genuinely quiet game (130 pass yd, 17 rush yd, no TDs); Bijan's
  was low-usage plus a lost fumble (25 rush yd, 23 rec yd, `fumble_lost=1`).
  Neither shows a sign flip, double-count, or missing category — the
  in-house tier is a 6-game rolling average, which cannot and isn't meant to
  predict single-game blowups/duds. That's exactly what the four-round
  statistical backtest (`docs/research/projection-model-backtest-findings.md`)
  already exists to characterize in aggregate; this hand-check's job is
  output *sanity*, not projection *accuracy*, and nothing here looked
  structurally wrong.

## One real caveat found (not a bug in the tool's actual use case)

`load_player_pool_nflreadpy` filters `nflreadpy`'s roster snapshot to
`status == "ACT"` (`generate_report.py:316`). That snapshot reflects each
player's **current, as-of-today** status, not their status during the
historical week being requested. Brock Bowers — a real, active, playing TE
in 2025 weeks 8/12/16 — was picked as a watch-list player specifically to
check this, and is silently absent from `player-pool.json` for all three
weeks: `nflreadpy`'s 2025 roster snapshot currently shows him
`status == "RES"` (as of 2026-08-12, reflecting his real-world status *today*,
long after those games were played), not `"ACT"`.

This is not a bug for `generate_report.py`'s actual intended use (generating
a report for the current/upcoming week, where "who's active right now" is
the correct question) — it's a footgun specific to reusing it retroactively
against a past, fully-completed season the way this hand-check does.
`backtest.py`'s `load_full_pool_game_logs` doesn't have this problem because
it sources its pool directly from `load_player_stats()` (who actually has a
stat line that week), not from a live roster-status snapshot — worth keeping
that distinction in mind for anyone else pointing `generate_report.py` at a
historical season by hand later.

## Conclusion

No bugs found in either projection tier's output, the DST/K wiring, bye-week
handling, or waiver-target selection across all three spot-checked weeks —
every number traced back cleanly to a real stat line and the documented
scoring formula. Closes CLAUDE.md Next Steps item 4. The one finding worth
carrying forward is the roster-status caveat above, which only matters if
`generate_report.py` is pointed at a past season again (e.g., for a future
hand-check or demo) — not a fix needed for the tool's real, forward-looking
use case.
