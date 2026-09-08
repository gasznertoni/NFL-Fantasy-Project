"""Week-1 estimates for kickers and team defences.

The problem this solves. calibration_fit.dst_kicker_baselines gives K and D/ST
a prior-season POSITIONAL MEAN, which in week 1 of a new season is the whole
projection: there are no current-season logs, empirical-Bayes weights the
absent sample at zero, and project_player returns the baseline unchanged. So
every kicker in the report projected the same number and every defence
projected the same number. Two of the eight starting slots in league-1 were
therefore unrankable -- the report could not tell the builder which kicker to
start, only that kickers exist.

That baseline's own docstring calls a positional mean "what 'no information
about this player yet' actually implies". For QB/RB/WR/TE that is true in week
1. For K and D/ST it is not: last season happened, the entity persists (a
defence IS its team), and last season's per-game average under this league's
scoring is real information about it.

What this module does. Prior-season per-entity mean, shrunk toward the
positional mean by games played -- the same empirical-Bayes form projections.py
already uses in-season, with k fitted from history rather than assumed.

Measured, leave-one-season-out over 2020-2025 week 1s (2020 excluded: no
crowds, distorted lines), against the flat baseline it replaces:

    league-1  K     RMSE 5.194 -> 5.133  (p=0.045)   pairwise n/a -> 0.537
    league-1  D/ST  RMSE 6.702 -> 6.391  (p=0.014)   pairwise n/a -> 0.566
    league-2  K     RMSE 5.162 -> 5.068  (p=0.031)   pairwise n/a -> 0.546
    league-2  D/ST  RMSE 6.201 -> 6.004  (p=0.048)   pairwise n/a -> 0.555

Pairwise start/sit accuracy is "n/a" for the flat baseline in the literal
sense: every prediction ties, so it orders no pair at all. Going from "ranks
nobody" to 0.54-0.57 is the point of this module; the RMSE gain is modest and
honestly modest, but both moved together, which is the pair CLAUDE.md requires
before accepting a change.

Two things deliberately NOT built, both measured first:

1. A Vegas term (the kicker's own implied team total; the defence's opponent
   implied total, via context.py). It helped league-1 D/ST (RMSE 6.391 ->
   6.334) and HURT both kickers (5.133 -> 5.180) and was flat-to-negative for
   league-2 D/ST. A term that changes sign across two leagues on the same code
   path is not a term to ship -- the same call v18 made on the opportunity-based
   touchdown estimator.
2. A two-season prior. It was worse than one season everywhere (league-1 D/ST
   RMSE 6.391 -> 6.480), which is what roster and coordinator turnover
   predicts. One season only.
"""

from __future__ import annotations

import statistics
from typing import Any, Optional

# Grid searched when fitting k. Wide and coarse on purpose: the RMSE surface is
# flat near the optimum, and the fitted values land far apart by position
# (kickers ~10-20, defences ~3-4), which is itself the finding -- a kicker's
# prior season says much less about him than a defence's says about it.
SHRINKAGE_K_GRID = tuple(x / 2 for x in range(0, 81))

# Used only when history is too thin to fit. Midpoints of the values fitted on
# 2020-2025 across both shipped configs (K 10.5-19.5, D/ST 3.5-3.5).
FALLBACK_SHRINKAGE_K = {"K": 15.0, "DST": 3.5}

MIN_PRIOR_GAMES = 4          # below this a prior season is noise, not a sample
MIN_FIT_OBSERVATIONS = 30


def entity_season_means(
    game_logs_by_entity: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    position: str,
    min_games: int = MIN_PRIOR_GAMES,
) -> dict[str, tuple[float, int]]:
    """{entity: (per-game mean, games played)} for one completed season.

    `entity` is a gsis player id for a kicker and a team abbreviation for a
    defence -- the same keys generate_report already uses as playerId for each.
    """
    from scoring import compute_league_points

    out: dict[str, tuple[float, int]] = {}
    for entity, games in game_logs_by_entity.items():
        points = [
            float(compute_league_points(game, scoring_config, position=position))
            for game in games
        ]
        if len(points) >= min_games:
            out[entity] = (statistics.mean(points), len(points))
    return out


def shrink(prior_mean: float, games: int, positional_mean: float, k: float) -> float:
    """Empirical-Bayes shrinkage toward the positional mean: n/(n+k) on the
    entity's own average, the rest on the position's."""
    if games <= 0:
        return positional_mean
    return (games * prior_mean + k * positional_mean) / (games + k)


def fit_shrinkage_k(
    observations: list[tuple[float, int, float]],
    positional_mean: float,
    grid: tuple[float, ...] = SHRINKAGE_K_GRID,
) -> Optional[float]:
    """Pick k minimising RMSE on (prior_mean, prior_games, actual_week1) rows.

    Returns None when there are too few observations to fit anything, so the
    caller can fall back rather than trust a k fitted on a handful of rows.
    """
    if len(observations) < MIN_FIT_OBSERVATIONS:
        return None
    best_k, best_error = None, None
    for k in grid:
        error = statistics.mean(
            (shrink(mean, games, positional_mean, k) - actual) ** 2
            for mean, games, actual in observations
        )
        if best_error is None or error < best_error:
            best_k, best_error = k, error
    return best_k


def build_observations(
    means_by_season: dict[int, dict[str, tuple[float, int]]],
    week1_actuals_by_season: dict[int, dict[str, float]],
) -> list[tuple[float, int, float]]:
    """Pair each season's prior-season entity mean with what that entity
    actually scored in the NEXT season's week 1 -- the rows fit_shrinkage_k
    trains on. An entity without both halves is skipped."""
    rows = []
    for season, actuals in week1_actuals_by_season.items():
        priors = means_by_season.get(season - 1)
        if not priors:
            continue
        for entity, actual in actuals.items():
            prior = priors.get(entity)
            if prior is not None:
                rows.append((prior[0], prior[1], actual))
    return rows


