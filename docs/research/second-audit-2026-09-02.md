# Second statistical audit — prediction model and scoring engine

**Date:** 2026-09-02 · **Auditor pass:** independent re-audit after v17
**Scope:** everything shipped since `docs/research/scoring-engine-and-model-audit.md` (v16/v17)
**Data:** live `nflreadpy` 2021–2025, 29 376 QB/RB/WR/TE player-weeks, strict walk-forward
**Status:** 486 tests passing; all three v16 scoring defects verified still fixed

---

## 0. Summary

The v17 work is sound where it is *reached*. The three problems worth acting on are
not modelling failures — they are a **wrong uncertainty formula**, a **delivery gap**,
and **unused data**.

| # | Finding | Severity | Measured |
|---|---|---|---|
| 1 | Prediction interval multiplies conditional quantiles by P(play) | **High** | Coverage 80% → 26%; 88% of published bands affected |
| 2 | Every in-season fixture predates the v17 wiring | **High** | v17 live in 1 of 18 weeks |
| 3 | TD component should come from opportunity, not history | **Medium** | **+0.48 lineup pts/week**, p=4.2e-03 |
| 4 | Six advanced nflreadpy feeds entirely unused | Medium | Snap share worth ~0.5–2.9% RMSE |
| 5 | Consensus tier gets no interval and no calibration | Medium | 45 players in wk 1, the top-10/position |
| 6 | Kickers discounted for a risk that does not exist | Low | True play rate 0.932; model caps at 0.831 |
| 7 | D/ST still ~2.5× inflated (audit-1 carry-over) | Medium | 4 D/ST in the wk-10 top 20 |

Two plausible ideas were **tested and rejected** — see section 5. That is the point of a
second audit: the cheap wins are gone, so the job is as much killing good-sounding ideas
as finding new ones.

---

## 1. Regression check: audit-1 fixes hold

Every column map was re-checked against a live `load_player_stats()` / `load_team_stats()`
response — the failure mode that hid three defects through 253 tests in v16.

- All 12 keys in `NFLREADPY_OFFENSE_COLUMN_MAP` present, zero NaN in 2024–25.
- All 6 keys in `DST_DIRECT_COLUMN_MAP` present.
- 486 tests pass.

**One latent risk.** `nflreadpy_row_to_stat_line` guards with `if not val`, which does not
catch `float("nan")` (NaN is truthy). A NaN in any mapped column propagates to a NaN total,
and `generate_report.py` writes with `allow_nan=False` — so one NaN cell fails the entire
fixture, not one player. No real row currently carries one; this is a one-line guard against
a future upstream change, not a live bug. Same trap as v16's `playerId: NaN`.

---

## 2. Finding 1 — the interval is wrong for 88% of the players it is shown for

`generate_report.py:524-530`:

```python
band = interval_model.interval(player["position"], points)   # conditional quantiles
if band is not None:
    lo, hi = band
    if probability is not None:
        lo, hi = lo * probability, hi * probability          # <-- wrong
```

`IntervalModel` is fitted on residuals of players **who played**, so it returns conditional
quantiles. That is correct. Scaling both endpoints by `p` is not.

The outcome is a **mixture**: zero with probability `1-p`, otherwise the conditional
distribution. Quantiles of a mixture are not the quantiles of a scaled conditional:

```
Q_Y(t) = 0                              if t <= 1-p
       = Q_cond( (t-(1-p)) / p )        otherwise
```

so for the shipped 10/90 band:

```
floor   = 0                      if p <= 0.90     else Q_cond(1 - 0.90/p)
ceiling = Q_cond(1 - 0.10/p)     if p >  0.10     else 0
```

**Measured**, on the WR bucket projecting ~9.9 points (n=1 906 played weeks, conditional
10/90 = [1.80, 20.10]), coverage of the *shipped* band under the true mixture:

| P(play) | shipped floor | shipped ceil | correct floor | correct ceil | true coverage |
|---|---|---|---|---|---|
| 1.00 | 1.80 | 20.10 | 1.80 | 20.10 | **80.5%** |
| 0.95 | 1.71 | 19.09 | 0.00 | 19.70 | 74.7% |
| 0.90 | 1.62 | 18.09 | 0.00 | 19.43 | 68.9% |
| 0.85 | 1.53 | 17.09 | 0.00 | 19.19 | 64.0% |
| 0.80 | 1.44 | 16.08 | 0.00 | 18.79 | 58.9% |
| 0.70 | 1.26 | 14.07 | 0.00 | 18.20 | 48.0% |
| 0.60 | 1.08 | 12.06 | 0.00 | 17.10 | 37.0% |
| 0.50 | 0.90 | 10.05 | 0.00 | 15.70 | **26.5%** |

