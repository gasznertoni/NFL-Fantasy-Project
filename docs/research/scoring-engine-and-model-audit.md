# Scoring-engine and projection-model audit — 2026-09-01

Statistical audit of `backend/scoring.py` and `backend/projections.py` against
real `nflreadpy` data (2016–2025, 56 554 player-games). Methods follow the
`math/probability-statistics` skill from [cc-polymath](https://github.com/rand/cc-polymath):
variance-component estimation, calibration regression with Wald tests on the
slope, Breusch–Pagan heteroscedasticity testing, paired significance tests, and
a block bootstrap over weeks for the ranking metrics.

Everything below is reproducible against live `nflreadpy`; all evaluation uses
strict walk-forward as-of discipline (a week-*w* projection only ever sees games
before week *w*).

---

## 1. Scoring engine: three column-mapping defects (all fixed)

`compute_league_points` itself is correct — the linear/milestone/tier machinery
reconciles exactly. The defects were all in `NFLREADPY_OFFENSE_COLUMN_MAP`, and
all three were silent: a key that matches no column contributes 0 rather than
raising.

**Method.** Reconcile our computed points against `nflreadpy`'s own
`fantasy_points_ppr` after subtracting every *intended* difference between the
two formulas (6-pt vs 4-pt passing TDs, our extra −1 per fumble, our 0 for
2-pt conversions, our yardage milestones). Any residual left over is a defect.

| | residual mean | residual sd | rows with abs(residual) > 0.011 |
|---|---|---|---|
| Before | +0.121 | 0.731 | 900 / 17 702 |
| After | −0.009 | 0.137 | 83 / 17 702 |

The remaining 83 rows are cases where `nflreadpy`'s own PPR total uses the
partial fumble columns and ours now uses the exact totals — our number is the
more correct one.

### 1a. Interceptions were never scored — the significant one

The map keyed on `"interceptions"`. The real column is
`"passing_interceptions"`. `pass_int`'s −2 therefore applied to nothing, in
both projections and computed actuals, for the entire life of the engine.

- 1 192 unscored interceptions across 2023–25
- **Every starting QB (≥15 attempts) scored +1.44 pts/game too high** — 7.9% of
  their mean, sd 1.70, max 10.0 in a single game
- Residual correlation with `passing_interceptions`: r = 0.86

Why the existing backtests could not catch it: the same bug inflated the
"actual" it was measured against, so MAE looked fine. It only shows up against
an external reference. Note this also means the QB calibration work recorded in
`docs/research/projection-model-backtest-findings.md` was fitted against a
contaminated target and should be re-read with that in mind.

### 1b. Fumbles were summed from a subset of columns

`fumble`/`fumble_lost` were built by summing `sack_`/`rushing_`/`receiving_`
variants. Those are a proper subset of the player's fumbles: over 2023–25 they
miss **15.6% of `fumbles_total`** and **10.6% of `fumbles_lost_total`** (fumbles
on returns, laterals, aborted snaps). `nflreadpy` carries both exact totals.
Impact is small (~0.02–0.04 pts/game) but it was free to fix.

### 1c. Return TDs by offensive players scored zero

`special_teams_tds` had no mapping. 43 return TDs by RB/WR/TE across 2023–25 —
rare, but a full 6-point miss each time. Added as a `return_td` category in both
league configs.

**Applied:** `backend/scoring.py` column map corrected; `return_td: 6` added to
both `leagues/*/scoring-config.json`; `tests/test_scoring.py` updated with
regression guards for all three (the two old tests asserted the wrong
behaviour). 290 tests pass.

---

## 2. What the projection model is actually worth

Walk-forward 2023–25, corrected scoring, 17 702 player-weeks.

| | n | mean actual | MAE | RMSE | bias | Pearson r | Spearman ρ |
|---|---|---|---|---|---|---|---|
| QB | 1 991 | 15.24 | 8.12 | 10.16 | −0.05 | 0.432 | 0.437 |
| RB | 4 585 | 7.61 | 4.68 | 6.45 | −0.02 | 0.599 | 0.640 |
| WR | 7 428 | 7.05 | 4.66 | 6.43 | +0.05 | 0.555 | 0.593 |
| TE | 3 698 | 5.46 | 3.64 | 5.09 | −0.19 | 0.527 | 0.524 |

**Skill against naive baselines** (skill = 1 − MAE_model / MAE_reference):

| position | vs "last game" | vs "season-to-date mean" | vs "positional mean" |
|---|---|---|---|
| QB | +0.163 | **−0.000** | +0.119 |
| RB | +0.147 | **−0.018** | +0.276 |
| TE | +0.195 | **−0.004** | +0.207 |
| WR | +0.169 | **−0.009** | +0.236 |

The tuned model (window 6, decay 1.0, shrinkage 0.5) is **statistically
indistinguishable from — and on RB slightly worse than — an unweighted average
of every prior game this season.** On the ranking metric it is the same story:
pairwise start/sit accuracy is 0.745 for the model vs 0.756 for the plain
season-to-date mean on RB (−1.1pp), 0.700 vs 0.711 on TE, 0.727 vs 0.735 on WR,
and only QB favours the model (+0.75pp).

### Why: the window is the wrong knob

Sweeping window × decay over 2023–25 and scoring on both objectives at once:

| window | decay | MAE | RMSE | pairwise acc | ρ |
|---|---|---|---|---|---|
| 12 | 0.9 | **4.715** | **6.762** | **0.7348** | **0.663** |
| none (expanding) | 0.9 | 4.714 | 6.761 | 0.7348 | 0.663 |
| 8 | 0.9 | 4.726 | 6.785 | 0.7344 | 0.662 |
| **6 (shipped)** | **1.0** | **4.755** | **6.833** | **0.7325** | **0.659** |
| 4 | 1.0 | 4.834 | 6.934 | 0.7294 | 0.654 |
| 3 | 1.0 | 4.934 | 7.071 | 0.7247 | 0.645 |

A longer window with mild recency decay dominates the shipped setting on all
four metrics simultaneously. Round 3 of the earlier backtest found window 6 beat
window 4 and stopped there; it never tested past 6, where the improvement
continues. The gain is real but small (see §5 on why).

---

## 3. The model is systematically over-dispersed

Calibration regression `actual = a + b · projected`, with a Wald test on
`b = 1`:

| position | a | b | R² | p(b = 1) | verdict |
|---|---|---|---|---|---|
| QB | 4.07 | 0.735 | 0.187 | 2.5e−14 | over-dispersed |
| RB | 0.95 | 0.878 | 0.359 | 2.2e−12 | over-dispersed |
| TE | 1.03 | 0.841 | 0.277 | 1.2e−12 | over-dispersed |
| WR | 1.00 | 0.852 | 0.308 | 2.3e−23 | over-dispersed |

Every slope is below 1 with overwhelming significance: **projections are spread
wider than reality.** The error is concentrated exactly where decisions get
made — the top decile of projections:

- QB: top decile projects 26.5, delivers 22.1 (**+4.4 too high**)
- WR: 17.8 → 15.5 (+2.3); RB: 19.0 → 17.0 (+2.0); TE: 13.6 → 12.1 (+1.5)
- and the bottom decile is under-projected by 0.6–0.8 across the board

### The cause is a known statistical one, and the theory predicts the number

One-way random-effects variance decomposition of weekly points:

| position | between-player σ² | within-player (week noise) σ² | ICC | implied k = σ²within/σ²between | weight at n = 6 |
|---|---|---|---|---|---|
| QB | 43.1 | 80.6 | 0.25 | 1.87 | 0.76 |
| RB | 30.3 | 33.6 | 0.47 | 1.11 | 0.84 |
| TE | 13.4 | 22.0 | 0.37 | 1.64 | 0.79 |
| WR | 27.4 | 35.6 | 0.43 | 1.30 | 0.82 |

The empirical-Bayes shrinkage weight `n/(n+k)` at n = 6 is **0.76 / 0.84 / 0.79 /
0.82** — against fitted calibration slopes of **0.735 / 0.878 / 0.841 / 0.852**.
The theory reproduces the observed miscalibration almost exactly. A 6-game
sample mean is simply not worth its face value, and `_shrinkage_weight` switches
shrinkage *off* precisely at `games_used >= window`, which is where this bias
lives. Confirmed directly: the `full` confidence tier has fitted slope
0.55–0.82.

The ICC column is also the honest ceiling for a player-identity model: even
knowing a player's true season mean explains only 25% (QB) to 47% (RB) of
week-to-week variance. Observed R² is 0.19 / 0.36 / 0.28 / 0.31 — the model
already captures 70–80% of what player identity alone can give. Further gains
must come from week-specific information.

---

## 4. Error grows with the projection, and no interval is reported

Breusch–Pagan style regression of squared residual on the projection: all four
positions significant, `p` from 4e−06 (QB) to 2e−110 (WR).

| position | sd(error) model | 80% normal interval coverage |
|---|---|---|
| QB | 7.87 + 0.119 · proj (R² 0.87) | 0.789 |
| RB | 3.62 + 0.320 · proj (R² 0.94) | 0.867 |
| WR | 3.47 + 0.360 · proj (R² 0.95) | 0.860 |
| TE | 2.86 + 0.376 · proj (R² 0.90) | 0.860 |

A 16-point WR projection carries sd ≈ 9. Empirical error quantiles for WR run
p5 = −8.7 to p95 = +12.3. Normal intervals over-cover because the outcome
distribution is strongly right-skewed (skew 1.39–1.64 for RB/WR/TE, excess
kurtosis 2.1–3.4, Jarque–Bera p ≈ 0) — use empirical quantiles, not a Gaussian.

**Objective mismatch.** Actual points are right-skewed with 14–22% of games at
or below zero. Mean bias is ~0 but *median* bias is +0.75 to +1.13 — the model
predicts a mean, as it should for a lineup that maximises expected total, but
every tuning round has optimised **MAE**, which is minimised by the *median*.
The two disagree by about a point per player per week, and the tuning has been
pulling toward the wrong one. RMSE is the metric that matches the product goal.

---

## 5. Putting the findings in one currency: simulated lineup points

8 500 simulated 14-team lineup decisions over 2025 (1 QB / 2 RB / 3 WR / 1 TE /
1 FLEX, drawn from random rosters), scored on real outcomes.

| strategy | pts/week | regret vs hindsight-perfect |
|---|---|---|
| hindsight-perfect lineup | 103.24 | 0.00 |
| rolling window 8, decay 0.9 | 88.94 | 14.29 |
| shipped model | 88.80 | 14.44 |
| empirical-Bayes shrinkage | 88.81 | 14.42 |
| affine recalibration of shipped | 88.80 | 14.44 |
| random lineup | 65.91 | 37.33 |

**The model captures 22.9 of the 37.3 points available over random (61%).** But
every estimator variant is within 0.15 pts/week of every other. Four rounds of
window/decay/shrinkage tuning have been optimising a parameter worth about
0.15 points a week (p = 0.009 — real, but tiny).

What is *not* flat, in the same currency:

| lever | pts/week | note |
|---|---|---|
| **modelling availability** | **+0.69** (p = 1.8e−19) | crude play-rate prior only |
| window 6 → 8 with decay 0.9 | +0.15 (p = 0.009) | free |
| interception fix | +0.02 (p = 0.42) on ranking | but +1.44 pts on every displayed QB number |
| affine recalibration | −0.00 on lineup | rank-preserving; fixes the displayed magnitude |

---

## 6. The largest gap: availability is not modelled at all

The projection is a **conditional-on-playing** average, presented and compared
as if it were an expected value. Inside a player's own active span (first to
last game of the season, byes excluded):