def week1_estimates(
    prior_means: dict[str, tuple[float, int]],
    positional_mean: float,
    k: float,
) -> dict[str, float]:
    """{entity: week-1 projected points}. An entity absent from prior_means --
    a rookie kicker, a defence with no usable prior season -- simply is not in
    the result, and the caller falls back to the positional mean for it."""
    return {
        entity: round(shrink(mean, games, positional_mean, k), 3)
        for entity, (mean, games) in prior_means.items()
    }


# ---------------------------------------------------------------------------
# Real data adapter -- NOT exercised by the test suite (network + nflreadpy),
# same split as every other module here.
# ---------------------------------------------------------------------------
def _kicker_logs_by_season(seasons: list[int]) -> dict[int, dict[str, list[dict[str, Any]]]]:
    """{season: {player_id: [stat_line, ...]}} for kickers.

    Stat lines are built the same way generate_report's own game-log loader
    builds them -- the offence map merged with the kicker map on every row --
    so a prior-season average here is on exactly the same scale as the
    in-season numbers this estimate is standing in for.
    """
    import nflreadpy as nfl

    from kicker import nflreadpy_kicker_row_to_stat_line
    from scoring import nflreadpy_row_to_stat_line

    stats = nfl.load_player_stats(seasons=[int(s) for s in seasons])
    df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
    df = df[(df["season_type"] == "REG") & (df["position"] == "K")]

    out: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for row in df.to_dict("records"):
        player_id = row.get("player_id")
        if not isinstance(player_id, str) or not player_id.strip():
            continue
        line = {**nflreadpy_row_to_stat_line(row), **nflreadpy_kicker_row_to_stat_line(row)}
        line["week"] = int(row["week"])
        out.setdefault(int(row["season"]), {}).setdefault(player_id, []).append(line)
    return out


def _dst_logs_by_season(seasons: list[int]) -> dict[int, dict[str, list[dict[str, Any]]]]:
    """{season: {team: [stat_line, ...]}} for team defences, via dst.py's
    team-stats self-join. A D/ST's entity key is its team abbreviation, which
    is also its playerId everywhere else in this project."""
    import nflreadpy as nfl

    from dst import build_dst_game_logs

    season_list = [int(s) for s in seasons]
    team_stats = nfl.load_team_stats(seasons=season_list)
    team_stats = team_stats.to_pandas() if hasattr(team_stats, "to_pandas") else team_stats
    team_stats = team_stats[team_stats["season_type"] == "REG"].to_dict("records")

    schedule = nfl.load_schedules()
    schedule = schedule.to_pandas() if hasattr(schedule, "to_pandas") else schedule
    schedule = schedule[
        (schedule["game_type"] == "REG") & (schedule["season"].isin(season_list))
    ].to_dict("records")

    out: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for team, games in build_dst_game_logs(team_stats, schedule).items():
        for game in games:
            out.setdefault(int(game["season"]), {}).setdefault(team, []).append(game)
    return out


def fit_from_history(
    target_season: int,
    scoring_config: dict[str, Any],
    history_seasons: list[int],
) -> dict[str, dict[str, Any]]:
    """Per-entity week-1 estimates for K and D/ST, plus the fit behind them.

    Returns {position: {"estimates": {entity: points}, "positional_mean": float,
    "k": float, "n_fit": int}}. A position whose history is unusable is simply
    absent, and the caller keeps the flat positional baseline for it.

    `history_seasons` should be completed seasons only. The season immediately
    before target_season supplies the priors; the earlier ones supply the
    (prior -> next week 1) pairs that k is fitted on.
    """
    loaders = {"K": _kicker_logs_by_season, "DST": _dst_logs_by_season}
    seasons = sorted({int(s) for s in history_seasons})
    out: dict[str, dict[str, Any]] = {}

    for position, loader in loaders.items():
        try:
            logs = loader(seasons)
        except Exception as exc:  # noqa: BLE001
            print(f"  week-1 {position}: history load failed ({exc}) -- keeping flat baseline.")
            continue

        means_by_season = {
            season: entity_season_means(by_entity, scoring_config, position)
            for season, by_entity in logs.items()
        }
        prior = means_by_season.get(target_season - 1)
        if not prior:
            print(f"  week-1 {position}: no {target_season - 1} history -- keeping flat baseline.")
            continue

        # The positional mean is taken over the same prior season the estimates
        # are shrunk from, so the two halves of the shrinkage are on one scale.
        positional_mean = statistics.mean(mean for mean, _ in prior.values())

        week1_actuals = {}
        for season, by_entity in logs.items():
            actuals = {}
            for entity, games in by_entity.items():
                from scoring import compute_league_points

                first = [g for g in games if int(g.get("week", 0)) == 1]
                if first:
                    actuals[entity] = float(
                        compute_league_points(first[0], scoring_config, position=position)
                    )
            if actuals:
                week1_actuals[season] = actuals

        observations = build_observations(means_by_season, week1_actuals)
        k = fit_shrinkage_k(observations, positional_mean)
        if k is None:
            k = FALLBACK_SHRINKAGE_K[position]
            print(
                f"  week-1 {position}: only {len(observations)} fit rows "
                f"(need {MIN_FIT_OBSERVATIONS}) -- using fallback k={k}."
            )
        out[position] = {
            "estimates": week1_estimates(prior, positional_mean, k),
            "positional_mean": round(positional_mean, 3),
            "k": k,
            "n_fit": len(observations),
        }
    return out
