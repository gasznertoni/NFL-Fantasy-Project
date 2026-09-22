"""Per-player prior-season shrinkage targets for the in-season estimator.

The problem this solves. projections.project_player shrinks a player's
current-season rolling average toward a POOL POSITIONAL MEAN. In week 2 that
average is one game, so the empirical-Bayes weight n/(n+k) puts most of the
mass on the target -- and the target is the same number for every player at
the position. The in-house tier therefore collapses toward a single value
exactly when it is asked to rank a lineup.

On the real week-2 2026 report before this module existed (league-1,
TE-premium scoring), the in-house tight ends sat at mean 4.19, sd 0.81, over a
2.06-7.15 range against a consensus tier averaging 13.46 -- 197 players inside
five points of each other.

Two honest caveats on that symptom, because it was first reported worse than
it is. The most dramatic version of it (TE sd 0.56, and Dallas Goedert ranked
126th of 209 at 3.52) came from a run where FANTASYPROS_API_KEY was not loaded,
so the consensus tier was skipped entirely and players who belong in it fell
into the in-house one; with the key loaded Goedert is consensus-tier and ranks
3rd. And what this module changes is only the shrinkage TARGET -- blend.py's
ridge runs afterwards and sets the final conditional, so it dominates the
visible spread either way. What the fix demonstrably does to the shipped
report is reduce ties: distinct in-house values go QB 74->92, RB 123->155,
WR 162->215, TE 107->122. It does not uniformly widen the variance, and
claiming otherwise would be overstating it.

This is the same mistake week1_kdst.py fixed for week-1 kickers and defences,
one position group over: baseline.py's docstring calls a positional mean "what
'no information about this player yet' actually implies", which is true of a
rookie and false of an established starter whose last season is sitting right
there. CLAUDE.md v20 named this extension and flagged it unmeasured. It is
measured now.

What this module does. Builds a per-player shrinkage TARGET -- the player's
own prior-season per-game mean, itself shrunk toward the positional mean by
games played -- and hands it to the `entity_baselines` hook
build_weekly_report_and_pool already exposes for week1_kdst. project_player is
unchanged; only what it shrinks toward changes.

Measured, leave-one-season-out over 2024 and 2025, weeks 2-9, against the pool
positional mean it replaces. Bare estimator first:

    league-1  fit 2024 -> eval 2025   RMSE 7.582 -> 7.366   pairwise .6655 -> .6877   rho .514 -> .568
    league-1  fit 2025 -> eval 2024   RMSE 7.276 -> 7.077   pairwise .6675 -> .6888   rho .532 -> .585
    league-2  fit 2024 -> eval 2025   RMSE 6.512 -> 6.262   pairwise .7175 -> .7394   rho .610 -> .662
    league-2  fit 2025 -> eval 2024   RMSE 6.459 -> 6.264   pairwise .7036 -> .7225   rho .597 -> .647

All four p < 1e-4 on a paired test over squared error. Error down and ranking
up together, in both leagues, on held-out seasons -- the pair CLAUDE.md
requires.

THROUGH THE SHIPPED STACK the gain is much smaller, and that is the number to
quote. Re-measured with blend.py's ridge over rolling volume and Vegas context
trained and applied around it, exactly the v18 retest that killed the
opportunity-based touchdown term:

    league-1  train 2024 -> eval 2025   RMSE 7.1540 -> 7.1070   pairwise .6903 -> .6932   rho .591 -> .598
    league-1  train 2025 -> eval 2024   RMSE 7.0370 -> 6.9900   pairwise .7075 -> .7103   rho .629 -> .636
    league-2  train 2024 -> eval 2025   RMSE 6.1140 -> 6.0710   pairwise .7461 -> .7481   rho .682 -> .689
    league-2  train 2025 -> eval 2024   RMSE 6.2020 -> 6.1610   pairwise .7439 -> .7443   rho .687 -> .691

So the blend already knew most of it -- RMSE gain drops from ~0.2 to ~0.045 --
which is the v18 lesson repeating: the volume block carries much of what a
prior-season mean carries. The difference from expected_td.py is that this
residual is significant (p < 1e-3, all four folds), never changes sign, and
concentrates where the collapse actually lives. Weeks 2-6 only:

    league-1  eval 2025   RMSE 7.1980 -> 7.1190   pairwise .6823 -> .6877   rho .583 -> .595
    league-1  eval 2024   RMSE 7.1110 -> 6.9980   pairwise .6909 -> .6979   rho .601 -> .619
    league-2  eval 2025   RMSE 6.0560 -> 5.9720   pairwise .7474 -> .7531   rho .694 -> .707
    league-2  eval 2024   RMSE 6.2770 -> 6.1840   pairwise .7318 -> .7347   rho .666 -> .681

ONE LIVE WEEK DID NOT CONFIRM IT, and that is recorded here rather than left
out. Week 2 of 2026 was played after this was built, so it is genuinely out of
sample for both variants. Grading the deployed report against the fixed one on
the population the backtest used -- players who actually recorded a stat line:

    all QB/RB/WR/TE  n=343   RMSE 6.749 -> 6.733   pairwise .686 -> .701   rho .597 -> .607   p=0.85
    in-house tier    n=297   RMSE 5.511 -> 5.616   pairwise .651 -> .666   rho .521 -> .522   p=0.22

Nothing significant either way; the ranking nudges up, the in-house RMSE nudges
the WRONG way. n=297 against the backtest's ~5,500 per fold is far too small to
overturn it, and one week is exactly the sample this project has already been
burned by reading too much into -- but it is not confirmation either, and the
next few weeks are worth re-checking against.

Beware one trap in that comparison. Scoring the whole report pool, counting a
player with no stat line as a 0, makes this change look much better than it is
(RMSE 5.041 -> 4.924, p=0.028) while making the ranking look much worse
(Spearman .659 -> .567). Both are artefacts of the 585 never-played players:
their targets drop toward their own low priors, which mechanically shrinks
error against zero and destroys any ordering among a group that all scored the
same thing. Grade on players who played, or the metric measures attendance.

No week cutoff, deliberately. Graded week by week over 2024+2025 the edge is
largest in week 2 (RMSE -0.53 league-1, -0.57 league-2; Spearman +0.14), stays
significant through week 8, and fades to noise from week 9 (p = 0.25, 0.72,
0.91) -- but it is never NEGATIVE at any week. That decay is not something to
hard-code around: it IS the n/(n+k) weight self-attenuating as current-season
games accumulate. A cutoff week would be an arbitrary constant duplicating a
mechanism the estimator already has.

k is ONE GLOBAL CONSTANT, and that is a deliberate departure from
week1_kdst.py, which fits k per position at run time because K and D/ST
genuinely want very different values (7.5-13 against 2-3). Here they do not.
Fitting k per position per season gives QB 4.0 then 1.5, RB 1.0 then 0.5, WR
3.0 then 2.0, TE 0.5 then 3.0 -- an optimum that jumps by 6x between two
adjacent seasons is a flat curve being chased by noise, not per-position
structure, and fitting it would be overfitting a constant. DEFAULT_PRIOR_K
sits inside every one of those per-position optima, and is the value every
number quoted above was measured at.

Scope. QB/RB/WR/TE only, weeks 2+. Week 1 is week1.py's dedicated ridge for
skill positions and week1_kdst.py's per-entity baseline for K and D/ST, both
of which are better-informed than this and already shipped. K and D/ST in
weeks 2+ are NOT covered here -- plausible by analogy, unmeasured in this
work, and CLAUDE.md's own rule is that a result does not transfer to a
population it was not measured on.

Split pure/adapter like every other module here: everything above
load_prior_season_logs_nflreadpy is pure and unit-tested; that one function
needs the network and is not exercised by the suite.
"""