Nominal coverage is 80%. It is correct only at `p = 1`.

Two distinct errors, in opposite directions:

- **The floor is far too high.** Once `1-p >= 0.10` the true 10th percentile is *exactly
  zero* — the player might not play. The shipped floor never reaches zero.
- **The ceiling is far too low.** The upside of a risky star barely moves in truth
  (19.70 → 15.70 as `p` goes 0.95 → 0.50) but collapses in the shipped code
  (19.09 → 10.05). A questionable stud's ceiling is understated by ~4 points.

**Scope, in the live week-1 fixture:** 821 players carry a published interval;
**725 of them (88%) have P(play) < 0.90**, where the floor should be zero. 677 are below
0.85 — which is exactly the threshold at which `PlayerCard.jsx` starts showing the
"82% likely to play" line. *The band is most wrong precisely where the UI draws attention
to it.*

**Fix.** `IntervalModel` currently fits offsets at two fixed quantiles. It needs to expose
`Q_cond(t)` for arbitrary `t`, then apply the mixture formula above. Concretely: fit the
bucket curve at a grid (0.05…0.95) instead of just (0.10, 0.90), and replace the
multiplication with the two-line mixture rule. `week1.py`'s interval path is not affected —
it attaches the band before availability is applied — but it should get the same treatment
so both tiers report the same quantity.

---

## 3. Finding 2 — the v17 stack ships in one week out of eighteen

| fixture | generated | P(play) | floor/ceiling | tiers |
|---|---|---|---|---|
| week 1 | 2026-09-02 00:28 | 936 / 936 | 821 | week1_model, in_house, consensus |
| week 2 | 2026-09-11 13:00 | 0 | 0 | consensus, in_house |
| week 6 | 2026-08-31 21:57 | 0 | 0 | in_house |
| week 10 | 2026-08-31 22:17 | 0 | 0 | in_house |
| week 18 | 2026-09-01 14:19 | 0 | 0 | in_house |

The commits that wire availability, calibration, intervals and the blend into reports
(`b13b39f` … `122ee79`) all landed at **2026-09-01 19:56–19:57**. The week-18 fixture was
written at **19:55** — two minutes earlier. Weeks 6 and 10 predate it by a day.

So every in-season week on the deployed frontend still shows the pre-v17 behaviour: no play
probability, no interval, no affine correction, no blend. The +3.70 pts/week headline is
real but is not what a visitor to weeks 2–18 is looking at. Week 2's `generatedAt` of
2026-09-11 is also in the future relative to its file mtime, which suggests it is a
hand-made fixture rather than a pipeline product.

This is a one-command fix, and it is the highest value-per-minute item in this audit.

---

## 4. Finding 3 — take the TD component from opportunity, not from history

This is the one change that measurably improves accuracy.

Fantasy points decompose exactly as `points = (non-TD points) + 6 x (touchdowns)`.
Touchdowns are the least stable component in the whole system (audit 1 measured receiving-TD
ICC at 0.07–0.11). Averaging total points inherits that noise directly.

Confirmation that TD noise is what caps ranking — same players, same estimator, TDs removed
from both sides (2023–25):

| position | pairwise, league scoring | pairwise, all TDs removed |
|---|---|---|
| RB | 0.7562 | **0.7649** |
| WR | 0.7363 | **0.7459** |
| TE | 0.7109 | **0.7293** |
| QB | 0.6436 | 0.6304 |

`load_ff_opportunity()` carries `rec_touchdown_exp` / `rush_touchdown_exp` /
`pass_touchdown_exp` — expected touchdowns from actual opportunity (2021–2025, ~6 000
rows/season). Replacing the *TD term only*:

**Held out 2024–25, complete cases, walk-forward affine per position:**