| position | miss rate | conditional E[pts] | unconditional E[pts] | overstatement |
|---|---|---|---|---|
| QB | 26.2% | 15.24 | 11.24 | 26.2% |
| TE | 23.9% | 5.46 | 4.15 | 23.9% |
| RB | 18.5% | 7.61 | 6.21 | 18.5% |
| WR | 18.4% | 7.05 | 5.75 | 18.4% |

Two consequences. First, the backtest never scores those weeks, so the tool's
own reported accuracy is optimistic. Second, and worse for decisions: comparing
two players with different injury risk on a conditional number is comparing the
wrong quantity. Scoring missed games as the 0 they really are drops the
simulated lineup from 89.3 to 77.4 pts/week — and multiplying by even a naive
historical play rate recovers 0.69 of that, five times the entire estimator
tuning.

---

## 7. Where the remaining headroom is

Walk-forward ridge on held-out 2025, adding feature blocks to the rolling
average:

| position | rolling pts only | + volume | + volume + DvP | + volume + Vegas |
|---|---|---|---|---|
| QB (RMSE) | 10.382 | 10.364 | 10.311 | **10.249** |
| RB | 6.676 | 6.630 | 6.620 | **6.585** |
| WR | 6.029 | **5.965** | 5.962 | 5.968 |
| TE | 5.430 | 5.351 | 5.348 | **5.337** |

