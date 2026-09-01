"""
Fits the calibration layer from real history, in one call.

calibration.py holds the estimators and blend.py holds the model; this module
is the adapter that gets real nflreadpy game logs into them and hands
generate_report.py a ready-to-use bundle. Split out for the same reason every
other `load_*` in this backend is: it needs the network, so it stays out of the
pure, unit-tested modules.

Everything here is fitted on COMPLETED PRIOR SEASONS only. That is the same
walk-forward discipline projections.py follows, and it is what makes the
resulting numbers honest -- a calibration fitted on the season being projected
would flatter every metric the eval layer later reports.
"""

from __future__ import annotations

from typing import Any, Optional

import baseline
import blend as blend_module
import calibration as calibration_module
from projections import DEFAULT_DECAY, DEFAULT_WINDOW, project_player
from scoring import compute_league_points, nflreadpy_row_to_stat_line

POSITIONS = ("QB", "RB", "WR", "TE")


def load_game_logs(seasons: list[int], positions: tuple[str, ...] = POSITIONS):
    """{player_id: [game, ...]} with league points already computed and the
    volume columns carried through for blend.rolling_volume."""
    import nflreadpy as nfl

    frame = nfl.load_player_stats(seasons=seasons)
    frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
    frame = frame[(frame["season_type"] == "REG") & (frame["position"].isin(positions))]

    logs: dict[str, list[dict[str, Any]]] = {}
    for record in frame.to_dict("records"):
        line = nflreadpy_row_to_stat_line(record)
        line.update(
            season=int(record["season"]),
            week=int(record["week"]),
            position=record["position"],
            team=record["team"],
            opponent_team=record.get("opponent_team"),
        )
        for column in blend_module.VOLUME_COLUMNS:
            line[column] = record.get(column)
        logs.setdefault(record["player_id"], []).append(line)
    return logs


def _walk_forward_rows(
    logs: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    season: int,
    window: int,
    decay: float,
    game_context_by_week: Optional[dict[int, dict[str, dict[str, Any]]]] = None,
    weeks: range = range(1, 19),
) -> list[dict[str, Any]]:
    """One row per player-week of `season`, each carrying the projection that
    would have been made at the time plus the result that followed."""
    week_list = list(weeks)
    season_logs = {
        p: [g for g in log if g["season"] in (season, season - 1)] for p, log in logs.items()
    }
    season_logs = {p: log for p, log in season_logs.items() if log}

    # One baseline per position per week, over the whole population -- what
    # empirical-Bayes shrinkage pulls toward at every sample size (the legacy
    # thin/debut split existed only because the old rule shrank thin samples
    # alone).
    baselines = baseline.positional_baselines_by_week(
        season_logs, scoring_config, season, week_list, window=window, population="all"
    )

    rows = []
    for player_id, log in season_logs.items():
        played = {g["week"] for g in log if g["season"] == season}
        position = next((g.get("position") for g in log if g.get("position")), None)
        team_by_week = {g["week"]: g.get("team") for g in log if g["season"] == season}
        for week in week_list:
            if week not in played:
                continue
            actual_entry = next(
                (g for g in log if g["season"] == season and g["week"] == week), None
            )
            if actual_entry is None:
                continue
            positional_baseline = (baselines.get(week, {}).get(position) or {}).get("baseline")
            projection = project_player(
                log, scoring_config, season, week,
                window=window, decay=decay,
                positional_baseline=positional_baseline,
                shrinkage_k=calibration_module.DEFAULT_SHRINKAGE_K.get(position),
            )
            volume = blend_module.rolling_volume(log, season, week, window, decay)
            context = (game_context_by_week or {}).get(week, {}).get(team_by_week.get(week))
            row = blend_module.build_feature_row(player_id, position, projection, volume, context)
            row.update(
                season=season,
                week=week,
                actual_points=float(compute_league_points(actual_entry, scoring_config)),
                projected=projection["projected_points"],
            )
            rows.append(row)
    return rows