| pos | predictor | RMSE | MAE | Spearman | pairwise | p vs shipped |
|---|---|---|---|---|---|---|
| QB | shipped: rolling total | 10.114 | 8.132 | 0.373 | 0.6303 | — |
| QB | split, TDs from opportunity | **10.080** | 8.087 | **0.392** | **0.6363** | 0.55 |
| RB | shipped: rolling total | 6.529 | 4.815 | 0.669 | 0.7475 | — |
| RB | split, TDs from opportunity | **6.468** | 4.769 | 0.672 | 0.7481 | **1.2e-02** |
| WR | shipped: rolling total | 6.420 | 4.712 | 0.610 | 0.7231 | — |
| WR | split, TDs from opportunity | **6.378** | 4.690 | 0.614 | 0.7239 | **6.4e-03** |
| TE | shipped: rolling total | 5.336 | 3.860 | 0.566 | 0.7088 | — |
| TE | split, TDs from opportunity | **5.276** | 3.807 | **0.575** | 0.7114 | **3.6e-03** |

**Control that validates the harness:** splitting while keeping the player's *own* TD history
reproduces the shipped predictor to the digit at every position (6.529 / 6.420 / 5.336). The
decomposition is exact; **the entire gain comes from where the TD term is sourced**, not from
the split itself.

**In lineup points** — 1 920 simulated 14-team lineups (1QB/2RB/3WR/1TE/1FLEX), 2024–25,
missed games scored as zero:

| strategy | pts/week | regret |
|---|---|---|
| shipped (rolling total points) | 94.82 | 15.58 |
| + TD split with expected TDs | **95.30** | 15.10 |
| hindsight-perfect | 110.40 | 0.00 |

**+0.484 pts/week, t=2.86, p=4.2e-03** — 3.1% of remaining regret, and roughly **three times
the entire estimator-tuning band** audit 1 measured (0.15 pts/week across window, decay and
shrinkage combined).

---

## 5. Tested and rejected

Recording these matters as much as the positive results — both are ideas the linked
industry sources actively recommend.

### 5a. "Use expected fantasy points instead of actual" — redundant

A rolling average of `total_fantasy_points_exp` is **statistically indistinguishable** from a
rolling average of actual points as a standalone predictor (2023–25, n=13 875):

| pos | RMSE actual | RMSE expected | ΔRMSE | paired p |
|---|---|---|---|---|
| QB | 9.805 | 9.813 | +0.08% | 0.89 |
| RB | 6.435 | 6.446 | +0.18% | 0.74 |
| WR | 6.418 | 6.443 | +0.40% | 0.30 |
| TE | 5.268 | 5.242 | −0.48% | 0.28 |

And on top of the volume block **already shipping** in `blend.py`, it adds nothing at all
(RB 6.467 → 6.468, WR 6.312 → 6.314, TE 5.211 → 5.210). Expected points is *derived from*
volume, so the blend already has it.

This is why finding 3 is stated narrowly. Swapping expected points in wholesale throws away
the well-measured non-TD signal along with the noisy TD one. **The value is in the TD term
only** — a distinction the popular framing ("use expected points") obscures.

### 5b. "The 6-point passing TD is why QB projections are weak" — it is not

Re-scoring the same players under a 4-point passing TD barely moves predictability
(Spearman 0.406 → 0.418, pairwise 0.6436 → 0.6477). QB is the weakest position because its
ICC is 0.25 — starting QBs are genuinely similar to one another and weekly variance
dominates between-player variance. This is the ceiling audit 1 identified, not a defect, and
the honest response is to communicate it rather than tune against it.

---

## 6. Finding 4 — six advanced feeds, all unused

`load_ff_opportunity`, `load_snap_counts`, `load_nextgen_stats`, `load_pfr_advstats`,
`load_ff_rankings`, `load_participation` appear in **zero** modules.

Adding trailing snap share (`offense_pct`) on top of the full shipped feature set,
held out 2024–25, common complete cases:

| pos | shipped blend | + snap share | ΔRMSE |
|---|---|---|---|
| QB | 10.096 | 10.057 | −0.39% |
| RB | 6.467 | 6.454 | −0.20% |
| WR | 6.312 | 6.296 | −0.25% |
| TE | 5.211 | 5.182 | −0.56% |

Small, but consistent in sign at all four positions, and snap share alone (one feature)
recovers most of what the ten-column volume block delivers. Note the shipped volume block is
**not significant for QB** (p=0.60) — the blend currently does nothing for the position with
the worst absolute accuracy.

Separately: `rotowire_cache_2025.json` is 13 MB of per-game snap counts, red-zone targets and
carries for 597 players — and it feeds **only waiver-rationale text**. Red-zone opportunity is
the natural second input to the TD term in finding 3.

`load_ff_rankings` (FantasyPros preseason consensus) was flagged in the week-1 doc as "the
most promising thing to add" and still is not added.

---

## 7. Smaller defects