**Volume is a far more stable signal than points.** ICC of the input itself:

| position | fantasy points | primary volume stat | target share |
|---|---|---|---|
| WR | 0.43 | targets 0.56 | **0.61** |
| RB | 0.46 | carries **0.60** | 0.44 |
| TE | 0.37 | targets 0.51 | **0.54** |
| QB | 0.25 | attempts 0.32 | — |

Touchdowns are the least stable component of all (receiving TD ICC 0.07–0.11) —
they are most of the week-to-week noise, and averaging *points* inherits that
noise directly. Projecting volume, then applying a shrunk efficiency rate, keeps
the stable part and regresses the unstable part.

**Vegas lines are free in `nflreadpy`** (`load_schedules()` carries
`spread_line` / `total_line`); implied team total = (total ± spread) / 2. Biggest
single gain for QB: R²oos +2.3pp, pairwise +1.05pp.

**Defence-vs-position adds almost nothing** once volume is in the model — which
corroborates the project's earlier decision to leave `matchup.py` unwired, and
now with a reason: the opponent's quality is already priced into the betting
line, more currently than a season-to-date DvP rate can be.

---

## 8. Recommendations, in priority order

> **Status (2026-09-01): all seven are implemented.** Measured end-to-end on
> held-out 2024-25, scoring missed games as the zeros they are, the combined
> stack is worth **+3.70 lineup points per week** over the previous estimator.
> Section 9 has the head-to-head. What follows is the reasoning as it stood
> when the recommendations were made.