def fit_from_history(
    scoring_config: dict[str, Any],
    season: int,
    seasons: list[int],
    window: int = DEFAULT_WINDOW,
    decay: float = DEFAULT_DECAY,
    fit_blend: bool = True,
) -> dict[str, Any]:
    """Fit every calibration piece on `seasons` and return the bundle
    build_weekly_report_and_pool takes as keyword arguments.

    Returns:
        {"positional_baselines", "shrinkage_ks", "affines", "interval_model",
         "blend_model"} -- each independently None/empty-safe, so a partial
        failure degrades one piece rather than the whole layer.
    """
    logs = load_game_logs(list(range(min(seasons) - 1, season)))

    # Per-position k from the variance components, on the training seasons.
    points_by_position: dict[str, dict[str, list[float]]] = {}
    for player_id, log in logs.items():
        for game in log:
            if game["season"] not in seasons:
                continue
            position = game.get("position")
            if not position:
                continue
            key = f"{player_id}:{game['season']}"
            points_by_position.setdefault(position, {}).setdefault(key, []).append(
                float(compute_league_points(game, scoring_config))
            )
    k_by_position = calibration_module.shrinkage_k_by_position(points_by_position)

    context_by_week = _load_context(seasons)
    rows: list[dict[str, Any]] = []
    for train_season in seasons:
        rows.extend(
            _walk_forward_rows(
                logs, scoring_config, train_season, window, decay,
                context_by_week.get(train_season),
            )
        )

    affines: dict[str, tuple[float, float]] = {}
    for position in {r["position"] for r in rows if r.get("position")}:
        pairs = [
            (r["projected"], r["actual_points"]) for r in rows if r.get("position") == position
        ]
        fitted = calibration_module.fit_affine(pairs)
        if fitted:
            affines[position] = fitted

    interval_model = calibration_module.IntervalModel().fit(
        {"position": r.get("position"), "projected": r["projected"], "actual": r["actual_points"]}
        for r in rows
    )

    blend_model = None
    if fit_blend:
        blend_model = blend_module.BlendModel().fit(rows)

    # Baselines for the CURRENT season are computed by the caller's own week
    # loop; what this returns is the prior-seasons positional mean, used as the
    # shrinkage target before the season has enough of its own data. Keyed by
    # position, matching build_weekly_report_and_pool's parameter.
    positional_baselines: dict[str, float] = {}
    for position in {r.get("position") for r in rows if r.get("position")}:
        values = [r["actual_points"] for r in rows if r.get("position") == position]
        if values:
            positional_baselines[position] = sum(values) / len(values)

    print(
        f"  calibration fitted on {sorted(seasons)}: k="
        + ", ".join(f"{p}={k_by_position.get(p, float('nan')):.2f}" for p in sorted(affines))
        + "; affine slopes "
        + ", ".join(f"{p}={affines[p][1]:.3f}" for p in sorted(affines))
        + (f"; blend for {blend_model.fitted_positions}" if blend_model else "; blend off")
    )

    return {
        "positional_baselines": positional_baselines,
        # Resolved per PLAYER by the caller's loop, but k is a per-position
        # quantity -- expose the position map and let the caller key it.
        "shrinkage_ks": _by_player(logs, k_by_position),
        "affines": _by_player(logs, affines),
        "interval_model": interval_model,
        "blend_model": blend_model,
    }


def _by_player(logs: dict[str, list[dict[str, Any]]], by_position: dict[str, Any]) -> dict[str, Any]:
    """Fan a {position: value} map out to {player_id: value}, which is the
    shape project_player's batch parameters take."""
    out: dict[str, Any] = {}
    for player_id, log in logs.items():
        position = next((g.get("position") for g in log if g.get("position")), None)
        if position in by_position:
            out[player_id] = by_position[position]
    return out


def _load_context(seasons: list[int]) -> dict[int, dict[int, dict[str, dict[str, Any]]]]:
    """{season: {week: {team: context}}} for the training seasons. Returns an
    empty map on any failure -- the blend then trains without game context,
    which is a weaker model but not a broken one."""
    try:
        import nflreadpy as nfl

        from context import game_context_by_team

        frame = nfl.load_schedules()
        frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
        frame = frame[(frame["game_type"] == "REG") & (frame["season"].isin(seasons))]
    except Exception:
        return {}

    out: dict[int, dict[int, dict[str, dict[str, Any]]]] = {}
    for (season, week), group in frame.groupby(["season", "week"]):
        out.setdefault(int(season), {})[int(week)] = game_context_by_team(
            group.to_dict("records")
        )
    return out