**7a. Consensus tier bypasses calibration and gets no interval.** In `generate_report.py`
the consensus branch applies the availability multiplier but never `apply_affine`, and never
attaches `floor`/`ceiling`. Verified in the live week-1 fixture: all 45 consensus rows have
`floor: null`. These are the top-10-per-position players — the ones actually started. The
report therefore shows an interval for the bench and none for the starters.

**7b. Kickers are discounted for a risk that does not exist.** Measured play rate inside an
active span, 2021–24: **K 0.932** — the highest of any position (QB 0.745, RB 0.802,
WR 0.808, TE 0.747). But `AvailabilityModel` is fitted on QB/RB/WR/TE rows only, so a kicker
gets the reference-level position dummy and no depth interaction. Observed range in the
week-1 fixture: 0.367–0.831, i.e. every kicker is discounted ~10pp below their true rate.
Either add K to the training rows or add K to `ALWAYS_AVAILABLE_POSITIONS`-style handling
with its own measured rate.

**7c. D/ST inflation is unresolved and still visible.** Audit 1 flagged `def_return_yd` at
0.1 pts/yard producing 68.8% of D/ST scoring, and deliberately left it. In the current week-10
fixture, **4 of the top 20 projections are D/STs at 20.6–22.6 points**, against a typical
ESPN D/ST of 7–9. The decision to defer was defensible when it was one line of an unconfirmed
config; it is now the single largest known bias in the report and it is still one line.

**7d. Documentation drift.** `backend/leagues.json` defines two leagues with per-league
scoring configs, but `CLAUDE.md` still says "single-user, single-league tool for v1. Don't
build multi-tenant scaffolding." One of the two should be updated to match reality.

---

## 8. Betting odds — where they actually help

The builder asked about consolidating sportsbook odds. The honest read:

**Game-level odds are already in and largely exhausted.** `context.py` derives implied team
totals from `load_schedules()`' spread and total lines, and `blend.py` consumes them. Audit 1
already showed that once the line is in the model, a defence-vs-position rate is worth
literally nothing (ΔRMSE −0.000) — the market prices the opponent better than a carried-over
rate does. Adding a second source of the *same* game-level number will not move accuracy.

**Player props are a different thing, and they solve a structural problem.** The consensus
tier is capped at 10 players per position — not by choice, but because both free feeds
(FantasyPros, Rotowire) cap there. That leaves the entire bench and waiver pool on the
in-house estimator. Sportsbook player props cover ~200+ players a week with a sharp,
week-specific, market-clearing estimate of exactly the quantities this engine already
scores: passing yards, rushing yards, receiving yards, receptions, anytime TD.

That maps directly onto the scoring engine's existing stat-line interface — a prop line is a
projected stat line, and `compute_league_points` already turns one of those into league
points. No new scoring mechanism required.

It also attacks the right target. Audit 1 established that a player-identity model is capped
at R² 0.25–0.47 by ICC, and that "anything more has to come from week-specific information."
Props are week-specific information, priced by people with money at risk.

**Feasibility (the-odds-api.com, verified 2026-09-02):**
- Free tier: 500 credits/month. Historical snapshots back to 2020 are available on the free plan.
- Market keys exist: `player_pass_yds`, `player_rush_yds`, `player_reception_yds`,
  `player_receptions`, `player_anytime_td`.
- **Constraint:** props are per-event only (`/events/{eventId}/odds`), not per-sport. So a
  weekly pull is ~16 events × 5 markets ≈ 80 credits, ≈ **344 credits/month** — inside the
  free tier, but with little headroom. Budget one pull per week, cached.

**Two cautions before building it.**
1. An anytime-TD price is a *probability*, not a TD count. Convert by removing the vig
   (normalise the two-way implied probabilities) before using it — a raw American-odds
   conversion is biased upward by the hold, typically 4–8%.
2. Validate it walk-forward like everything else. The historical endpoint makes that
   possible; without it, props would be the one input in the system that was never tested.
   Note that props are also a *direct competitor* to the TD-opportunity signal in finding 3 —
   test them against each other, not just against the shipped baseline.

---

## 9. Ranked next steps

Ordered by measured value per unit of work.

**1. Regenerate the in-season fixtures.** (minutes) The entire v17 stack is invisible on 17
of 18 weeks. Nothing else in this list changes what a visitor sees as much as this does.