1. **Model availability.** Report `P(play) × conditional points` as the
   expected-value number, or show both explicitly. Biggest measured lever by 5×.
   `nflreadpy.load_injuries()` gives practice status and report designation
   weekly; `news.py` already surfaces the qualitative version.
2. **Change the tuning metric.** MAE optimises the median; the product
   maximises an expected total. Tune on RMSE, and report pairwise start/sit
   accuracy and lineup regret alongside it — a variant that improves MAE while
   worsening ranking is not an improvement.
3. **Shrink at every sample size**, with `n/(n+k)` and k from the variance
   components above (QB 1.9, RB 1.1, TE 1.6, WR 1.3), instead of switching
   shrinkage off at `games_used >= window`. This fixes calibration (slopes move
   to 0.89–1.05) at a small MAE cost and a small RMSE gain. Note it slightly
   *worsens* within-position ranking, because it reorders by sample size — so if
   only one number is shown, apply the rank-preserving affine version
   (`a + b·projection`, fitted per position on prior seasons) and keep the
   ranking untouched.
4. **Widen the window to 8–12 with decay ≈ 0.9.** Improves MAE, RMSE, ρ and
   pairwise accuracy at once. One-line change.
5. **Add volume features and the Vegas implied team total.** ~1.5–3% RMSE and
   +0.5–1pp pairwise; the implied total is the single best week-level feature
   available for free.
6. **Publish an interval, not a point.** sd ≈ a + b · projection with the
   coefficients in §4, rendered from empirical quantiles rather than a normal.
   A 16-point WR projection with sd 9 is a materially different recommendation
   from a 16-point QB projection with sd 9.7 on a 15.2 mean.
7. **Accept the ceiling.** ICC caps a player-identity model at R² 0.25–0.47, and
   14.4 of the ~37 points of available lineup edge is irreducible weekly noise.
   The honest framing for the case study is that the tool captures 61% of the
   available edge, and that most of the remaining gap is not a modelling failure.

Items 1–2 are the ones that change outcomes; 3–6 are worth doing and cheap;
4 and 5 have no downside.

---

## 9. What was implemented, and what it measured

All seven recommendations landed on 2026-09-01. New modules: `availability.py`,
`calibration.py`, `calibration_fit.py`, `blend.py`, `context.py`, `ridge.py`
(shared linear algebra). Changed: `projections.py`, `backtest.py`,
`generate_report.py`, `PlayerCard.jsx`. 415 tests pass.

### 9.1 Conditional-on-playing accuracy

Walk-forward, held out on 2024-25 (11 901 player-weeks), each layer added to
the one above it:

| estimator | RMSE | MAE | bias | pairwise acc | mean calibration slope | mean abs(slope - 1) |
|---|---|---|---|---|---|---|
| A shipped (window 6, decay 1.0, window-shrink) | 6.759 | 4.853 | −0.036 | 0.7254 | 0.826 | 0.174 |
| B + window 8 / decay 0.9 + empirical-Bayes | 6.717 | 4.943 | +0.162 | 0.7222 | 1.013 | 0.035 |
| C + affine recalibration | 6.712 | 4.911 | −0.007 | 0.7222 | 1.016 | 0.023 |
| **D + volume & Vegas blend** | **6.521** | **4.761** | −0.020 | 0.7244 | 1.048 | 0.048 |

