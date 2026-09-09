# Third statistical audit — 2026-09-09

Scope: verify that audit 1 (v16/v17) and audit 2 (v18) fixes survived the v20
rewrite; audit v20's new surfaces; reconcile both leagues against an external
reference; and produce the three forward-looking sections (vendor comparison,
UI, post-week-1 plan).

Both leagues audited: **league-1** (AUT-HUN, 14 teams, ESPN, `te_premium`) and
**league-2** (Intuitech, 8 teams, Sleeper, full PPR).

Every numeric claim below names the script that produced it. All scripts live
in `docs/research/scripts/` and are runnable from the repo root as
`.venv/bin/python docs/research/scripts/<name>.py`.

---

## 0. Headline

The scoring engine is **clean**. It reconciles to the cent against an external
reference on all 6 037 of 2025's QB/RB/WR/TE player-games, for both leagues,
and to an independent re-implementation on every D/ST and kicker game too. Every
column-map key — including the eight v20 added and never checked by an auditor —
matches a real column in a live 2024-25 response. Audit 1's and audit 2's fixes
all hold.

What this audit found is not in the scoring. It is in three places:

1. **The fixtures the deployed site is serving right now are wrong for weeks
   2–18** — not merely "placeholder", as `CLAUDE.md` describes them, but
   systematically deflated, by up to 74%.
2. **league-1's return-yardage line is the most consequential unmodelled thing
   in the system.** It breaks the consensus tier (RB −1.49 pts/game, not the
   +0.18 v20 records), and it costs 0.10 Spearman of held-out ranking at both
   RB and WR. It is also, per its ICC, entirely predictable.
3. **Three guards that raise rather than warn were never called by the
   pipeline they guard**, and a fourth instance of this repo's NaN trap was
   sitting latent in `dst.py`.

Items 3 and the NaN trap are **fixed in this branch** with regression tests
(commit `aa55fb9`, separate from the audit write-up, as instructed). 621 tests
pass, up from 607.

**Task 2 could not be run as specified.** Every vendor host is blocked by this
sandbox's egress policy. See §9.

---

## 1. Findings