**2. Fix the interval mixture.** (half a day) Replace `lo, hi = lo*p, hi*p` with the mixture
quantile rule in section 2; extend `IntervalModel` to serve arbitrary quantiles. Add a
regression test asserting the floor is exactly 0 whenever P(play) <= 0.90, and a coverage
test asserting empirical coverage lands near 0.80 across play-probability bands. Affects 88%
of published bands.

**3. Take the TD term from opportunity.** (1–2 days) `points = rolling(non-TD points) +
6 x rolling(expected TDs)`. Worth **+0.48 lineup pts/week (p=4.2e-03)**, significant at
RB/WR/TE individually. Needs a `load_ff_opportunity` adapter and a re-fit of the affine and
interval models against the new estimator. Ablate it in `backtest.py` like every other layer.

**4. Give the consensus tier calibration and an interval.** (half a day) It covers the
players most likely to be started and is currently the least-instrumented path in the system.

**5. Resolve the D/ST return-yard question.** (one conversation) Check the real ESPN D/ST
settings screen. If return yards are not a D/ST category — and they are not in ESPN's
default set — it is a one-line config change that removes the largest known bias in the
report. If they are, record the confirmation and the numbers stop being suspicious.

**6. Add snap share to the blend, and fix QB.** (1 day) Consistent small gain at all four
positions. While there: the shipped volume block does nothing for QB (p=0.60) — QB needs its
own feature treatment or should be excluded from the blend rather than carrying a fit that
does not help it.

**7. Player props from the odds API.** (3–5 days, highest ceiling) Biggest potential gain and
the only item that lifts the ICC ceiling, but also the only one with a live external
dependency, a vig correction to get right, and a real risk of being redundant with item 3.
Do it after items 1–4, and validate it walk-forward against the historical endpoint before
wiring it into a report.

**8. Kicker availability, and the NaN guard.** (an hour) Both small, both cheap.

---

## Reproducibility

Analysis scripts and cached panels for every figure above are in the audit scratchpad
(`build_panel.py`, `exp_vs_actual.py`, `incremental2.py`, `decompose.py`, `interval_math.py`,
`lineup_sim.py`, `qb_diag.py`, `kicker_rate.py`, `nan_check.py`). All results are strict
walk-forward: training data never includes the season being tested, and every model
comparison is on a common complete-case row set so RMSEs are directly comparable.


---

# Addendum — implementation and retest, 2026-09-02

Everything above was written before the fixes landed. This section records what
was built, what the retest found, and the one place the retest **overturned a
finding in this very document**.

## A. league-2's real scoring landed

The builder supplied screenshots of the second league's complete ESPN "Scoring
Settings" screen (Intuitech Fantasy, 8 teams). `leagues/league-2/scoring-config.json`
is now real rather than placeholder. It differs from league-1 structurally, which
made several latent single-league assumptions visible:

| | league-1 | league-2 |
|---|---|---|
| Passing TD | 6 | **4** |
| Interception | −2 | **−1** |
| 2-pt conversion | 0 (absent) | **2** |
| FG bands | 3 rolled | **6 native** (3/3/3/4/5/6) |
| Yardage milestones | 400 pass, 200 rush/rec | **none** |
| D/ST yards allowed | tiered | **none** |
| D/ST return yards | *(removed, see C)* | **none** |
| Mean QB score | 15.28 | 13.92 |
| Mean K score | 6.70 | 8.17 |
| Mean D/ST score | 5.20 | 6.57 |

Three code changes followed, each guarded against the silent-miss failure class:

- **`kicker.py`** now emits all six native FG buckets alongside the three rolled
  bands, so each config picks up only the family it defines.
  `validate_fg_band_family()` **raises** if a config mixes them, because that
  would double-count every made kick.