Paired against A: D improves squared error at p = 2.4e−25 and absolute error at
p = 5.9e−08. **The over-dispersion is gone** — the calibration slope moves from
0.826 to ~1.01–1.05, and the mean distance from a perfect slope falls by 4-7x.
Pairwise ranking is unchanged (−0.001, within noise), which is the point of
making the magnitude correction affine.

Note the shape of B on its own: empirical-Bayes shrinkage *fixes calibration
while slightly worsening MAE*, exactly as predicted in section 8 item 3 — it
reorders by sample size. The blend recovers both.

### 9.2 The product metric

18 000 simulated 14-team lineups over 2024-25, **scoring missed games as 0** —
the production reality the earlier simulation could not see:

| strategy | pts/week | vs shipped |
|---|---|---|
| hindsight-perfect lineup | 86.97 | +14.68 |
| **D + availability (full new stack)** | **75.99** | **+3.70** |
| A + availability | 74.67 | +2.37 |
| D new estimator, no availability | 74.26 | +1.97 |
| A shipped | 72.29 | — |

Every delta is significant (p from 5e−174 to numerically 0). **Availability
alone, on top of the new estimator, is worth +1.73 pts/week (p = 2.8e−247)** —
more than double the +0.69 the audit estimated from play-rate history alone,
because the injury report carries the fact that history cannot: a player ruled
Out has P(play) = 0.0006.

The full stack closes **25% of the gap** between the old estimator and a
hindsight-perfect lineup.

### 9.3 The availability model

Logistic regression over injury report status, practice participation, the
player's shrunk play rate, and position. Fitted 2018-23, held out 2024-25
(14 991 player-weeks):

| | log loss | Brier | AUC |
|---|---|---|---|
| league base rate | 0.509 | 0.164 | 0.493 |
| player play-rate history only | 0.985 | 0.146 | 0.723 |
| **fitted model** | **0.374** | **0.116** | **0.823** |

Calibration is tight — the largest gap between predicted and observed rate
across probability deciles is 0.068 — and the decision-relevant groups land
almost exactly: Out predicts 0.007 against 0.000 observed, Questionable 0.682
against 0.701, no designation 0.826 against 0.825.

DST is excluded (`ALWAYS_AVAILABLE_POSITIONS`): a team defence plays every week
its team has a game, so discounting it would price a risk that does not exist.

### 9.4 What the report now shows

`projection.points` is an **expected value**. Alongside it:
`conditionalPoints` (the if-he-plays number), `playProbability`, and
`floor`/`ceiling` from empirical 10th/90th residual percentiles. The frontend
renders the range under the point estimate and, only when a player is under 85%
likely to play, a line reading e.g. "82% likely to play · 4.8 if he does".

### 9.5 Open, and surfaced by this work

**DST return yardage dominates every DST score.** Decomposing 544 team-games of
2025 DST scoring under the current config:

| category | mean pts | share |
|---|---|---|
| `def_return_yd` | 11.46 | **68.8%** |
| def_sack | 2.35 | 14.1% |
| def_int | 1.40 | 8.4% |
| everything else combined | 1.45 | 8.7% |

At the configured 0.1 points per return yard, an average 114.6 return yards a
game is worth 11.46 points — more than every other DST category put together,
and it puts eight D/STs above every quarterback at the top of the report. Mean
DST score is 16.66/game; without return yardage it is 5.20, and a typical ESPN
D/ST scores roughly 7-9.

The *mapping* is defensible (`punt_return_yards + kickoff_return_yards` matches
a "return yards" label), so this is a league-settings question, not a code bug:
either the rate is not 0.1, or the category does not apply to kick/punt returns
in this league. **Not changed here** — the value is recorded as confirmed from
the real ESPN settings, and overriding that on inference would be worse than
flagging it. One line in `leagues/*/scoring-config.json` if it turns out to be
wrong.

**Still genuinely open:** DST and K have no fitted interval (the residual model
covers QB/RB/WR/TE only), the DST points-/yards-allowed tier bands remain the
acknowledged placeholder, and the availability model is applied slightly out of
domain for players who have never recorded a game (it was trained on
players inside an active span). All three are documented limits, not silent.