from __future__ import annotations

import statistics
from typing import Any, Optional

# Shared with week1_kdst rather than re-derived: it is the same empirical-Bayes
# form, and two copies of one formula is how they drift apart.
from week1_kdst import shrink

# Fitted by grid search over 0.5-24 on 2024 and 2025 independently, both
# leagues; the leave-one-season-out optimum landed at 1.0-2.0 every time. 2.0
# is the value every measurement in this module's docstring used.
DEFAULT_PRIOR_K = 2.0

# The positions this was measured on. K/DST deliberately absent -- see Scope.
COVERED_POSITIONS = ("QB", "RB", "WR", "TE")

# Week 1 has better-informed models of its own (week1.py, week1_kdst.py).
FIRST_COVERED_WEEK = 2


def entity_season_means(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    positions: tuple[str, ...] = COVERED_POSITIONS,
) -> dict[str, tuple[float, int, str]]:
    """{player_id: (per-game mean, games played, position)} for one completed
    season, scored through `scoring_config`.

    No minimum games filter, unlike week1_kdst.MIN_PRIOR_GAMES. It would be
    redundant here: shrink() already weights a one-game prior at 1/(1+k), so a
    thin prior season collapses to the positional mean on its own rather than
    injecting noise. Every measurement in the module docstring was taken
    without one.
    """
    from scoring import compute_league_points

    out: dict[str, tuple[float, int, str]] = {}
    for player_id, games in game_logs_by_player.items():
        position = next((g.get("position") for g in games if g.get("position")), None)
        if position not in positions:
            continue
        points = [float(compute_league_points(game, scoring_config)) for game in games]
        if points:
            out[player_id] = (statistics.mean(points), len(points), position)
    return out


def entity_baselines(
    prior_means: dict[str, tuple[float, int, str]],
    positional_baselines: dict[str, Optional[float]],
    k: float = DEFAULT_PRIOR_K,
) -> dict[str, float]:
    """{player_id: shrinkage target} -- the player's prior-season mean pulled
    toward his position's pool mean by games played.

    A player with no usable positional baseline is omitted rather than given a
    bare prior-season mean: the caller's fallback (the positional baseline) is
    then used, which is the pre-existing behaviour. Omission is the safe
    direction here, since a target nothing is shrunk toward is worse than a
    slightly stale one.
    """
    out: dict[str, float] = {}
    for player_id, (mean_points, games, position) in prior_means.items():
        positional = positional_baselines.get(position)
        if positional is None:
            continue
        out[player_id] = round(shrink(mean_points, games, float(positional), k), 4)
    return out


def applies(week: int, position: Optional[str]) -> bool:
    """Whether this module should supply a target for (week, position).

    Exposed so the caller states the scope rule once rather than re-deriving
    it, and so the test suite can assert on it directly.
    """
    return week >= FIRST_COVERED_WEEK and position in COVERED_POSITIONS


# ---------------------------------------------------------------------------
# Real data adapter -- NOT exercised by the test suite (network + nflreadpy
# required, same caveat as every other load_* in this backend).
# ---------------------------------------------------------------------------


def load_prior_season_logs_nflreadpy(season: int) -> dict[str, list[dict[str, Any]]]:
    """Game logs for `season - 1`, the season this module's prior comes from.

    One season only. A two-season prior was measured for week1_kdst and was
    worse than one season everywhere, which roster and coordinator turnover
    predicts; that result was not re-tested here, so the single-season shape is
    carried over rather than widened on a guess.
    """
    from backtest import load_full_pool_game_logs

    return load_full_pool_game_logs(season - 1, positions=COVERED_POSITIONS)
