# Kicker (K) Scoring Field Coverage — Resolved (2026-08-12)

`scoring_config.placeholder.json`'s `_kicker_note` flagged a real gap: FG-make/miss and
PAT categories exist as placeholder point values, but no nflreadpy column mapping or
research doc like `docs/research/dst-scoring-fields.md` existed for them. Probed hands-on
against real 2025 (and a 2024 sample for the blocked-kick edge case) `nflreadpy` data,
same method as the DST probe.

## Which table kickers show up in

**`load_player_stats()`** — the same per-player table QB/RB/WR/TE game logs already come
from, not `load_team_stats()`. Kickers are just another `position` value (`"K"`) in that
table (569 K rows in the 2025 season alone). This means kicker stat-line assembly reuses
the exact same per-player, per-week row shape `scoring.nflreadpy_row_to_stat_line` already
expects — no self-join needed, unlike DST. `load_rosters()` also carries kickers under
`position == "K"` with a real `gsis_id`, confirmed against 33 real active 2025 kickers
(e.g. Chris Boswell → `00-0031136`) — so K gets a real, stable `playerId` the same way
QB/RB/WR/TE already do, not an invented ID.

## Result: direct fields cover every category, at finer distance granularity than the config expects

| League K need | Coverage | Detail |
|---|---|---|
| Field goals made, by distance | **Direct fields, finer buckets than the config** | `fg_made_0_19`, `fg_made_20_29`, `fg_made_30_39`, `fg_made_40_49`, `fg_made_50_59`, `fg_made_60_` — six real distance buckets, not the config's three (`fg_made_0_39`/`fg_made_40_49`/`fg_made_50_plus`). The six buckets nest cleanly inside the config's three (0–19 + 20–29 + 30–39 → `fg_made_0_39`; 40–49 passes through as-is; 50–59 + 60+ → `fg_made_50_plus`), confirmed lossless against real rows — no distance information is lost or approximated by re-bucketing, just aggregated. |
| Field goals missed | **Direct field** | `fg_missed` — already a single aggregate total, not distance-bucketed the way makes are (distance-bucketed miss fields also exist — `fg_missed_0_19` etc — but the config has only one flat `fg_missed` category, so the aggregate is what's used, same "don't invent a category the config doesn't have" discipline as everywhere else in this project). |
| Field goals blocked | **Direct field, but no config category exists for it on the kicker's side** | `fg_blocked` is its own field, separate from `fg_missed` — confirmed against 64 real 2024–2025 blocked-kick rows (e.g. Joey Slye, 2024 week 2: `fg_att=3, fg_made=2, fg_missed=0, fg_blocked=1` — `fg_att` splits three ways, a block is not folded into makes or misses upstream). The config has a DST-side `def_blocked_kick` category but no kicker-side one. **Modeling decision** (see Conclusion): mapped into `fg_missed` for the kicker's own stat line, same "reasonable placeholder call, documented" treatment `fantasypros.py` gave its own ambiguous `fumbles` field. |
| PATs made/missed | **Direct fields** | `pat_made`, `pat_missed` — map 1:1 to the config's categories. |
| PATs blocked | **Direct field, same gap as FG blocks** | `pat_blocked` exists as its own field, separate from `pat_missed`. Same modeling decision: folded into `pat_missed`. |

## Conclusion

No data gap — nflreadpy's `load_player_stats()` carries everything the league's kicker
scoring needs, at *finer* distance granularity than `scoring_config.placeholder.json`
currently models (6 real buckets vs. the config's 3 bands), which aggregate down cleanly
with no loss. The one real gap is on the *config's* side, not the data's: FG/PAT blocks
are their own tracked outcome in the real data (distinct from a normal miss), but the
config has no `fg_blocked`/`pat_blocked` category for the kicker to be charged against —
only the DST side charges/credits blocks. Per this task's scope (stat-line assembly
plumbing only, not a config-schema change — that's CLAUDE.md Next Steps item 2/the
commissioner's call), blocks are folded into `fg_missed`/`pat_missed` in
`kicker.py`'s assembly rather than silently dropped or given their own unscored category.
Flagged here and in `kicker.py`'s docstring so this is a visible modeling choice, not a
silent one, and easy to un-fold into a real `fg_blocked` category later if the real league
scoring rules turn out to define one.

This closes the kicker half of CLAUDE.md Next Steps item 3 ("Wire in DST/K"), alongside
`docs/research/dst-scoring-fields.md` for the DST half.