- **`dst.py`** splits one overloaded category into three that mean what they say:
  `def_fumble_rec_td` (fumble returned for a score — league-1's rule), `def_td`
  (any defensive TD — league-2), `def_st_td` (special-teams return TD — league-2).
  `validate_dst_td_categories()` raises on the overlapping pair. league-1's config
  key was renamed to match; same source column, same 6 points, identical scoring.
- **`scoring.py`** gained `fumble_recovery_td` (league-2's MISC line) and the NaN
  guard from section 1.

Three league-2 categories have no nflreadpy source and will always score 0
(`st_player_forced_fumble`, `st_player_fumble_rec`, and the two D/ST
special-teams fumble lines). They are kept in the config because the rule exists;
each is worth 1 point and is vanishingly rare for a rostered player.

## B. The interval mixture is fixed

`IntervalModel` now stores the whole conditional quantile curve on a 19-point
grid rather than two endpoints, and `interval()` takes `play_probability` and
returns the **mixture** band. `generate_report.py` no longer multiplies. The
week-1 tier had the same defect and is fixed via a shared `_mixture_band()`
helper — it was the tier actually shipping the 821 affected bands.

Seven regression tests guard it, including empirical coverage across
probabilities and an exact assertion that the floor is `0.0` whenever
P(play) ≤ 0.90. One subtlety worth recording: `1.0 - 0.90` is
`0.09999999999999998`, so a bare `lo_q <= 1 - p` comparison misses the boundary
case exactly at p=0.90. It carries a 1e-9 tolerance.

## C. D/ST return yards: resolved and removed

Section 7c flagged this and left it. league-2's settings screen — which lists
every D/ST category — has **no return-yardage line at all**, and league-2 scores
6.57 pts/game. With the builder's explicit confirmation, `def_return_yd` was
removed from league-1: **16.66 → 5.20 pts/game**, and the four D/STs that sat in
the week-10 top 20 drop out entirely. This was the largest known bias in the
report.

## D. The TD-from-opportunity finding did NOT replicate

**Section 4 of this document overstates its case, and this corrects it.**

The standalone measurement was real: substituting an opportunity-based touchdown
term for the player's own touchdown history beat a bare rolling average at every
position (RB p=1.2e-02, WR p=6.4e-03, TE p=3.6e-03), worth +0.48 lineup points a
week. That is reproducible.

Re-measured through the **real pipeline** — shrinkage, affine recalibration, and
a blend already carrying rolling volume and the Vegas implied total — it is
worth nothing:

| league | held-out | snap share only | + TD estimator | p |
|---|---|---|---|---|
| league-1 | 2025 | 6.4872 | 6.4763 | 0.170 |
| league-1 | 2024 | 6.5569 | 6.5596 *(worse)* | 0.687 |
| league-2 | 2025 | 6.1662 | 6.1579 | 0.135 |
| league-2 | 2024 | 6.2659 | 6.2698 *(worse)* | 0.387 |

Not significant in either direction, and **the sign flips between held-out
seasons** — the signature of noise, not signal. The volume block already carries
most of what opportunity-based touchdowns know, so the marginal value against the
full stack is nil even though it is real against a bare average.

Two lessons, both worth keeping:

1. **A substitution and an augmentation are not the same change.** The first
   integration attempt added the two halves as blend features alongside
   `rolling_avg`; that is worth nothing (p=0.60) because a ridge cannot exploit a
   decomposition of a feature it is already given whole. Only the substitution
   reproduces the standalone measurement — and even then, only against a bare
   average.
2. **Measure against the stack you ship, not the component you are replacing.**
   This is the same trap the v16 audit fell into from the other direction, and
   the reason audit 1's estimator-tuning band was 0.15 pts/week.

`expected_td.py` is built, tested (13 tests) and wired; the estimator flag
`projections.USE_OPPORTUNITY_TD_ESTIMATOR` is **False** by default, matching the
call already made for `matchup.py` and `usage.py`.

## E. What the retest actually validated

Snap share, and only snap share. Held out 2025, trained 2022–24, through the
shipped pipeline:

| league | stack | RMSE | MAE | pairwise | Spearman | p vs base |
|---|---|---|---|---|---|---|
| league-1 | pre-audit | 6.5089 | 4.6887 | 0.7405 | 0.6466 | — |
| league-1 | **shipped now** | **6.4763** | **4.6451** | **0.7429** | **0.6517** | **6.1e-04** |
| league-2 | pre-audit | 6.1877 | 4.5026 | 0.7437 | 0.6561 | — |
| league-2 | **shipped now** | **6.1579** | **4.4605** | **0.7458** | **0.6610** | **2.0e-04** |

Every metric improves in both leagues, and the direction is consistent at all
four positions (QB −0.6%, RB −0.5%, WR −0.3%, TE −0.8%). Modest, and honestly
modest — but significant, and it improves the *ranking* as well as the error,
which is the pair of things that has to move together.

## F. Kicker availability

`build_training_rows_nflreadpy` now includes K. The model had never seen a kicker
and extrapolated them from the reference position level; they came out at
0.367–0.831 against a measured play rate of 0.932. A depth-1 kicker now predicts
**0.972**.

## G. Test suite

**512 passing**, up from 486. New: 13 for `expected_td.py`, 7 interval-mixture
regression guards, 3 for the D/ST category split, plus the config-validation
raisers.
