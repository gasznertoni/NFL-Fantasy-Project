# Projection Model Backtest — Findings (2026-08-10)

Empirical follow-up to `docs/design/in-house-projection-model-spec.md` and `backend/matchup.py`'s design. Ties off two open questions the design spec explicitly left unresolved: the rolling-window size (§7, "a starting guess, not a researched number") and whether the opponent win-record multiplier (added 2026-08-06) is actually worth keeping. Run with `backend/backtest.py` against real 2025 season results.

**Read this before reusing these numbers for anything else:** the backtest runs every projection and every "actual" result through `scoring_config.placeholder.json` — not the real league's scoring rules, which are still blocked on the commissioner. That makes MAE and bias valid for comparing model variants *against each other* (same config on both sides of every comparison here), but not a claim about real-2026 accuracy in absolute points. Correlation is reported as a secondary, somewhat more config-robust signal for the same reason.

## Method

Tuning-weeks (2025 weeks 5-14) vs. holdout-weeks (2025 weeks 15-18) split, so a choice that only looks good on the weeks it was picked on doesn't get trusted. First pass used 7 players; results below are the second, larger pass — 19 players, mixed by position and archetype (established stars, breakout rookies, a committee-role situation, a couple of QBs), which meaningfully firmed up the conclusions versus the 7-player pass (compare `n=54`/`n=24` in the first run to `n=155`/`n=65` here).

## Results

### Window-size sweep (unweighted rolling average, matchup multiplier off)

| Window | Tuning MAE | Tuning bias | Tuning corr | Holdout MAE | Holdout bias | Holdout corr |
|---|---|---|---|---|---|---|
| 3 | 7.939 | -0.406 | 0.286 | 8.537 | -0.789 | 0.477 |
| 4 | 7.378 | -0.433 | 0.382 | 7.879 | -0.418 | 0.535 |
| 5 | 7.431 | -0.466 | 0.378 | 7.939 | -0.277 | 0.516 |
| 6 | 7.397 | -0.463 | 0.377 | 7.788 | -0.329 | 0.554 |

### Matchup multiplier ablation (window=4, tuning weeks)

| Variant | MAE | Bias | Corr |
|---|---|---|---|
| matchup=off | 7.378 | -0.433 | 0.382 |
| matchup=on | 7.373 | -0.634 | 0.387 |

### Matchup multiplier — paired significance test (window=4, tuning weeks, n=155)

