"""Week-1 per-entity estimates for kickers and team defences. See ARCHITECTURE.md §5."""

from __future__ import annotations

import statistics
from typing import Any, Optional

SHRINKAGE_K_GRID = tuple(x / 2 for x in range(0, 81))

FALLBACK_SHRINKAGE_K = {"K": 15.0, "DST": 3.5}

MIN_PRIOR_GAMES = 4
MIN_FIT_OBSERVATIONS = 30


def entity_season_means(
    game_logs_by_entity: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    position: str,
    min_games: int = MIN_PRIOR_GAMES,
) -> dict[str, tuple[float, int]]:
    """{entity: (per-game mean, games played)} for one completed season."""
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
    """Empirical-Bayes shrinkage: n/(n+k) on the entity's mean, the rest on the position's."""
    if games <= 0:
        return positional_mean
    return (games * prior_mean + k * positional_mean) / (games + k)


def fit_shrinkage_k(
    observations: list[tuple[float, int, float]],
    positional_mean: float,
    grid: tuple[float, ...] = SHRINKAGE_K_GRID,
) -> Optional[float]:
    """Pick k minimising RMSE on (prior_mean, prior_games, actual_week1) rows."""
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
    """(prior-season mean, games, next season's week-1 score) rows for fitting k."""
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
    """{entity: week-1 projected points}; entities without a prior are absent."""
    return {
        entity: round(shrink(mean, games, positional_mean, k), 3)
        for entity, (mean, games) in prior_means.items()
    }


def _kicker_logs_by_season(seasons: list[int]) -> dict[int, dict[str, list[dict[str, Any]]]]:
    """{season: {player_id: [stat_line, ...]}} for kickers."""
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
    """{season: {team: [stat_line]}} for team defences, via dst.py."""
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
    """Per-entity week-1 estimates for K and D/ST, plus the fit behind them."""
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