| # | Severity | Finding | Measured effect | Status |
|---|---|---|---|---|
| 1 | **High** | Weeks 2–18 of the shipped fixtures carry a collapsing `playProbability`, so every projection the deployed site shows for those weeks is deflated | mean P(play) 0.55 (wk 2) → **0.255** (wk 18); `points` is that fraction of `conditionalPoints` | Reported, not fixed (regeneration is out of this audit's scope) |
| 2 | **Medium** | Fourth instance of the NaN trap: `dst._points_allowed_for_team_game` returned `nan` and `build_dst_game_logs` guarded with `is None`. Compounded by `scoring._tier_points` having no NaN guard | up to **5 pts** per D/ST game silently absent; latent today | **Fixed** + 9 regression tests |
| 3 | **Medium** | The three config-shape validators added by v18/v20 were never called on the report-generation path; `validate_dst_td_categories` was never applied to a shipped config at all | Defence-in-depth gap; a future config edit double-counts silently | **Fixed** + 5 regression tests |
| 4 | **Medium** | Consensus-tier RB bias is **−1.49 pts/game**, not the +0.18 `CLAUDE.md` v20 records. Fully attributable to player return yardage, which no vendor publishes and nothing imputes | RB −1.49, WR −2.92, QB −0.47, TE −0.19 | Reported |
| 5 | **Medium** | league-1's kick/punt return-yardage line costs **0.107 Spearman at RB and 0.100 at WR** on held-out 2025, yet its ICC is 0.488 — as stable as receiving yards. The model has no feature for it | See §6 and §8 | Reported; the obvious fix was tested and **rejected** (§8) |
| 6 | **Low-Med** | QB prediction intervals under-cover: **73.0–75.8%** against a nominal 80%, in both leagues. RB/WR/TE are within tolerance | league-1 QB 0.741, league-2 QB 0.758 | Reported |
| 7 | **Low** | `dst.assemble_dst_stat_line` does not set `position`, contradicting both `scoring.py`'s docstring and league-1's config `_linear_notes`, which name it explicitly | Zero effect today (no D/ST category is position-scoped); latent | Reported with patch |
| 8 | **Low** | `test_store.py`'s fallback test read the ambient `FANTASY_STORE_DIR`, so it passed on a bare checkout and failed under this repo's own documented run instructions | 1 test | **Fixed** |
| 9 | **Low** | `leagues/league-2/roster.json` carries no `playerId` for any entry; league-1's does | Roster lookups for league-2 must fall back to name matching | Reported |
| 10 | Info | The published 10th–90th band is exactly the mixture's 10th/90th percentiles, but its *realised* coverage is 87–89% whenever P(play) ≤ 0.90, because the whole zero-atom sits inside it | Correct as labelled; worth knowing | Reported |

### Verified still holding (no defect)

| Check | Result | Script |
|---|---|---|
| Every `*_COLUMN_MAP` key against a live 2024-25 `load_player_stats()` / `load_team_stats()` | **48/48 present**, including all eight v20 keys | `audit3_column_check.py` |
| Engine vs external reference (`fantasy_points_ppr` + analytic delta), 2025 | **residual 0.0000, sd 0.0000, 0 rows over 0.01**, both leagues, all four positions | `audit3_reconcile.py` |
| Engine vs a from-scratch re-implementation off raw columns | **0 disagreement** on 6 037 player-games, 544 D/ST games, 543 kicker games, both leagues | `audit3_reconcile.py`, `audit3_dst_kicker.py` |
| Scored-category coverage | league-1: **zero** orphan categories. league-2: four, all documented and deliberate | `audit3_category_coverage.py` |
| v18 mixture-quantile interval fix | Holds. Monte-Carlo coverage 0.798–0.801 at p ≥ 0.95 | `audit3_walkforward.py` |
| `impute_unpublished_categories` leakage | **None.** Refit on 2022-24 only differs from the shipped 2022-25 fit by ≤0.08 pts/game everywhere | `audit3_imputation.py` |
| v20 position-scoping (`resolve_value`, implicit `position`) | Correct at every producer except `dst.py` (finding 7) | `audit3_nan_probes.py` |

---

## 2. Regression check: the column maps (finding: none)

`audit3_column_check.py` pulls a live `load_player_stats(seasons=[2024,2025])`
(38 405 rows, 150 columns) and `load_team_stats` (1 140 rows, 138 columns) and
checks every key in `scoring.NFLREADPY_OFFENSE_COLUMN_MAP`,
`dst.DST_DIRECT_COLUMN_MAP`, the three D/ST column tuples, and all four of
`kicker.py`'s band families.

**All 48 keys resolve.** The eight v20 added and that no auditor had checked:

| key | → | non-zero rows (2024-25) |
|---|---|---|
| `receiving_first_downs` | `rec_first_down` | 5 959 |
| `rushing_first_downs` | `rush_first_down` | 3 122 |
| `completions` | `pass_completion` | 1 321 |
| `sacks_suffered` | `pass_sacked` | 1 059 |
| `kickoff_return_yards` | `kick_return_yd` | 1 452 |
| `punt_return_yards` | `punt_return_yd` | 858 |
| `fumble_recovery_tds` | `fumble_recovery_td` | 40 |
| `attempts` (derived input for `pass_incompletion`) | — | present |

`fg_blocked_list` is `NaN` on 1 096 of 1 138 kicker rows, which
`_blocked_distances`' `isinstance(raw, str)` check handles correctly — a NaN is
a float, so it returns `[]`.

Note the *shape* of this check: it is the one CLAUDE.md's "a column-map key
that matches nothing is silent" note asks for, and it is cheap. It should be a
test, not an audit artifact — see §11.

---

## 3. External reconciliation (finding: none — the engine is exact)

CLAUDE.md's rule is that an internal backtest cannot catch a scoring bug,
because the bug contaminates both sides. `audit3_reconcile.py` does two
independent things on 2025's 6 037 regular-season QB/RB/WR/TE player-games:

**A. Independent re-implementation.** A scorer written from each league's JSON,
reading raw nflreadpy columns directly and never importing `scoring.py`'s
column map. Disagreement with `compute_league_points()` would be an engine or
map defect.

**B. External reconciliation.** nflverse's own standard-PPR total, rebuilt from
raw columns (verified: rebuilt − shipped `fantasy_points_ppr` = 0.0000 with sd
0.0000 on every row, so the reconstruction *is* nflverse's formula), then
compared against `league_points − analytic_delta`, where the delta is derived
rule by rule from the config.

Residual distribution, both checks, both leagues:

```
                       n      mean       sd       min       p1      p50      p99      max   |>0.01|
league-1 A          6037   +0.0000   0.0000    +0.000   +0.000   +0.000   +0.000   +0.000        0
league-1 B          6037   -0.0000   0.0000    -0.000   -0.000   +0.000   +0.000   +0.000        0
league-2 A          6037   +0.0000   0.0000    +0.000   +0.000   +0.000   +0.000   +0.000        0
league-2 B          6037   -0.0000   0.0000    -0.000   -0.000   +0.000   +0.000   +0.000        0
```

Broken out by position, every cell is zero. This is a materially stronger
result than v16's "within 0.14 sd on 99.5% of rows" — the engine now agrees
*exactly*, not approximately, because the analytic delta accounts for every
rule difference including the fumble-totals-vs-subset distinction and the
position-scoped reception.

Mean points per game under each engine, 2025 actuals:

| | QB | RB | WR | TE | K | D/ST |
|---|---|---|---|---|---|---|
| league-1 | 15.47 | 9.88 | 8.07 | 6.47 | 8.26 | 5.22 |
| league-2 | 13.92 | 7.58 | 6.65 | 5.60 | 8.17 | 6.57 |

league-2's QB 13.92 reproduces v18's recorded figure exactly. league-1's
numbers are all new (its config was rewritten in v20).

D/ST and kicker have no external reference — no vendor publishes a team-defence
points total on this scoring basis — so `audit3_dst_kicker.py` runs check A
only, against a from-scratch scorer including the nine-band ladders, the six
native FG buckets, the rolled bands, both miss families, and blocked-kick
folding by distance. **Zero disagreement on all 544 D/ST and 543 kicker
game-rows, both leagues.**

---

## 4. Finding 1 (High): weeks 2–18 of the shipped fixtures are deflated

`CLAUDE.md` describes weeks 2–18 of a season with no published stats as
"placeholders that get regenerated properly once they become the current week",
and characterises the degradation as falling back to "the flat positional mean".
That understates it. Measured directly off the shipped fixtures:

| week | mean P(play) | median | `points` ÷ `conditionalPoints` |
|---:|---:|---:|---:|
| 1 | 0.670 | 0.660 | 0.676 |
| 2 | 0.551 | 0.495 | 0.551 |
| 4 | 0.410 | 0.316 | 0.410 |
| 8 | 0.313 | 0.215 | 0.313 |
| 12 | 0.273 | 0.170 | 0.273 |
| 18 | **0.255** | **0.151** | **0.255** |

Because `projected_points = P(play) × conditional_points`, the headline number
on every card in the week-18 report is **about a quarter** of the player's
if-he-plays projection. The median player is shown at 15% of it.

**Mechanism.** `availability.py` builds its play-rate feature as

```python
grid["prior_play_rate"] = grouped.transform(lambda s: s.shift(1).expanding().mean())
```

— an expanding mean of `played` *within the current season*. In 2026 nflverse
has published no game stats, so `played` is 0 for every week; the expanding mean
is 0 and `prior_games_observed` grows week by week, so the shrunk rate is pulled
toward zero with increasing confidence. The model is behaving correctly on the
data it is given; the **pipeline** is giving it a play rate computed from a
season that has not happened.

**Why it matters more than "these are placeholders".** The deployed frontend
serves these files, and this is a portfolio artifact. A reader who clicks to
week 4 today sees a report in which no player projects above single digits.
It is also the exact boundary Task 4 asks about — see §11.

**Recommended fix** (not applied here — regenerating fixtures is outside this
audit's remit): when a season has no published player stats at or before the
target week, suppress the current-season play-rate feature rather than feeding
it zeros, and fall back to the prior-season rate. A one-line guard in the
adapter that builds the grid; a test that a season with zero published weeks
produces the same P(play) at week 12 as at week 1.

---

## 5. Findings 2 and 3 (Medium, both FIXED in `aa55fb9`)

### 5.1 The fourth NaN trap, in `dst.py`

`CLAUDE.md` records three shipped NaN defects and says plainly: "when you write
a 'is this value missing?' check against feed data, `value != value` is the
test." `dst.py` still had the v19 shape:

```python
points_allowed = _points_allowed_for_team_game(schedule_game, team)
if points_allowed is None:
    continue
```

`audit3_nan_probes.py` establishes it empirically. All **272** of 2026's REG
schedule rows carry `home_score = nan`. `_points_allowed_for_team_game` returns
that `nan`; `is None` does not catch it; `!= itself` does.

It compounds. `scoring._linear_points` has guarded NaN since v18
(`if raw and raw == raw`), but `_tier_points` had not:

```python
raw = stat_line.get(category)
if raw is None or not bands:
    continue
```

`nan <= band["max"]` is `False` for *every* band including the catch-all, so a
NaN points-allowed matched nothing and the category contributed **0** — not a
crash, not a NaN total, just a silently missing tier worth up to 5 points, the
whole difference between a shutout and a blowout. Probe output:

```
_points_allowed_for_team_game -> nan;  `is None` catches it: False;  `!= itself` catches it: True
  resulting stat line def_points_allowed=nan
  scored total=4.0  breakdown keys=['def_sack', 'def_yards_allowed']
```

**Live or latent?** Latent. `load_team_stats()` has no row for an unplayed
game, so `build_dst_game_logs`' `game_id` self-join drops those games before
the guard is reached. It is one data-shape change from live: a mid-scrape pull,
a game in progress, or nflverse publishing zero-rows for scheduled games.

Fixed with a named `_has_final_score()` helper mirroring `season_week.py`'s,
plus a NaN guard in `_tier_points`. Nine regression tests, all of which fail
against the pre-fix module. Both engines reconcile to identical values after
the change (§3's numbers were re-run post-fix).

### 5.2 Guards that never ran on the path they guard

v18 introduced `kicker.validate_fg_band_family` and
`dst.validate_dst_td_categories`, and v20 added
`kicker.validate_fg_miss_band_family`, all deliberately raising rather than
warning — audit 2's stated principle being that "a silent miss is worse than a
crash". A config defining two overlapping category families double-counts
every made kick, every missed kick, or every fumble-return touchdown.

A full-repo grep finds **no caller outside `kicker.py`, `dst.py` and
`tests/`**. `generate_report.py` — the only thing that ever loads a config and
scores with it — never called any of them. Worse, of the three:

- `validate_fg_band_family` and `validate_fg_miss_band_family` are at least
  applied to both shipped configs by `test_kicker.py`'s
  `test_both_shipped_configs_pass_both_family_validators`;
- **`validate_dst_td_categories` is only ever called on synthetic dicts.** No
  test has ever run it against a real config.

Fixed with `generate_report.validate_scoring_config()`, called once per league
before any point is computed, on both the multi-league and single-league paths.
Both shipped configs pass. Five regression tests, including one that runs all
three validators over the real shipped configs.

---

## 6. Finding 4 (Medium): the consensus tier's RB bias is larger than recorded

`CLAUDE.md` v20 records post-imputation bias on "the top-10-per-position
population the tier actually covers" as QB +0.19 / RB **+0.18** / WR −2.94 /
TE −0.02.

Re-measured on 2025, defining that population as the top 10 per position per
week by scored points (`audit3_imputation.py`), in league-1 points per game:

| population | pos | n | no imputation (pre-v20) | **shipped** | refit on 2022-24 only |
|---|---|---:|---:|---:|---:|
| top-10/pos/week | QB | 180 | −1.84 | **−0.47** | −0.39 |
| top-10/pos/week | RB | 181 | −5.92 | **−1.49** | −1.50 |
| top-10/pos/week | WR | 180 | −6.31 | **−2.92** | −2.92 |
| top-10/pos/week | TE | 181 | −2.69 | **−0.19** | −0.17 |

Two things follow.

**No leakage.** The shipped coefficients are documented as least squares on
2022-25 and the v20 bias figures were measured on 2025 — inside the fitting
window. Refitting the identical functional forms on 2022-24 only and
re-measuring on held-out 2025 moves nothing: the largest gap is 0.08 pts/game.
The in-sample fit bought nothing, so the v20 methodology, while technically
in-sample, was not misleading. **The imputation is sound.**

**But WR reproduces and RB does not.** WR −2.92 against v20's −2.94 is a match.
TE −0.19 against −0.02 is close. **RB −1.49 against +0.18 is not**, and neither
is QB. `audit3_imputation_decomp.py` decomposes the residual category by
category and it reconciles to the cent:

| pos | imputation estimator error | categories no vendor publishes and nothing imputes | sums to |
|---|---:|---|---:|
| QB | −0.499 | `fumble` −0.267, `pass_2pt` +0.189, `rush_2pt` +0.044 | −0.47 ✓ |
| RB | −0.244 | **`kick_return_yd` +1.201**, `return_td` +0.099, `punt_return_yd` +0.030, `rush_2pt` +0.022, `fumble` −0.105 | −1.49 ✓ |
| WR | −0.216 | **`kick_return_yd` +1.929**, **`punt_return_yd` +0.482**, `return_td` +0.267, `rec_2pt` +0.056, `fumble` −0.028 | −2.92 ✓ |
| TE | −0.180 | `rec_2pt` +0.033, `fumble` −0.022 | −0.19 ✓ |

So the estimator's own error is small everywhere (−0.18 to −0.50). **The entire
remaining bias is player return yardage**, and v20 flagged it only for WR while
recording RB as fixed. It is not: a top-10 RB loses 1.23 pts/game to it.

**Why this matters more than its size.** The consensus and in-house tiers are
ranked against each other for start/sit. A −1.5 pt bias that hits only the
consensus tier makes a consensus RB look worse than an in-house RB who is
genuinely equal. In league-1's week-1 report, four of the eight starters are in
the consensus tier — including the sole RB.

**Recommended fix.** Return duty is a *role*, not a coin flip: its ICC is 0.488
(§7), on a par with receiving yards. Impute it per player from the player's own
prior-season return usage, which `nflreadpy` carries, rather than leaving it at
zero or (worse) assigning a population mean to everyone. This is a genuinely
different move from the flat-constant treatment sacks get, and the ICC is what
justifies it.

---

## 7. Finding 5 (Medium): return yardage is the least-modelled scored category

`audit3_rankability.py` measures, for every category league-1 scores, how much
of a player's week-to-week variation in that category's *points* is stable
(between-player) rather than noise — a one-way ICC over 2025, 436 players with
at least six games.

| category | mean pts/g | sd | **ICC** |
|---|---:|---:|---:|
| `pass_completion` | 0.186 | 0.603 | **0.877** |
| `pass_yd` | 1.013 | 3.344 | 0.866 |
| `pass_incompletion` | −0.103 | 0.343 | 0.826 |
| `rush_yd` | 1.053 | 2.420 | 0.638 |
| `pass_sacked` | −0.107 | 0.422 | 0.603 |
| `pass_td` | 0.537 | 2.181 | 0.596 |
| `reception` | 1.159 | 1.485 | 0.577 |
| `rush_first_down` | 0.453 | 1.024 | 0.565 |
| **`kick_return_yd`** | **0.851** | 2.580 | **0.488** |
| `rec_yd` | 2.016 | 2.840 | 0.474 |
| `rec_first_down` | 0.724 | 1.053 | 0.463 |
| `pass_int` | −0.126 | 0.632 | 0.354 |
| `punt_return_yd` | 0.132 | 0.724 | 0.339 |
| `rush_td` | 0.503 | 2.010 | 0.220 |
| `rec_td` | 0.797 | 2.306 | 0.122 |
| `fumble` | −0.083 | 0.310 | 0.116 |
| `fumble_lost` | −0.080 | 0.411 | 0.042 |
| `return_td` | 0.019 | 0.336 | 0.025 |

Two readings.

**league-1's new v20 categories are the most rankable things in the config.**
Completions (0.877), incompletions (0.826) and sacks taken (0.603) are all more
stable than receiving yards. The league's unusual scoring is, if anything,
*easier* to project than plain PPR at QB. Touchdowns (0.12–0.22) are the noise,
as expected.

**Return yardage is as stable as receiving yards and is not modelled at all.**
`blend.VOLUME_COLUMNS` carries targets, carries, attempts, receptions, target
share, WOPR, air yards, and snap share — and no return usage. So the whole
0.85 + 0.13 pts/game sits in the target as unexplained variance.

Its cost is measurable directly. Recompute the *actual* with the return lines
removed, then measure how well the same shipped projection ranks it (held-out
2025, trained 2022-24):

| pos | Spearman vs actual | vs actual without return | Δ |
|---|---:|---:|---:|
| QB | 0.4587 | 0.4587 | +0.0000 |
| **RB** | 0.5667 | **0.6736** | **+0.1069** |
| **WR** | 0.4604 | **0.5603** | **+0.0999** |
| TE | 0.5891 | 0.5891 | +0.0000 |

That gap is most of the difference between the two leagues. On identical stat
lines and an identical estimator, the shipped stack scores held-out 2025 at
Spearman 0.5645 for league-1 and 0.6545 for league-2; at WR specifically, 0.460
vs 0.617. The scoring rule, not the model, is where the gap comes from — and it
is concentrated in one line.

---

## 8. Tested and rejected: a rolling return-yardage blend feature

Audit 2's most useful section recorded an idea that did not replicate. This is
this audit's.

Everything above says the same thing: return yardage is worth ~1 pt/game to the
WR/RB population, its ICC says it is predictable, and the model has no feature
for it. The obvious move is to add rolling `kick_return_yd` / `punt_return_yd`
to `blend.VOLUME_COLUMNS`. `audit3_return_volume.py` does exactly that, changing
nothing else, and measures **against the stack we ship** (rolling average +
shrinkage + blend + affine), trained on 2022-24 and tested on 2025.

**league-1:**

| variant | RMSE | MAE | pairwise start/sit | Spearman |
|---|---:|---:|---:|---:|
| SHIPPED blend | 7.1806 | 5.3340 | **0.6753** | 0.5645 |
| SHIPPED + return volume | **6.8976** | **4.9766** | 0.6571 | **0.6438** |

Paired test on per-row squared error: **z = +10.34, p = 4.8e-25** in favour of
the feature. Pairwise evaluated on 294 756 orderable within-(position, week)
pairs, deterministically enumerated and scored on the same pairs for both
variants.

By position:

| pos | RMSE base | RMSE +ret | Sp base | Sp +ret | pairwise base | pairwise +ret | Δ pairwise |
|---|---:|---:|---:|---:|---:|---:|---:|
| QB | 9.8604 | 9.8604 | 0.4587 | 0.4587 | 0.6674 | 0.6674 | +0.0000 |
| RB | 7.6369 | **7.2965** | 0.5667 | **0.6861** | 0.6983 | 0.6898 | **−0.0085** |
| WR | 6.6543 | **6.1561** | 0.4604 | **0.6074** | 0.6566 | 0.6287 | **−0.0280** |
| TE | 5.8252 | 5.8240 | 0.5891 | 0.5890 | 0.7150 | 0.7151 | +0.0000 |

**league-2** (which does not score return yardage): identical to six decimal
places on every metric. The feature is an exact no-op there, which is the right
sanity check that the harness is measuring what it claims.

### Verdict: rejected

RMSE falls by 0.283 with p = 4.8e-25. MAE falls by 0.357. Pooled Spearman rises
by 0.079. And **within-week start/sit accuracy falls at both positions the
feature touches** — 2.8 points at WR, 0.9 at RB, with QB and TE untouched. That
is not noise: it is concentrated exactly where the feature acts, and the
untouched positions move by 0.0000.

Under this repo's own acceptance rule — error and ranking must move together,
the rule that exists because window=6 improved MAE while degrading the order —
this fails. It is worth noting that it fails in the *opposite* direction from
the usual case: here RMSE and pooled Spearman both improve and the ranking
metric that matters degrades.

**Why the two ranking metrics disagree**, because that is the useful part.
Pooled Spearman is computed across all weeks, so it rewards separating
season-long returners from non-returners — a between-player, level effect, which
is real and which the feature captures. Within-week pairwise asks which of two
same-position players outscores the other *this week*, which is the actual
start/sit question. Return yardage is bursty within a week (a returner posts 0
or 90), so adding a stable expected 1–2 pts to every returner moves them up the
within-week ordering more often than the outcome justifies.

**What to try instead** (untested, offered as a hypothesis, not a
recommendation): model return yardage as a separate additive expectation with
its own uncertainty rather than as a ridge feature that perturbs the whole
prediction. That keeps the level correction — which is what fixes the consensus
tier's RB bias in §6 — without letting it reorder within-week starts. It should
be measured on both metrics before anyone believes it.

**Also worth recording:** this is the second time a plausible per-position
signal has looked strong on error and failed on ranking or replication (after
v18's opportunity-based touchdown estimator). The pattern is consistent enough
to be a design note: *this repo's estimator is already close to the ceiling on
level, and the remaining headroom is in ordering.*

---

## 9. Task 2: vendor comparison — **could not be run**

The task specified pulling Sleeper, ESPN and FantasyPros week-1 2026
projections, re-scoring their raw stat lines through each league's config, and
comparing. **All three vendor hosts are blocked by this sandbox's egress
policy.** This is a hard limitation, recorded rather than worked around.

Evidence, from the proxy's own status endpoint and from direct probes with the
real FantasyPros key loaded:

```
api.sleeper.app:443                CONNECT -> 403 (policy denial)
lm-api-reads.fantasy.espn.com:443  CONNECT -> 403 (policy denial)
api.fantasypros.com:443            CONNECT -> 403 (policy denial)
site.api.espn.com:443              CONNECT -> 403 (policy denial)   [news.py's source]
api.the-odds-api.com:443           CONNECT -> 403 (policy denial)
github.com / raw.githubusercontent.com  -> reachable
```

Confirmed both from the shell and through the harness's own fetch tool, which
returned `EGRESS_BLOCKED` for `api.sleeper.app`. Only GitHub is reachable, which
is why `nflreadpy` (whose data lives on nflverse GitHub releases) works and
nothing else does. **The API keys were never the problem and were never
rejected — they were simply never reachable.** Nothing was spent on the
builder's Anthropic account.

Consequently these are **not run** and are marked as such: the live three-vendor
pull, aggregate per-position agreement across the whole pool, and any
vendor-disagreement flagging. `news.py`'s live path is likewise unauditable —
its source host is blocked (§10.2).

### 9.1 What was delivered instead

**(a) The harness, written and reviewable.**
`docs/research/scripts/audit3_vendor_compare.py` does the full method: pull,
map each vendor's raw stat line into this repo's shape, run
`impute_unpublished_categories`, set `position` on every line, and re-score
through `compute_league_points` with each league's own config. It **refuses to
print a comparison table** without `--i-have-verified-the-stat-ids`, because
`ESPN_STAT_IDS` is exactly the shape of bug that cost this project three silent
defects in v16 and has not been checked against a live response. `--probe`
dumps one raw entry per vendor so the ids can be verified first. That refusal is
deliberate: an unverified stat-id map that prints plausible numbers is worse
than no script.

**(b) The reduced-basis cost, quantified** — the part of Task 2 that needs no
vendor network. §6's decomposition is the answer to "quantify how much that
reduction is worth per position". For league-1, per top-10 player-game:

| pos | lost to categories no vendor publishes | of which return yardage |
|---|---:|---:|
| QB | 0.03 | 0.00 |
| RB | 1.25 | **1.23** |
| WR | 2.71 | **2.41** |
| TE | 0.01 | 0.00 |

Vendors publish yards/TDs/receptions/INTs/fumbles. Rotowire additionally
publishes **real completions and attempts**, so its incompletions are exact
rather than estimated; `setdefault` in `impute_unpublished_categories` means a
published value always wins. Nobody publishes first downs, sacks taken, return
yardage, 2-point conversions, or return TDs.

**(c) The starter table**, from the shipped week-1 fixtures — which *do* carry
vendor-derived numbers for the players inside the consensus tier (FantasyPros +
Rotowire raw stat lines, re-scored through each league's config by the pipeline
at generation time, 2026-09-08) — plus ESPN's own in-app per-game projections
for the five starters `CLAUDE.md` records the builder capturing by hand.

### 9.2 league-1 starters (AUT-HUN, 14-team TE-premium), week 1

`audit3_starters.py`. `cond` = if-he-plays; `exp` = P(play)-weighted, the number
on the card. **The ESPN column is ESPN's own in-app projection and is compared
to `cond`**, which is the like-for-like quantity — a vendor projection is a
conditional number, not an availability-weighted one.

| slot | player | pos | tier | cond | exp | P(play) | 10th | 90th | ESPN | gap |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| QB | Jaxson Dart | QB | consensus | 21.99 | 20.66 | 0.94 | 3.25 | 33.76 | 20.70 | +1.29 |
| RB | Javonte Williams | RB | consensus | 17.68 | 16.00 | 0.91 | 0.00 | 30.25 | 18.00 | −0.32 |
| WR | Jaxon Smith-Njigba | WR | consensus | 19.39 | 18.15 | 0.94 | 6.05 | 30.77 | 19.30 | +0.09 |
| TE | Tyler Warren | TE | consensus | 15.07 | **10.79** | **0.72** | 0.00 | 24.64 | 14.60 | +0.47 |
| FLEX | George Pickens | WR | consensus | 16.26 | **12.80** | **0.79** | 0.00 | 26.45 | — | — |
| FLEX | Dallas Goedert | TE | consensus | 12.78 | 11.46 | 0.90 | 0.00 | 23.69 | — | — |
| D/ST | Jaguars | DST | in-house | 7.42 | 7.42 | 1.00 | — | — | — | — |
| K | Cameron Dicker | K | in-house | 9.03 | 8.15 | 0.90 | — | — | — | — |
| | **STARTER TOTAL** | | | **119.62** | **105.43** | | | | **118.50** | **+1.12** |
| BN | Alec Pierce | WR | week1 | 11.02 | 10.32 | 0.94 | 0.31 | 20.00 | — | — |
| BN | Josh Downs | WR | week1 | 9.30 | **4.82** | **0.52** | 0.00 | 14.24 | — | — |
| BN | KC Concepcion | WR | week1 | 6.49 | 5.11 | 0.79 | 0.00 | 14.76 | — | — |
| BN | Baker Mayfield | QB | week1 | 19.80 | 18.60 | 0.94 | 7.10 | 33.69 | 18.20 | +1.60 |

Per-player agreement with ESPN is close: +1.29, −0.32, +0.09, +0.47, +1.60,
**mean +0.63**, all five inside our own 10–90 band. The starter total is
+1.12 against ESPN's 118.5, reproducing `CLAUDE.md`'s recorded 119.6. **No
starter is flagged.**

Under a threshold defined up front — flag when the gap exceeds either 3.0 pts
or 20% of the projection, *and* falls outside our own 10th–90th band — nothing
in this table qualifies. With only five external reference points that is a
weak test, and it is stated as weak: it cannot detect a bias shared by ESPN and
by us.

**Two things in the table are worth more attention than the ESPN gaps:**

1. **P(play) is doing more work than the vendor gap ever could.** Tyler Warren
   loses **4.28 points** (15.07 → 10.79) to a 0.72 play probability, and George
   Pickens **3.46** (16.26 → 12.80) to 0.79. Those two adjustments together are
   larger than the entire starter-level disagreement with ESPN. Yet P(play) is
   shown only inside the card's expandable section and only below 0.85 — so
   Goedert's 0.90 haircut is invisible entirely. See §10.
2. **Five of the eight starters have a published 10th percentile of exactly
   0.00**, because any P(play) ≤ 0.90 forces the floor to the zero atom. "0.0 –
   26.5" is a correct band and a poor decision surface.

### 9.3 league-2 starters (Intuitech, 8-team PPR), week 1

No external reference exists for this league (the builder's ESPN capture is
league-1's). Our numbers only.

| slot | player | pos | tier | cond | exp | P(play) | 10th | 90th |
|---|---|---|---|---:|---:|---:|---:|---:|
| QB | Jaxson Dart | QB | consensus | 19.17 | 18.01 | 0.94 | 3.80 | 28.35 |
| RB | Jonathan Taylor | RB | consensus | 18.85 | 17.06 | 0.91 | 3.41 | 29.86 |
| RB | Bucky Irving | RB | week1 | 13.54 | 12.26 | 0.91 | 2.63 | 22.06 |
| WR | Jaxon Smith-Njigba | WR | consensus | 19.49 | 18.25 | 0.94 | 7.01 | 30.43 |
| WR | Drake London | WR | week1 | 14.44 | 13.52 | 0.94 | 3.26 | 22.57 |
| TE | Brock Bowers | TE | consensus | 15.57 | 13.96 | 0.90 | 0.00 | 24.84 |
| FLEX | Tee Higgins | WR | consensus | 15.62 | **8.10** | **0.52** | 0.00 | 22.31 |
| FLEX | Cam Skattebo | RB | week1 | 15.15 | 13.71 | 0.91 | 4.24 | 23.67 |
| K | Cameron Dicker | K | in-house | 9.22 | 8.33 | 0.90 | — | — |
| DEF | Eagles | DST | in-house | 8.32 | 8.32 | 1.00 | — | — |
| | **STARTER TOTAL** | | | **149.37** | **131.52** | | | |

**Tee Higgins is the one player either roster should be looking at.** P(play) =
0.52 costs him **7.52 points** — he is projected as the second-best FLEX option
if he plays and the worst if you weight by availability. That is a genuine
start/sit decision the report currently surfaces in an expandable panel.

Note the same player, Jaxon Smith-Njigba, scores 19.39 in league-1 and 19.49 in
league-2 from the same stat line — the leagues' rules genuinely differ, and the
engine is applying each. Dart moves 21.99 → 19.17 (league-1 pays completions,
first downs and 0.05/passing yard; league-2 pays 0.04 and neither).

### 9.4 Vendor tier coverage of the two rosters

- league-1: **6 of 12** rostered players reached the vendor tier (Dart,
  Javonte Williams, Smith-Njigba, Warren, Pickens, Goedert).
- league-2: **6 of 15** (Dart, Taylor, Smith-Njigba, Bowers, Higgins, Fannin).

So roughly half of each roster is priced by our own model with no vendor
cross-check at all — which is the architectural point the two-tier design has
always made, and the reason §6's tier-comparability bias matters.

---

## 10. Remaining Task 1 sections

### 10.1 Calibration and intervals, walk-forward (`audit3_walkforward.py`)

Trained on 2022-24, tested on every played 2025 player-week (n = 6 037).
All four metrics reported together, per CLAUDE.md.

**league-1:**

| variant | RMSE | MAE | pairwise | Spearman |
|---|---:|---:|---:|---:|
| raw (rolling + shrinkage) | 7.3547 | 5.5252 | 0.6783 | 0.5363 |
| + blend | 7.1762 | 5.3498 | 0.6757 | 0.5634 |
| + blend + affine (**shipped**) | 7.1806 | 5.3340 | 0.6761 | 0.5645 |

**league-2:**

| variant | RMSE | MAE | pairwise | Spearman |
|---|---:|---:|---:|---:|
| raw (rolling + shrinkage) | 6.3787 | 4.7208 | 0.7240 | 0.6106 |
| + blend | 6.1686 | 4.5142 | 0.7285 | 0.6536 |
| + blend + affine (**shipped**) | 6.1682 | 4.4755 | 0.7276 | 0.6545 |

The blend earns its place in both leagues (RMSE −0.18 and −0.21, Spearman +0.027
and +0.043). The affine is, as advertised, essentially rank-neutral and improves
MAE. Per position, shipped stack:

| | league-1 RMSE | Sp | pairwise | bias | league-2 RMSE | Sp | pairwise | bias |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| QB | 9.8604 | 0.4587 | 0.6674 | +0.008 | 8.2573 | 0.4609 | 0.6696 | +0.090 |
| RB | 7.6369 | 0.5667 | 0.6983 | +0.755 | 6.3461 | 0.6881 | 0.7490 | +0.343 |
| WR | 6.6543 | 0.4604 | 0.6566 | +0.318 | 5.9310 | 0.6173 | 0.7251 | −0.080 |
| TE | 5.8252 | 0.5891 | 0.7150 | −0.161 | 5.0371 | 0.5918 | 0.7174 | −0.105 |

QB is the weakest position by ranking in both leagues (Spearman 0.46, pairwise
0.67) despite being the one slot every lineup must fill. That is not new to this
audit but it is now measured on a walk-forward basis and is the largest
remaining modelling gap after return yardage.

**Interval coverage (a): conditional band, nominal 80%, held-out 2025**

| | pooled | QB | RB | WR | TE |
|---|---:|---:|---:|---:|---:|
| league-1 | **0.799** | **0.741** | 0.798 | 0.792 | 0.846 |
| league-2 | **0.830** | **0.758** | 0.815 | 0.847 | 0.852 |

Pooled coverage is at or slightly above nominal — the interval model is honest.
**QB under-covers by 4–6 points in both leagues** (finding 6): QB intervals are
too narrow, which understates a quarterback's downside. TE over-covers.

**Interval coverage (b): the mixture band, Monte Carlo**

Audit 2's fix claims `Q(t) = 0` for `t ≤ 1−p`. Verified independently by drawing
40 000 outcomes per cell from the fitted conditional quantile curve by inverse
transform and zeroing them with probability 1−p:

| pos | p | floor | ceiling | MC coverage |
|---|---:|---:|---:|---:|
| RB | 1.00 | 1.37 | 20.89 | 0.799 |
| RB | 0.95 | 0.64 | 20.62 | 0.800 |
| RB | 0.90 | 0.00 | 20.32 | **0.869** |
| RB | 0.70 | 0.00 | 18.70 | **0.879** |
| RB | 0.50 | 0.00 | 16.08 | **0.883** |
| WR | 0.90 | 0.00 | 20.37 | **0.876** |
| WR | 0.50 | 0.00 | 15.93 | **0.887** |

**The v18 fix holds.** At p ≥ 0.95 coverage is 0.798–0.801 against a nominal
0.80 — a decisive improvement on the pre-fix 64% at p = 0.85 and 26.5% at
p = 0.50 that audit 2 measured.

Finding 10 is the nuance: once p ≤ 0.90 the floor is exactly 0 and the interval
`[0, Q(0.90)]` swallows the entire zero-atom, so its realised probability
content is 87–89%, not 80%. This is **correct behaviour and correctly labelled**
— `PlayerCard.jsx` says "Likely range (10th-90th percentile)", which is exactly
what it is. But since audit 2 established that 88% of published bands sit below
p = 0.90, nearly every band the user sees is an ~88% interval while a
fully-healthy player's is an 80% one, so band *widths* are not comparable across
players. Worth a tooltip, not a code change.

### 10.2 The consensus tier and the news layer

**Consensus tier.** It reconciles on the same scoring basis — `fantasypros.py`
and `rotowire_projections.py` both route through `compute_league_points` with an
explicit `position`, never reading `points`/`points_ppr`. v20's
`blend_consensus_projections` position bug is fixed and the fix is correct. The
remaining tier-comparability problem is §6's, and it is a coverage problem, not
a basis problem.

One residual risk worth recording, not measurable here: `blend_consensus_projections`
averages the two vendors' stat lines key-by-key, substituting **0.0** for a key
present in one and absent from the other. Both vendors run through
`impute_unpublished_categories` first, so the categories that matter are present
on both sides — with one exception: **FantasyPros publishes `fumbles` and
Rotowire publishes no fumble field at all**, so a blended player carries half the
fumble penalty. At league-1's −2 per fumble lost and a typical weekly projection
of ~0.05 fumbles, that is ~0.05 pts/game — negligible, but it is the same
"absent contributes 0" reasoning v20 rejected, reintroduced one layer up. The
principled form is to average over the keys each side actually supplies rather
than over the union.

**News layer.** Its live path could not be audited: `site.api.espn.com` is
egress-blocked (§9), so no article could be fetched and no summary generated.
The LLM call itself was not exercised and nothing was billed.

What *can* be audited is whether the two signals agree, since every shipped
report already carries `newsFlag.riskLevel` and `projection.playProbability`
side by side. Over all 18 weeks of both leagues (`audit3_news_vs_availability.py`):

| news `riskLevel` | n | mean P(play) |
|---|---:|---:|
| none | 12 900 | 0.387 |
| low | 2 367 | 0.315 |
| **medium** | 116 | **0.404** |
| high | 1 481 | **0.006** |

| news `designation` | n | mean P(play) |
|---|---:|---:|
| Healthy | 13 880 | 0.394 |
| Questionable | 1 503 | 0.210 |
| Out | 265 | 0.038 |
| IR | 1 216 | 0.001 |

**`designation` is monotone and sensible.** `riskLevel` is **not**: "medium"
(0.404) sits *above* both "none" (0.387) and "low" (0.315). Only "high" (0.006)
carries real separation. On 116 observations the medium/none difference is not
statistically interesting on its own, and the whole comparison is confounded —
the population mixes starters with deep bench players whose P(play) is low for
reasons unrelated to health. So this is recorded as a **signal to watch, not a
confirmed defect**: the three-level `riskLevel` ladder is doing much less work
than the four-level `designation` does, and `riskLevel` is what the UI colours
its status tag by.

Week-1 disagreements: three players carry a news risk flag while the
availability model is confident (Aaron Jones and MarShawn Lloyd at 0.91, Tyler
Loop at 0.97) — all "medium", consistent with the table above. In the other
direction, 418 players are flagged Healthy with P(play) < 0.75, but those are
overwhelmingly depth players correctly priced low by role, not by health.

### 10.3 Things the first two audits did not reach

- **Category coverage matrix** (`audit3_category_coverage.py`). league-1 scores
  43 categories and **every one has a producer**. league-2 scores 36, four of
  which have no producer and never will: `st_player_forced_fumble`,
  `st_player_fumble_rec`, `def_st_forced_fumble`, `def_st_fumble_rec`. All four
  are documented in the config's own `_unmapped_categories_note`, are worth 1
  point each, and nflreadpy genuinely cannot separate special-teams from
  defensive tackles. **Correctly handled; no action.**
- **Finding 7: the D/ST stat line has no `position`.** `scoring.py`'s
  `compute_league_points` docstring says position "defaults to
  `stat_line["position"]`, which `projections.load_full_pool_game_logs`,
  `dst.assemble_dst_stat_line` and `kicker.nflreadpy_kicker_row_to_stat_line`
  all already set". league-1's config `_linear_notes` repeats the claim.
  `dst.assemble_dst_stat_line` **does not set it** — verified: an assembled
  stat line's keys are `def_points_allowed, def_return_yd, def_sack,
  def_yards_allowed, opponent_team, season, week`. The kicker one does.
  Effect today is exactly zero, because no category either league scopes by
  position (`reception`, the `rec_yd` milestone) appears in a D/ST stat line.
  It is a false invariant in two places, and the fix is one line:

  ```python
  stat_line["def_points_allowed"] = points_allowed
  stat_line["position"] = "DST"          # add
  ```

  Not applied here: it changes what rides on every D/ST stat line, and this
  audit's remit is not to change model inputs. Applying it should be paired
  with a test that a D/ST stat line scores identically before and after.
- **Finding 9: `leagues/league-2/roster.json` has no `playerId`** on any of its
  15 entries, where league-1's carries a `gsis_id` for each. Anything keyed by
  player id must fall back to `(name, position)` matching for league-2, which is
  brittle across name-formatting differences. Cheap to fix from the crosswalk.
- **`week1_kdst.py`'s run-time-fitted k.** Reviewed; no leakage. `k` is chosen
  on `(prior-season mean → next season's week 1)` pairs drawn from completed
  history only; the target season's week 1 is never in the fit. One
  inconsistency worth noting rather than fixing: the shrinkage target is the
  mean over entities with ≥ 4 prior-season games, while an entity *absent* from
  `prior_means` (a rookie kicker, a defence with no usable prior) falls back to
  `calibration_fit.dst_kicker_baselines`, a **different** mean computed over a
  different population. Two positional means are in play on the same report.
  Small, but it means a rookie kicker and a shrunk veteran are not on precisely
  the same scale. The module docstring also says "2020-2025" where CLAUDE.md
  says "2021-2025".

---

## 11. Task 3: UI and experience recommendations

Read: `WeeklyReportView.jsx` (450 lines), `PlayerCard.jsx`, `StatusTag.jsx`,
`TierBadge.jsx`, `docs/specs/user-journey-frontend.md`.

The backend is producing more decision-relevant information than the card
surfaces. Everything below is powered by fields **already in the report JSON**
unless stated. Ordered by leverage, and deliberately short.

### R1 — Put the availability haircut on the card face, not behind a threshold
**Decision improved:** start/sit. **Effort:** ~1 hour. **Data:** exists
(`playProbability`, `conditionalPoints`).

Today `PlayerCard` shows the availability line only when `playProbability <
0.85`, inside `.player-card-expandable`. §9.2 shows why that is the wrong
threshold: Tyler Warren loses **4.28 points** to P(play) = 0.72 and Dallas
Goedert loses 1.32 to 0.90 — and Goedert's is invisible, because 0.90 > 0.85.
Tee Higgins in league-2 loses **7.52**.

These are the largest single adjustments in the whole pipeline — larger than the
model's disagreement with ESPN, larger than the blend, larger than the affine.
Surface them where the number is:

```
  Tyler Warren   TE · IND vs MIA
  10.8   ← 15.1 if he plays · 72% likely            [consensus]
  ────────────────────────────────────────
  0.0 ─────────────────■■■■■■■■──────── 24.6
```

Show the strike-through/arrow form whenever `points` and `conditionalPoints`
differ by more than ~0.5, not below a fixed probability. Two players at 10.8 —
one a reliable 10.8, one a 15.1 who might not play — are *different start/sit
decisions* and currently render identically.

### R2 — Make the range a picture, and stop showing a floor of 0.0 as if it were a projection
**Decision improved:** start/sit under risk. **Effort:** ~2-3 hours. **Data:**
exists (`floor`, `ceiling`).

Five of league-1's eight starters publish a 10th percentile of exactly **0.00**
(§9.2), because any P(play) ≤ 0.90 pushes the floor onto the zero atom. The
text "0.0–26.5" is correct and nearly useless: it is identical for a 79%-likely
16-point receiver and a 52%-likely 9-point one.

Replace the text range with a small inline bar per card — a horizontal track
from 0 to the position's ~95th percentile, the 10–90 band drawn as a filled
segment, the point estimate as a tick. Give the "this player might not play at
all" mass its own visual treatment (a hatched segment at zero sized by `1 −
playProbability`) rather than collapsing it into a floor of 0.0. That single
change makes floor-at-zero informative instead of degenerate, and it makes two
cards comparable at a glance, which text ranges never are.

Add a tooltip noting finding 10: the band is the 10th–90th percentile of the
*outcome*, so its probability content is wider for a doubtful player.

### R3 — Show which tier priced each player, and warn when the two tiers are being compared
**Decision improved:** start/sit and waivers. **Effort:** ~2 hours. **Data:**
exists (`tier`), plus one constant per position from §6.

`TierBadge` already renders the tier. What it does not say is that the two tiers
are **not on the same scale**: §6 measures a consensus-tier RB at −1.49 pts/game
and a WR at −2.92 against an in-house number for the same player. In league-1's
week-1 lineup, four starters are consensus-tier and four are not — and the app
ranks them against each other.

Two options, in order of preference:

1. **Correct it in the backend** (preferred): add the per-position offset from
   §6 to consensus-tier projections, or better, impute return yardage per player.
   Then the badge is purely informational.
2. If not corrected, the UI should not silently rank across tiers. At minimum,
   when a start/sit comparison is within the measured cross-tier bias, say so:
   *"These two are within the known ±1.5 pt gap between our estimate and the
   consensus number — treat as a coin flip."*

This is the recommendation with the most product substance: it is the one place
where a measured statistical fact should change what the interface asserts.

### R4 — A "why" line on every start/sit call
**Decision improved:** trust, and the case-study readability the brief asks for.
**Effort:** ~3 hours. **Data:** exists (`ScoringResult.breakdown` is computed
and then discarded — it would need carrying into the report JSON, ~10 lines in
`generate_report.py`).

`compute_league_points` already returns a per-category breakdown and the report
throws it away. In a league that pays 0.75 a first down, 1.0 a TE reception and
0.1 a return yard, "why is this tight end projected above that receiver" has a
genuinely non-obvious answer, and the engine knows it. One line under the
expanded card — *"12.8 pts: 6.2 receiving, 3.0 receptions (TE premium), 2.3
first downs, 1.3 return yards"* — is the single most persuasive thing this
project could show an interviewer, because it demonstrates the scoring engine
rather than describing it.

### R5 — Surface vendor disagreement once Task 2 can run
**Decision improved:** confidence in a close call. **Effort:** ~2 hours on top
of a working vendor pull. **Data:** does not exist yet (blocked, §9).

When the vendor comparison can be run, the useful UI signal is not the vendor
number — it is the **spread**. Three vendors within a point of each other and of
us is a different situation from three vendors clustered 4 points away from us.
Render as a confidence chip on the card: agreement (all within our 10–90 band),
outlier-us (all three agree and we differ), outlier-vendor (they disagree among
themselves). Do not render individual vendor point totals; they are on different
scoring bases and re-scoring them is exactly the work the harness does.

### Not recommended

- More positions on screen, more filters, more tabs. The report already renders
  992 players; the decision surface is the eight starting slots.
- A vendor-consensus "average of everything" number. Averaging across sources
  with different category coverage reintroduces the §6 bias with no way to
  attribute it.

---

## 12. Task 4: what to do once week 1's real stats land

In priority order. The framing throughout: **n = 1 week is a very weak signal**,
and this project has correctly killed several good-sounding ideas that did not
replicate (v18's opportunity-based touchdown estimator; §8 of this audit). The
discipline that has served it is to be explicit in advance about what a single
week can and cannot settle.

### 12.1 Do this first: fix the P(play) collapse before generating week 2

Finding 1 is not a week-1 problem, it is a **week-2 problem waiting to happen**.
The moment week 1's stats land, every player who did not record a game gets a
current-season play rate of 0/1. Combined with the shrinkage, a genuine starter
who was rested or inactive in week 1 will be marked down sharply on a single
observation, and there is no in-season history to pull him back.

Before running week 2, verify the regenerated report's P(play) distribution
against week 1's — mean should move by a few points, not collapse. This is the
"what could silently break at the cold-start boundary" question, and it is the
answer.

### 12.2 What one week CAN and CANNOT measure

**Can, credibly:**
- **Pipeline correctness.** Does the in-season path produce a sane report? Are
  the tier counts, P(play) distribution and interval coverage in the range
  weeks 2-18 of a real season should show? This is a smoke test, and it is the
  most valuable thing week 1 delivers.
- **Direction of the cold-start tiers.** Did `week1.py` and `week1_kdst.py`
  rank better than a flat baseline? With ~500 scoring players and 31 distinct
  K / 31 distinct D/ST values, **pairwise start/sit accuracy on one week is
  already worth computing** — there are thousands of within-position pairs, not
  one observation. This is the exception to the n=1 rule and should be the
  headline number.
- **Interval coverage, roughly.** ~500 players against a nominal 80% band gives
  a standard error of about 1.8 points, so a coverage of 65% or 95% would be a
  real signal. Anything between 74% and 86% is not.
- **Any catastrophic miss.** A player projected 18 who scores 0 because of a
  join or an id-resolution failure. Look at the tails, not the mean.

**Cannot, and should not be claimed:**
- **That the model is better or worse than last season.** One week's RMSE has an
  enormous standard error at n≈500 with a right-skewed target; a ±0.7 swing is
  noise.
- **Any per-position conclusion.** ~30 QBs is not a sample.
- **That any tuning change helped.** Do not tune on week 1. The temptation will
  be strongest exactly when the data is weakest.
- **Whether the vendor comparison favours us.** See 12.3 — it needs several
  weeks.

### 12.3 Scoring ourselves against the vendors — the cheapest credible benchmark

This is the highest-value new instrument available and it should be set up in
week 1 even though it cannot be *read* until week 4 or so.

**What to store, before kickoff each week:** for every player, each forecaster's
projection re-scored through each league's config — ours (conditional and
expected, separately), Sleeper, ESPN, FantasyPros, Rotowire — plus the
`playProbability` we used. The harness in `audit3_vendor_compare.py` produces
the vendor half; it needs egress and a one-time verification of `ESPN_STAT_IDS`
(§9.1).

**How `track_record.py` should ingest it.** Today it reads back
`weekly-report-week-N.json` as the prediction log, which carries only our
number. The minimal change that makes competing forecasters first-class:

1. Add an optional `competingProjections: {source: points}` map to each
   projection entry in the report JSON. It is additive — existing readers ignore
   it, and the existing fixtures stay valid.
2. Generalise `track_record.py`'s grading loop from "the projection" to "each
   forecaster in turn", keyed by source, so the output carries one accuracy
   block per source rather than one overall.
3. Report **paired** differences, not independent accuracies. The right
   statistic is per-player-week `(our error − their error)`, tested paired —
   the same `paired_significance_test()` already in `backtest.py`. Independent
   RMSEs on different player populations are not comparable, because each
   vendor covers a different set.
4. Grade `conditionalPoints` against the vendors, not `points`. Vendors publish
   conditional numbers. Grade `points` separately against the actual including
   zeros for missed games — that is the availability model's scorecard and it is
   a different question.

**When to read it.** Not before four weeks (~2 000 paired observations per
vendor pair, enough for a paired test to resolve a 0.3-point difference).
Say so up front so a week-1 result is not over-read.

### 12.4 The K/D/ST in-season shrinkage question — the experiment, not an answer

`CLAUDE.md` flags shrinking in-season K/D/ST projections toward *last season's
entity mean* rather than the positional mean as plausible but **unmeasured**,
and warns it should not be assumed to inherit `week1_kdst.py`'s result. It
should not: week 1 is the case where the prior is all the information there is,
and by week 6 the current season carries most of it. The two are different
questions.

**Hypothesis.** For K and D/ST, the shrinkage target in weeks 2-18 should be a
convex combination of last season's entity mean and the positional mean, rather
than the positional mean alone.

**Design.** Walk-forward, leave-one-season-out over 2021-2025, weeks 2-18 only,
both league configs. For each held-out season, fit on the others. Estimator:

```
target(entity, week) = λ · prior_season_entity_mean + (1 − λ) · positional_mean
projection            = shrink(current_season_mean, games_so_far, target, k)
```

Sweep λ ∈ {0, 0.25, 0.5, 0.75, 1.0} and refit `k` at each λ. λ = 0 is the
shipped behaviour and is the control. **Fit λ and k on the training seasons
only** — a λ chosen on the held-out season is the in-sample trap this project
has avoided elsewhere.

**Report, per position, per league, on the held-out seasons pooled:** RMSE, MAE,
within-week pairwise start/sit accuracy, Spearman, and a paired test on squared
error against λ = 0.

**Accept only if all of:**
1. RMSE improves in **both** leagues, at p < 0.05 on the paired test;
2. pairwise accuracy does **not** degrade in either league (§8 is the cautionary
   tale — RMSE alone would have shipped a feature that hurts the actual
   decision);
3. the fitted λ has the same sign and similar magnitude in both leagues (v18's
   opportunity-TD rule: a term that changes sign across two leagues on one code
   path does not ship);
4. the effect survives at week ≥ 6, not only at weeks 2-3 where it is nearly
   the week-1 result again. Report the effect split by week bucket.

**Reject otherwise, and record the rejection**, with the numbers, in the
research docs. Expected effort: half a day, entirely on existing machinery
(`week1_kdst.shrink`, `calibration_fit._walk_forward_rows`).

**Prior expectation, stated in advance so it cannot be rationalised afterwards:**
a small positive λ for D/ST (a defence is its team and persists) and λ ≈ 0 for
K (kickers change teams and the 2026 fitted k of 7.5-13 already says a kicker's
prior season says little). If it comes out the other way round, be suspicious of
the harness before believing it.

### 12.5 The two long-standing open items

**50+/40+ yard TD bonuses and the 1-point safety, via `load_pbp()`.**

- *Cost:* `load_pbp()` is roughly a 10× larger pull than the weekly aggregates
  (~50 k plays/season vs ~6 k player-games), plus a new per-play scoring path
  that `scoring.py`'s linear/milestone/tier vocabulary does not have. Realistically
  1-2 days including a caching layer, and it makes every report run slower for
  ever.
- *Worth:* measurable, and small. From 2025 actuals, `return_td` — a comparable
  rare-event category — is worth 0.019 pts/game with an ICC of **0.025**. The
  distance bonuses are in the same class: 1-2 points, a handful of times a
  season, and essentially unpredictable (a 50-yard TD is a 5-yard TD plus 45
  yards of nobody catching him). Adding them would improve *actuals* accuracy
  by a fraction of a point and *projections* by approximately nothing, because
  the projection would have to carry an expected value that is nearly the
  population mean for everyone.
- **Recommendation: NO-GO**, and stop revisiting it. v15 deferred it, v20
  deferred it, this audit finds the same. The right move is to close it
  explicitly: it is a known, quantified, accepted inaccuracy of well under
  0.1 pts/game, recorded in `_not_computable`, which is the correct place for
  it. Reopen only if `load_pbp()` gets pulled in for some other reason and the
  marginal cost drops to near zero.

**Sportsbook player props.**

- *Cost:* the-odds-api free tier is ~344 credits/month and is per-event, so a
  full week's slate is most of the budget. Needs a vig correction (raw
  implied probabilities sum to >1), a walk-forward validation before any number
  reaches a report, and — as this audit found the hard way — **egress the
  environment may not grant**. Call it 2-3 days plus an ongoing operational
  dependency on an unofficial free tier.
- *Worth:* genuinely the highest ceiling of anything unbuilt. Props are the only
  available signal that prices *this week's* expected receptions and yards for
  *this player* — the market's own conditional projection, which is a strictly
  better input than any rolling average. It is the one candidate that could move
  the ICC ceiling rather than squeezing the existing features.
- **Recommendation: GO, but sequenced third**, after (1) the P(play) fix in
  §12.1 and (2) the vendor benchmark in §12.3. The reason for the ordering is
  discipline, not enthusiasm: without §12.3's paired-comparison harness in place
  there is no way to tell whether props actually beat the vendors we already
  have for free, and this project's own history says that is exactly the
  question that kills good-sounding ideas. Build the scoreboard, then build the
  thing you want to measure on it.

### 12.6 Also worth doing in week 1, cheaply

- **Turn `audit3_column_check.py` into a test** (network-marked, skipped by
  default). It is the check CLAUDE.md's most-repeated rule asks for and it
  currently exists only as an audit artifact.
- **Record the pre-week-1 predictions as an immutable snapshot** before any
  regeneration, so the eval layer has a genuine ex-ante prediction log rather
  than one that could have been rewritten.
- **Re-run this audit's `audit3_reconcile.py` against 2026 week 1** the moment
  stats publish. It is the fastest possible detector of a column rename or a
  feed shape change, and it takes under a minute.

---

## 13. Reproducing this audit

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
.venv/bin/python docs/research/scripts/audit3_column_check.py         # §2
.venv/bin/python docs/research/scripts/audit3_reconcile.py            # §3
.venv/bin/python docs/research/scripts/audit3_dst_kicker.py           # §3
.venv/bin/python docs/research/scripts/audit3_category_coverage.py    # §10.3
.venv/bin/python docs/research/scripts/audit3_nan_probes.py           # §5.1
.venv/bin/python docs/research/scripts/audit3_imputation.py           # §6
.venv/bin/python docs/research/scripts/audit3_imputation_decomp.py    # §6
.venv/bin/python docs/research/scripts/audit3_rankability.py          # §7
.venv/bin/python docs/research/scripts/audit3_return_volume.py        # §8
.venv/bin/python docs/research/scripts/audit3_walkforward.py          # §10.1
.venv/bin/python docs/research/scripts/audit3_news_vs_availability.py # §10.2
.venv/bin/python docs/research/scripts/audit3_starters.py             # §9.2-9.4
.venv/bin/python docs/research/scripts/audit3_vendor_compare.py --probe  # §9 (blocked)
```

The walk-forward, rankability and return-volume scripts each take a few minutes
(they load 2021-2025 game logs). Everything else is under a minute.

Test suite: `cd backend && python -m pytest tests -q` → **621 passed**.