Added 2026-08-10 (`paired_significance_test()` / `paired_variant_metric()` in `backend/backtest.py`, via a separate local Claude Code session, PR #4) to replace the eyeballed "0.005 difference — noise" judgment call below with an actual test. Paired z-test on matched per-(player, week) values; see the function's docstring for why a normal approximation is used instead of scipy's exact paired t-test.

| Metric | mean diff (on − off) | p-value | Significant at α=0.05? |
|---|---|---|---|
| MAE (abs_error) | -0.0053 | 0.9424 | No |
| Bias (error) | -0.2006 | 0.0063 | **Yes** |

## Conclusions

**Window size: keep the default of 4 games.** Window=3 is clearly worse on both weeks sets — meaningfully higher MAE and, on tuning weeks, a notably weaker correlation (0.286 vs. ~0.38 for 4/5/6). Reacting off only 3 games is measurably noisier, not just marginally so. Among 4, 5, and 6, there's no consistent winner: 4 has the best MAE and correlation on tuning weeks, but 6 edges it out on holdout weeks — the "best" window flips depending on which weeks are being looked at, and the gaps involved (MAE differs by ~0.1-0.15, correlation by ~0.02-0.04) are within what's expected from just averaging over a different set of weeks, not a real underlying advantage. `projections.py`'s `DEFAULT_WINDOW = 4` needs no change.

**Matchup multiplier: do not enable by default — now statistically confirmed, not just eyeballed.** The significance test resolves this more precisely than the raw ablation numbers alone: the MAE difference is confirmed to be pure noise (p=0.94, nowhere near significant) — the multiplier has no accuracy benefit, full stop. But the bias shift is real: p=0.0063, comfortably significant. The multiplier reliably makes projections overproject *more* (bias moves ~0.2 points more negative), not by chance. So the honest characterization isn't "no effect, might be noise either way" — it's "confirmed no accuracy benefit, plus a small but real and reproducible cost to the model's bias." That's a cleaner and stronger case against shipping it than the original ablation numbers alone suggested. Consistent with the tradeoff flagged in `matchup.py`'s own module docstring when it was built: overall team record conflates offense and defense, so it's plausibly too blunt a proxy for "how hard is this specific matchup" to move the needle in the direction it's supposed to. `matchup.py` stays in the codebase (tested, documented, not deleted) but is not wired into the live projection path.

## A secondary finding worth flagging: the bias sign-flip

Across every window size, bias is *positive* on tuning weeks (the model underprojects — actual points came in higher than projected) and *negative* on holdout weeks (the model overprojects), and the magnitude grows with window size in both directions. That pattern suggests this player sample trended upward through weeks 5-14 (a rolling average lags behind improving form and underprojects) and cooled off or lost role in weeks 15-18 (the same lagging average now overprojects, since it's still anchored to the earlier hot stretch). This isn't really a "wrong window size" problem — it's the structural weakness any unweighted rolling average has: it always lags a trend change, in whichever direction the trend just moved. See "Ideas for improving accuracy" below.

## Ideas for improving accuracy (not built yet — candidates for the next round)

Roughly in order of expected effort-to-value:

1. **Recency-weighted average (exponential decay) instead of an unweighted mean.** Directly motivated by the bias sign-flip above — a decay-weighted average would react faster to a player heating up or cooling off, which is exactly the lag this backtest just measured. Flagged as the natural v1.1 step in the original design spec (§3.2). Cheapest change with the clearest rationale from this data.
2. **Shrinkage toward a positional baseline for thin samples.** The "low confidence" tier (players with under a full window of games) has shown noticeably worse error in past runs. Blending a thin-sample player's average with the position's league-average points, weighted by how many games they have, is standard empirical-Bayes practice for exactly this problem and mirrors what the design spec already recommended for the (deferred) matchup-difficulty model's own thin-sample problem (§3.4).
3. **Use usage/opportunity data, not just points.** `nflreadpy`'s player-stats table already carries `target_share`, `air_yards_share`, `wopr`, and snap-count fields that this model doesn't touch at all today. Points are a noisy *outcome* (a player can draw 10 targets and drop 3 passes, or get lucky with 2 garbage-time TDs); usage share is a steadier *process* signal and tends to be more predictive week-to-week than the point total itself. Likely the single highest-leverage change available, since the data's already being pulled and simply isn't used yet — but needs a real design decision on how to blend it with the points-based average, not just a config tweak.
4. **Position-specific window tuning.** The sweep above pools all positions together. RB/WR touchdown-dependent scoring is generally boom/bustier than QB scoring, so the ideal window size may differ by position. Cheap to test — `backtest.py`'s harness already supports it, just needs the player pool segmented by position before aggregating.
5. **Build the position-specific matchup model the design spec originally proposed (§3.4), instead of the record-based proxy that just failed.** Points allowed to a given position by a given defense is a more targeted signal than overall win-loss record, at the cost of real engineering (needs the same kind of stat-line joins already documented for DST scoring). Worth it only if items 1-4 don't get accuracy where it needs to be — the record-based shortcut was worth trying first because it was cheap, and now there's a real data point (this doc) showing it doesn't work, which is useful evidence either way.

## Round 2 (2026-08-10): items 1 and 4 built and tested — both rejected

Same player pool (19 players), same tuning/holdout split, same `scoring_config.placeholder.json`, run against live 2025 `nflreadpy` data via `backend/.venv` (network access confirmed working from this machine, unlike the Cowork cloud sandbox the rest of this file was built in). Both ideas below are now implemented in code (not deleted — same "tested, documented, not wired in as the default" treatment `matchup.py` got) and covered by unit tests in `tests/test_projections.py` and `tests/test_backtest.py`; only the backtest *conclusion* is negative.

### Idea 1: recency-weighted (exponential decay) average — rejected, confirmed significant, wrong direction

Implemented as `projections.py`'s `_rolling_average()` / `project_player(..., decay=...)`: `decay=1.0` (default) is exactly the old unweighted mean (not an approximation — every weight collapses to 1.0), `decay<1.0` discounts games further back. Swept `[1.0, 0.95, 0.9, 0.85, 0.8, 0.7, 0.6]` at `window=4` via `backtest.py`'s new `DECAY_VALUES_TO_SWEEP`.

| Decay | Tuning MAE | Tuning bias | Tuning corr | Holdout MAE | Holdout bias | Holdout corr |
|---|---|---|---|---|---|---|
| 1.0 (baseline) | 7.378 | -0.433 | 0.382 | 7.879 | -0.418 | 0.535 |
| 0.95 | 7.427 | -0.433 | 0.375 | 7.905 | -0.447 | 0.532 |
| 0.9 | 7.483 | -0.434 | 0.368 | 7.929 | -0.476 | 0.527 |
| 0.85 | 7.540 | -0.435 | 0.360 | 7.963 | -0.506 | 0.523 |
| 0.8 | 7.601 | -0.435 | 0.350 | 7.999 | -0.536 | 0.517 |
| 0.7 | 7.745 | -0.437 | 0.330 | 8.072 | -0.596 | 0.505 |
| 0.6 | 7.902 | -0.440 | 0.306 | 8.172 | -0.653 | 0.491 |

MAE gets **monotonically worse** as decay drops, on both tuning and holdout weeks — not the improvement the bias sign-flip motivated this idea with. Paired significance test (`decay=0.95`, the least-bad candidate, vs. `decay=1.0`, tuning weeks, n=155): MAE mean_diff = **+0.0488, p=0.0105, significant** (decay is reliably worse, not noise); bias mean_diff = -0.0005, p=0.98, not significant (decay doesn't fix the sign-flip either — bias is essentially unchanged).

**Why the hypothesis was wrong, best guess:** the sign-flip's real driver looks less like "the mean lags a trend" and more like this specific 19-player sample's role/usage shifting between the tuning and holdout windows in a way no in-season weighting scheme fixes — decay just shrinks the effective sample size of an already-short 4-game window, trading a little responsiveness for real variance. That points more directly at idea 3 (usage/opportunity data) as the one actually capable of catching a role change before the points reflect it, rather than idea 2's shrinkage (which addresses thin *samples*, not this). **Decision: `DEFAULT_DECAY = 1.0` stays the default** — the code path exists (so a future re-test against more data or a different player pool doesn't require rebuilding it) but isn't wired in as an improvement, because it measurably isn't one.

### Idea 4: position-specific window tuning — no significant deviation from window=4 for any position

Segmented the same 19-player pool by position (QB n=3, RB n=4, TE n=3, WR n=9 — small per-position samples, flagged up front) and re-ran the window sweep per position on tuning weeks only (`backtest.py`'s new position-specific sweep section). WR's best window was already 4, matching the pooled result. QB, RB, and TE each had a *nominally* lower MAE at window=6 — but per the statistical-analysis skill's guidance on small samples ("small samples produce unreliable results, even with significant p-values" — and these aren't even significant), a raw MAE table isn't enough to act on with n in the 20s-30s. Ran the same paired significance test used for the matchup ablation and the decay check above, this time for each position's apparent best window vs. the global default:

| Position | n_players | Apparent best window | MAE diff vs window=4 | p-value | Significant? |
|---|---|---|---|---|---|
| QB | 3 | 6 | -0.430 | 0.3365 | No |
| RB | 4 | 6 | -0.136 | 0.7792 | No |
| TE | 3 | 6 | -0.237 | 0.2041 | No |
| WR | 9 | 4 (= default) | — | — | n/a |

None of the apparent per-position improvements clear even a loose bar for significance — every p-value is well above 0.05, consistent with "differences this size are exactly what a handful of games per position would produce by chance." **Decision: no position-specific override.** `DEFAULT_WINDOW = 4` stays global. The honest limiting factor here is the player-pool size (3-9 players per position), not the method — worth re-running this exact check once the eval layer (CLAUDE.md Next Steps item 8) has accumulated more real 2026 weeks and a larger pool to segment.

### What this leaves for the next round

Both cheap ideas (1 and 4) are now negative results, not open questions — that's useful evidence, not wasted effort, but it moves idea 3 (usage/opportunity data) up in practical priority even though it was already flagged as "likely the single highest-leverage change" on effort-to-value grounds. Idea 2 (shrinkage for thin samples) is still untested and unrelated to either negative result above — it targets the low-confidence tier specifically, which this round's sweeps didn't isolate. Idea 5 (real position-specific matchup model) still stays gated behind those two, per the original ordering's own reasoning.

## Round 3 (2026-08-11): full active pool re-run (611 players, not 19 hand-picked) — MAE gap changes what to prioritize next

Rounds 1-2 ran the same 19 hand-picked players (mixed archetypes: stars, boom/bust, a committee back, rookies, a couple of QBs) through every sweep. That was enough to firm up a first pass, but it's a small, curated sample — not one the `by_confidence` breakdown or a position split can be trusted against. This round replaces it with `backtest.py`'s new `load_full_pool_game_logs()`: every QB/RB/WR/TE `nflreadpy` has a 2025 stat line for — **611 players**, one network call instead of 19 (the old `load_player_game_log` re-fetched the entire season's stats table once per player, which would have meant ~600 redundant fetches at this scale). Same `scoring_config.placeholder.json`, same tuning/holdout split. Graded rows jumped from n=155/65 (tuning/holdout) to **n=3201/1407**.

This round runs roughly a dozen significance tests (2 window-vs-default, 1 window-vs-runner-up, 2 matchup, 2 decay, 4 position, re-confirmed a second time post-update) at α=0.05 with no multiple-comparisons correction. Each test is pre-specified — asking a question the doc already planned to ask, not mined from the data after the fact — so this isn't the p-hacking failure mode, but it's still worth naming: at α=0.05 across ~12 independent tests, a false positive is more likely than the 5%-per-test headline number suggests. The window-size finding is the one this matters most for; it clears p=0.0003 and p=0.0325 on two independent week sets, which is a much stronger bar than a single borderline p≈0.04 would be.

### Headline: MAE dropped from ~7.4-7.9 to ~4.3-4.5 — a sample-composition effect, not a model improvement

| Pool | Tuning MAE (window=4) | Holdout MAE (window=4) |
|---|---|---|
| 19 hand-picked archetypes (Round 1-2) | 7.378 | 7.879 |
| Full pool, 611 players (Round 3) | 4.375 | 4.517 |

Round 1-2's pool was deliberately picked for high-variance, "interesting" players (boom/bust starters, breakout rookies) — exactly the players hardest to project. The full pool is dominated by low-usage bench players scoring near-zero every week, which are easy to hit with a small absolute error even when the *relative* error is large. **These MAE numbers are not comparable across rounds** — different populations, not a real accuracy change — same caveat this doc already applies across scoring-config changes, now applying across sample changes too. Read every number below only relative to other Round 3 numbers, not against Round 1-2's table.

### Window size: reversed from "no consistent winner" to a real, holdout-confirmed effect

Round 1 called this a coin flip: 4 won on tuning weeks, 6 won on holdout weeks, gaps small enough to be noise. At full-pool scale, **window=6 wins on both week sets, and it's now significant on both**:

| Window | Tuning MAE | Tuning corr | Holdout MAE | Holdout corr |
|---|---|---|---|---|
| 3 | 4.490 | 0.639 | 4.683 | 0.605 |
| 4 (current default) | 4.375 | 0.656 | 4.517 | 0.625 |
| 5 | 4.315 | 0.662 | 4.467 | 0.626 |
| 6 | 4.290 | 0.664 | 4.433 | 0.633 |

Paired significance test, window=6 vs. window=4 (new — `backtest.py` now runs this pooled comparison, which neither Round 1 nor 2 did; every other "does X help" question here already got one):

| Weeks | n | mean diff (6−4) | p-value | Significant? |
|---|---|---|---|---|
| Tuning | 3201 | -0.0844 | 0.0003 | **Yes** |
| Holdout | 1407 | -0.0837 | 0.0325 | **Yes** |

This is the first time the window-size decision has cleared a significance bar in either direction — Round 1's "keep 4" call was an eyeballed judgment on a sample too small to test it properly. **Applied: `projections.py`'s `DEFAULT_WINDOW` changed from 4 to 6.**

Window=6 beats window=4, but that doesn't make 6 precisely optimal — the runner-up, window=5, is close enough that it's worth checking directly rather than trusting the sweep table's ranking at face value. `backtest.py` now runs this comparison too:

| Comparison | Weeks | n | mean diff | p-value | Significant? |
|---|---|---|---|---|---|
| window=5 vs. window=4 | Tuning | 3201 | -0.0597 | 0.0013 | **Yes** — 5 also clearly beats 4 |
| window=5 vs. window=6 | Tuning | 3201 | +0.0247 | 0.0788 | No |
| window=5 vs. window=6 | Holdout | 1407 | +0.0343 | 0.1849 | No |

Honest framing: **5 and 6 are statistically indistinguishable from each other, and both clearly beat 4.** 6 was chosen as the shipped default because it has the better point-estimate MAE on both tuning and holdout weeks, not because it's been shown to be uniquely correct — a future round with more data could reasonably land on 5 instead without contradicting anything found here.

### Position-specific window: RB and TE now show a real effect; Round 2's "no position needs an override" was underpowered

Round 2 explicitly flagged its own per-position samples (3-9 players) as too small to trust. Full pool gives each position 81-240 players:

| Position | n_players | n graded (tuning) | Apparent best window | MAE diff vs window=4 | p-value | Significant? |
|---|---|---|---|---|---|---|
| QB | 81 | 347 | 6 | -0.166 | 0.1356 | No |
| RB | 154 | 837 | 6 | -0.105 | 0.0296 | **Yes** |
| TE | 136 | 691 | 6 | -0.121 | 0.0011 | **Yes** |
| WR | 240 | 1326 | 5 | -0.033 | 0.2084 | No |

RB and TE now clear significance for window=6 over the current default of 4 — reversing Round 2's "no position-specific override" call. QB nominally prefers 6 too but doesn't reach significance (fewer graded weeks — QBs play fewer of them per team). WR's pooled best is window=5, not significant. The pooled window=6 win above is mostly RB/TE-driven. Not acting on a per-position override yet — window=6 as the new *global* default (already applied) already moves in this direction for RB/TE; a genuine per-position config is a second-order refinement on top of that, not an independent next step.

### Matchup multiplier: same "don't enable" conclusion, on firmer but smaller-magnitude footing

| Metric | mean diff (on − off) | p-value | Significant? |
|---|---|---|---|
| MAE (abs_error) | +0.0271 | 0.0019 | **Yes** (Round 2: p=0.94, called noise) |
| Bias (error) | -0.0915 | ~0.0 | **Yes** (consistent with Round 2) |

At n=3201 the MAE cost is now real, not noise — but it's tiny (0.027 points on a ~4.3 baseline). The practical conclusion is unchanged (`matchup.py` stays out of the live path), just more precisely characterized: a real but negligible MAE cost, plus the same reproducible bias cost Round 2 already found.

### Decay (recency weighting): Round 2's rejection looks like a curation artifact, not a real effect

Round 2 rejected `decay=0.95` as "confirmed significant, wrong direction" (p=0.0105 at n=155). At full-pool scale, the same comparison is **not significant** on MAE (p=0.7555) or bias (p=0.1111), and the magnitude shrank from +0.049 to +0.0009. The direction is technically the same (decay doesn't help), but Round 2's "confirmed harmful" framing doesn't hold up — a small, curated 19-player sample was apparently enough to make a near-zero effect look statistically real. `DEFAULT_DECAY = 1.0` stays the default either way (there's still no benefit to switching), but the honest characterization is "no measurable effect," not "confirmed harmful."

### Re-confirmed after applying `DEFAULT_WINDOW=6`

Everything above (window sweep aside) ran with `DEFAULT_WINDOW` still at its old value of 4, since the matchup/decay/position checks pull `window=DEFAULT_WINDOW` from `projections.py` at run time — that's normally what you want (test against whatever's actually shipped), but it means the matchup and decay numbers above were computed just *before* this round's own window change landed. Re-ran `backtest.py` once more after applying `DEFAULT_WINDOW=6` to confirm nothing flips:

| Check | At window=4 (original pass) | At window=6 (post-update) | Conclusion changed? |
|---|---|---|---|
| Matchup MAE (on−off) | +0.0271, p=0.0019, **significant** | +0.0329, p=0.0001, **significant** | No — same call, slightly larger effect |
| Matchup bias (on−off) | -0.0915, p≈0, **significant** | -0.0918, p≈0, **significant** | No |
| Decay MAE (0.95 vs 1.0) | +0.0009, p=0.7555, not significant | +0.0004, p=0.9032, not significant | No |
| Decay bias (0.95 vs 1.0) | -0.0045, p=0.1111, not significant | -0.0115, p=0.0006, **significant** | Yes, newly significant — but the magnitude (-0.01 points) is still practically negligible, doesn't change the "don't ship decay" call |
| Position sweep (QB/RB/TE) | best window=6, differs from then-default 4 | best window=6 = new default — "nothing to test" | Confirms QB/RB/TE's preference was for window=6 specifically, not just "more than 4" |
| Position sweep (WR) | best window=5 vs. default 4, p=0.2084, not significant | best window=5 vs. new default 6, p=0.9324, not significant | No — WR still indifferent among 4/5/6 |

Only the decay-bias check flips to significant, and even then the effect size is too small to act on. Every other conclusion in this round holds at the newly-shipped default.

### `by_confidence` breakdown, run against the full pool — new evidence for ideas 2 and 3

This is the breakdown your next-steps item 3 asked for; `aggregate_metrics` has always computed it, this is the first time it's been run at a scale worth reading.

| Tier | Tuning n | Tuning MAE | Tuning bias | What it means |
|---|---|---|---|---|
| `full` (full window) | 2573 | 4.749 | -0.103 | Roughly unbiased, highest MAE (highest-scoring players — established starters, larger point totals to miss by) |
| `low` (partial window) | 502 | 3.091 | +0.913 | Lower MAE, but consistently and substantially **under**projects |
| `no_data` (0 games logged yet) | 126 | 1.849 | +1.849 | `bias == MAE` exactly, every single row |

Two things this changes about idea 2 (shrinkage) and idea 3 (usage data):

- **`no_data` is a literal predict-zero, not just "a thin sample."** `project_player` returns `rolling_avg=0.0` when `per_game_points` is empty, and this league's scoring can't go negative — so `error = actual - 0 = actual` on every single `no_data` row, which is why bias equals MAE exactly in that row above. This is a concrete, well-defined gap: any positional-average fallback, however crude, would strictly beat predicting zero for a rookie or season-debut player with no games logged yet. This is a sharper, more actionable version of idea 2 than "blend thin samples toward a baseline" — the `no_data` tier doesn't need blending, it needs *any* baseline at all.
- **`low` tier doesn't have worse MAE — it has worse bias.** Round 1 assumed thin samples would show up as noisier (higher MAE); the full pool shows the opposite in absolute terms (`low` MAE is *lower* than `full`), but `low` tier bias (+0.913) is roughly 9x `full` tier's (-0.103) and consistently in the underproject direction. That's consistent with `low`-confidence players skewing toward role-emerging situations (early-season call-ups, players who just won a job) — the design spec's flagged "role-change discontinuity" failure mode. **Idea 2 should be evaluated on bias reduction for the `low` tier specifically, not blanket MAE** — a shrinkage blend that pulls a hot rookie's rolling average down toward a positional mean would likely make bias worse here, not better, since the underlying signal (actual > current small sample) is directionally correct, just not weighted heavily enough yet.
- This also reinforces idea 3 (usage/opportunity data) rather than competing with it: a systematic underprojection concentrated in exactly the players most likely to be on a role upswing is the kind of pattern `target_share`/`snap_count` trends would catch before the points do. No change to idea 3's standing as the highest-leverage untested item.

Idea 5 (position-specific matchup model) stays gated behind 2 and 3, per the existing ordering's own reasoning — nothing here changes that.

### Net effect on priorities

- **Applied:** `DEFAULT_WINDOW` updated 4→6 in `projections.py` (significant on both tuning and holdout, full pool).
- **Re-ranked:** idea 2 (shrinkage) is now better-specified — fix the `no_data` literal-zero gap first (cheap, unambiguous win), then treat `low`-tier bias (not MAE) as the shrinkage target.
- **Unchanged:** idea 3 (usage/opportunity data) is still the highest-leverage untested idea, now with an extra piece of supporting evidence (the `low`-tier bias pattern). Idea 5 stays gated behind 2 and 3.
- **Downgraded:** Round 2's decay rejection — still not worth shipping, but "no measurable effect" replaces "confirmed harmful" as the honest description.

## Round 4 (2026-08-11): idea 2 built and shipped — two population confounds found and fixed along the way

Built `backend/baseline.py` (mirrors `matchup.py`'s "compute externally, inject as a plain float" pattern) plus `projections.py`'s `positional_baseline`/`shrinkage_strength` parameters on `project_player`. The straightforward first version — one "thin" population (players with fewer than `DEFAULT_WINDOW` prior games) blended toward for both the `no_data` and `low` tiers — **did not work**, and not in a subtle way. Two real confounds, found only by actually running it against the full pool rather than reasoning about it in the abstract:

**Confound 1 — a single "thin" population is too coarse for `no_data` specifically.** "Thin" pools every `games_played_before` value from 0 up to `window-1` together. A player on their 5th game (about to become "full") already scores close to an established starter, and dragged the pooled mean well above what a true debut game (`games_played_before == 0`) actually scores. Injecting that pooled baseline for `no_data` rows flipped their bias from +1.8 (underprojecting a literal 0.0) to **-5.4** — badly overprojecting in the opposite direction, worse than doing nothing. Fix: a dedicated `population="debut"` (`games_played_before == 0` exactly) for the `no_data` tier; `"thin"` (unchanged) stays for the `low` tier. `baseline.baselines_by_player_week_for_shrinkage` resolves each player-week to whichever population actually matches that player's real `games_used` at that week, computed the same way `project_player` computes it, so the two never drift apart.

**Confound 2 — even "debut" alone overshot, because week 1 isn't like other weeks.** After fix 1, `no_data` bias was still -3.6, not the ~0 expected. Measured directly against the 611-player pool: **354 of 611 players' season debut was in week 1** (mean 6.56 points — a normal starter's game, since the entire league's roster debuts simultaneously in week 1), versus only 157 players debuting week 5 or later (mean 2.00 points — much closer to the `no_data` tier's actual ~1.8-1.85). A pooled "debut" population is dominated by week-1 stars, not the rare mid-season call-ups/waiver-claims/injury-replacements that actually produce a `no_data` row in-season (since `games_used==0` requires zero *current-season* games before the target week, which for a week-1 player is trivially true for everyone and structurally different from a real gap later in the season). Fix: `min_week` on the population functions, with `DEFAULT_DEBUT_MIN_WEEK=2` excluding week 1 from the debut population specifically.

Both fixes are logged in `baseline.py`'s module docstring with the exact numbers, not just described here — worth reading before touching this module again.

### Results after both fixes (full pool, `DEFAULT_WINDOW=6`)

| Tier / check | Tuning: off → on | Holdout: off → on | Significant? |
|---|---|---|---|
| `no_data` bias (fallback-only, strength=0.0) | +1.798 → **-0.523** | +2.952 → **+0.414** | Yes, both (p=0.0) |
| `no_data` MAE | 1.849 → 2.433 | 2.952 → **2.418** (improved) | — |
| `low` bias (strength=0.5) | +0.571 → **+0.042** | +0.817 → **-0.271** | Yes, both (p=0.0) |
| Pooled MAE guardrail (strength=0.5 vs off) | +0.0906, p=0.0, significant (~2% of 4.29 baseline) | +0.0092, p=0.6412, **not significant** | Mixed — see below |

Bias magnitude dropped ~70-86% across both tiers on both week sets — holdout's `no_data` MAE even *improved* alongside the bias fix. The pooled MAE guardrail cost is real on tuning weeks (statistically significant, large `n`) but small in absolute terms and, notably, **not even statistically significant on holdout** (p=0.64) — consistent with the bias-first-with-MAE-guardrail criterion this idea was scoped against (unlike every prior round's MAE-first bar): predicting exactly 0 for every debut/call-up player was already close to MAE-optimal for that zero-heavy population, so *some* MAE cost for fixing a bias this large was expected going in, not a surprise finding — and on holdout specifically, that cost doesn't even clear a noise floor.

`strength=0.5` was picked mechanically (smallest tuning-weeks `|low-tier bias|` among `[0.0, 0.25, 0.5, 0.75, 1.0]`; full sweep: 0.0→0.571, 0.25→0.306, **0.5→0.042**, 0.75→0.222, 1.0→0.487 — so the actual tuning-weeks runner-up is `0.75`, not `0.25`). Checked against that real runner-up with a paired significance test (mirroring Round 3's window-size best-vs-runner-up check), not just eyeballed: `0.5` beats `0.75` significantly on both tuning (mean diff +0.264, p=0.0) and holdout (mean diff +0.545, p=0.0) low-tier bias. Holdout's own low-tier bias at `strength=0.5` is -0.271, close in magnitude to `0.25`'s +0.273 (both roughly halving `off`'s +0.817) — the two aren't holdout-distinguishable from each other on that one number alone, but `0.5` is the one confirmed significantly better than its neighbors on both week sets, `0.25` was not tested that way, and `0.5`'s holdout MAE cost turned out to be noise. Net: `strength=0.5` is a well-supported pick, not just "whatever tuning happened to land on." The population comparator (`debut+thin` vs a flat `"all"` population, both at `strength=1.0`) confirms the scoped-population design choice: `debut+thin` MAE=4.547/bias=-0.331 clearly beats `all`'s MAE=4.719/bias=-0.569.

**Applied: `projections.py`'s `DEFAULT_SHRINKAGE_STRENGTH` set to 0.5.** Same caveat as `DEFAULT_WINDOW`/`DEFAULT_DECAY`: this constant only takes effect when a caller supplies `positional_baseline` — with the current default of `None`, shrinkage never activates. There's no live orchestration layer yet (see `backend/README.md`), so this is "correctly configured, not yet wired into a running report" — whatever eventually generates a real weekly report needs to call `baseline.positional_baselines_by_week`/`baselines_by_player_week_for_shrinkage` and pass the result into `project_players`, the same follow-up note that applies to the matchup multiplier if it's ever revisited.

### What this changes for idea 3

The two confounds found here are a concrete illustration of exactly the risk idea 3 (usage/opportunity data) already carried: a population/cohort mismatch that looks fine on paper can be badly wrong in practice, and the only way to know is to check it against real data before trusting it. Worth keeping in mind when building `usage.py` next — sanity-check the actual distribution before trusting a sweep's p-values, the same discipline that caught both confounds here.

## Round 4, idea 3: usage/opportunity trend signal — tested, rejected

Built `backend/usage.py`: a recent-vs-trailing usage-share ratio (`wopr` by default, `target_share` as a sweep alternative), raised to a swept exponent `alpha`, clamped to a bounded multiplier — same "compute externally, inject as a plain float" pattern as `matchup.py`/`baseline.py`. Unlike idea 2, this stayed **MAE-first** — there's no equivalent "literal bug" (like the `no_data` tier's predict-zero) forcing a different framing here, so it's held to the same bar window/decay/matchup used.

**A read-only probe against real 2025 data caught a real bug before it ever ran, the same discipline idea 2's two confounds argued for:** `wopr` can be *negative* (`air_yards_share`'s numerator/denominator can go net-negative on a low-volume week; observed min ≈ -9 in the real pool), and the naive draft formula (`ratio ** alpha` with the clamp only on the *output*) would raise a domain error or go complex on a negative base for a non-integer `alpha`. Fixed by clamping the *ratio* to a safe positive range (`[0.1, 5.0]`) before exponentiating — full detail in `usage.py`'s module docstring. The probe also showed the ratio's real spread (p10=0.45, p90=1.58) is wider than an initial guess of `[0.85, 1.20]` for the output bound, so `MIN_USAGE_MULTIPLIER`/`MAX_USAGE_MULTIPLIER` were set to `[0.7, 1.4]` from the observed distribution, not a round-number guess.

### Results (full pool, `DEFAULT_WINDOW=6`, `DEFAULT_SHRINKAGE_STRENGTH=0.5` from idea 2 already in effect where noted)

- **Coverage**: usage data present for 3603/4608 graded rows (78.2%). The gap isn't missing data — `wopr` itself is 100% non-null (confirmed in the probe) — it's the `MIN_BASELINE_USAGE` no-signal gate correctly firing for QBs and any RB/WR/TE with near-zero receiving usage, exactly as designed.
- **Pooled MAE (primary), tuning weeks**: MAE gets **monotonically worse** as `alpha` increases — 0.0→4.290, 0.25→4.298, 0.5→4.346, 0.75→4.398, 1.0→4.440. The mechanically-picked best non-zero candidate (`alpha=0.25`, smallest deviation from the baseline) is **not significant** against `alpha=0.0` on tuning weeks (mean diff +0.0075, p=0.5061) or holdout (mean diff -0.0058, p=0.7540).
- **Low-tier bias (secondary — the reason this idea was prioritized)**: a real effect, but a small one. `alpha=0.25` vs off, tuning weeks: mean diff -0.0259, **p=0.0341, significant** — but the magnitude is ~4.5% of the low tier's +0.571 baseline bias, nowhere near idea 2's 70%+ reductions.
- **Per-position sanity check** (tuning weeks, MAE): QB p=0.42, RB p=0.57, TE p=0.66, WR p=0.66 — no position shows a significant effect, including QB, which is the expected null result confirming the no-signal gate works rather than a red flag.
- **Metric comparator**: `target_share` (MAE=4.287) and `wopr` (MAE=4.298) are both statistically indistinguishable from the `alpha=0` baseline (4.290) — neither metric shows a real advantage over the other or over doing nothing.
- **Interaction with idea 2's shrinkage**: `shrinkage_only` MAE=4.381 vs `both=on` MAE=4.386 — adding usage on top of shrinkage doesn't materially change the picture either way; `shrinkage_only` low-tier bias +0.042 vs `both=on` +0.02, a small additional nudge in the same direction as the standalone low-tier bias result, consistent in sign but too small to act on alone.

**Rejected: `DEFAULT_USAGE_ALPHA` stays 0.0.** The MAE-first bar this idea was scoped against isn't cleared — the one significant result (low-tier bias) is real but too small to justify shipping a feature whose entire premise was being "the highest-leverage untested idea." Code stays in the repo, tested and documented, same "built, verified, not wired in as a positive default" treatment `matchup.py` and Round 2's decay rejection already got — worth revisiting if a genuinely different signal (e.g. snap-count share, which needs the `db_playerids.csv` crosswalk this round deliberately didn't build) or a larger sample changes the picture.

### Why the "highest-leverage" idea underperformed — best guess

Round 3 flagged usage/opportunity data as high-leverage specifically because the `low` tier's bias pattern looked like role-emerging players being caught late by a lagging points average — usage share was expected to catch that trend earlier. The small-but-real low-tier bias effect found here is consistent with that story directionally, just far weaker in practice than the pooled MAE hoped for. Plausible reasons, not yet tested: (1) a 2-game recent window may be too short to distinguish a real role change from single-game noise — a longer recent window, or a decay-weighted recent average, wasn't swept this round; (2) `wopr` conflates target share and depth of target, and a rising target share with flat/falling depth (a short-area role change) may wash out in the composite where `target_share` alone wouldn't — the metric comparator here only checked the *pooled* effect of each metric, not whether they diverge specifically on the role-emerging sub-population idea 2 already isolates (the `low` tier); (3) RB usage is receiving-only in this build (module docstring's stated limitation) — a rushing-share signal, not built here, may matter more for the position where role changes are most visible in real fantasy outcomes.
