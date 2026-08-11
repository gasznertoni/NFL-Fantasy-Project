"""
Positional-baseline computation for projections.py's thin-sample shrinkage --
a pool-level counterpart to matchup.py's opponent-difficulty multiplier.
Same "compute externally, inject as a plain float" pattern matchup.py
established: project_player() itself has zero awareness of this module,
just a `positional_baseline` parameter it blends toward when one is
supplied.

Why this module exists (see
docs/research/projection-model-backtest-findings.md, "Round 3", the
`by_confidence` breakdown): project_player() returns a literal
`projected_points = 0.0` for any player with zero games logged before the
target week (`_rolling_average([])` returns `0.0`, and this league's
scoring can't go negative, so every one of those rows has `bias ==
abs_error`, exactly). The `low`-confidence tier (partial window) has
roughly 9x the bias of the `full` tier, consistently underprojecting --
consistent with these players skewing toward role-emerging situations
(call-ups, players who just won a job). A positional baseline gives
project_player() something better than zero to fall back on, and something
to blend a thin sample toward.

Why population="thin" (players with fewer than `window` prior games in
their own season at the time of the game), not "all" player-games at a
position: the population a baseline is applied to must match the
population it's built from. The no_data tier's actual mean score is a few
points (thin-sample/debut games skew low-scoring) -- nowhere near a
pool-wide positional mean, which tracks much closer to the `full` tier's
higher averages (established starters). Baselining thin-sample players
against a pool-wide mean would overshoot in the opposite direction and
likely raise both bias and MAE, not just fail to help. population="all" is
kept available specifically so backtest.py can demonstrate this
empirically rather than the module docstring merely asserting it.

A real backtest run against the full pool found "thin" itself isn't
granular enough for the no_data tier specifically: "thin" pools together
every games_played_before value from 0 up to window-1, and a player on
their 5th game (about to become "full") already scores close to an
established starter -- dragging the pooled mean well above what a true
debut game (games_played_before == 0) actually scores. Confirmed
empirically: injecting the pooled "thin" baseline for no_data rows flipped
their bias from +1.8 (underprojecting a literal 0.0) to roughly -5.4
(badly overprojecting) rather than toward 0 -- see
docs/research/projection-model-backtest-findings.md's Round 4. population=
"debut" (games_played_before == 0 exactly) exists specifically to give the
no_data tier its own correctly-scoped population; "thin" (0..window-1,
unchanged) remains what the "low" tier's blend uses. See
baselines_by_player_week_for_shrinkage, which resolves each player-week to
whichever of the two actually matches that player's games_used at that
week, rather than a caller picking one population for everyone.

A SECOND real confound, found the same way after the fix above: "debut"
alone still overshot (bias -3.6, not the ~0 expected) because
games_played_before==0 conflates two very different events. Week 1 is
when the entire league's roster debuts simultaneously -- established
starters included -- while a genuine in-season "no_data" row (the thing
actually being predicted, since games_used==0 only happens when a player
has zero *current-season* games before the target week) is almost always
a rare mid-season call-up, waiver claim, or injury replacement. Measured
directly against the 2025 pool: 354 of 611 players' season debut was in
week 1 (mean 6.56 points -- a normal starter's game), versus 157 players
debuting week 5 or later (mean 2.00 points) -- much closer to the no_data
tier's actual ~1.8-1.85. `min_week` (see per_game_points_by_position)
exists to exclude week 1 from the debut population for exactly this
reason; DEFAULT_DEBUT_MIN_WEEK below is what backtest.py actually uses.

Prior-season fallback: same-season-only (see projections.games_before)
means a position's population is empty at week 1 (and, with
min_week=DEFAULT_DEBUT_MIN_WEEK, at week `min_week` too), by construction,
for every player league-wide. Falls back to the same position's full
prior season population (same population/min_week choice applied to that
season), tagged source="prior_season" -- directly mirroring matchup.py's
compute_opponent_multiplier's own prior-season fallback for early-season
thin samples.
"""

from __future__ import annotations

import statistics
from typing import Any, Optional

from projections import DEFAULT_WINDOW, games_before
from scoring import compute_league_points

DEFAULT_BASELINE_POPULATION = "thin"  # "thin" | "all" | "debut" -- see module docstring
DEFAULT_BASELINE_STATISTIC = "mean"  # "mean" | "median" -- mean is bias-optimal,
# matching this feature's bias-first ship criterion (see the findings doc);
# median is the MAE-optimal constant for a right-skewed population and is
# kept available as a guardrail comparator.
DEFAULT_DEBUT_MIN_WEEK = 2  # excludes week 1 (the whole-league roster-debut
# week) from population="debut" -- see module docstring's second confound.
# Not applied automatically by any function here (min_week defaults to 1,
# i.e. off, everywhere) -- backtest.py passes this explicitly when building
# the debut population specifically, same "caller opts in" pattern every
# other default in this module follows.

_VALID_POPULATIONS = ("thin", "all", "debut")
_VALID_STATISTICS = ("mean", "median")


def _apply_statistic(points: list[float], statistic: str) -> float:
    if statistic not in _VALID_STATISTICS:
        raise ValueError(f"statistic must be one of {_VALID_STATISTICS}, got {statistic!r}")
    if statistic == "median":
        return round(statistics.median(points), 2)
    return round(sum(points) / len(points), 2)


def _player_position(game_log: list[dict[str, Any]]) -> Optional[str]:
    """Same idiom backtest.py already uses for this exact lookup: a
    player's position is carried on every game-log entry (see
    backtest.load_full_pool_game_logs), so the first present value is
    enough -- a player doesn't change position mid-season in this data."""
    return next((g.get("position") for g in game_log if g.get("position")), None)


def per_game_points_by_position(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    season: int,
    before_week: Optional[int] = None,
    window: int = DEFAULT_WINDOW,
    population: str = DEFAULT_BASELINE_POPULATION,
    min_week: int = 1,
) -> dict[str, list[float]]:
    """Every eligible player-game's actual points, grouped by position --
    the raw population `positional_baselines` reduces to a single scalar.

    Args:
        before_week: as-of filter, same discipline as projections.games_before
            -- only games strictly before this week count. None means the
            whole season (safe only for a fully-completed season -- do NOT
            pass None for the current, in-progress season).
        window: only meaningful when population="thin" -- a player's game
            counts if it's among their first `window` games of the season
            (i.e. `games_used < window` would have been true when that game
            was actually projected). This is the same threshold
            projections._confidence() uses to define the "low"/"full" tier
            boundary, reused here rather than a second, uncalibrated cutoff.
        population: "thin" (default, see module docstring), "all", or
            "debut" (games_played_before == 0 exactly -- the no_data tier's
            dedicated, more narrowly-scoped population, see module
            docstring for why "thin" alone overshoots there).
        min_week: exclude any game with week < min_week from the
            population, regardless of that game's games_played_before
            index. Default 1 (no exclusion). Exists specifically for
            population="debut": week 1 is structurally different from
            every later week -- it's when the entire league's roster
            debuts simultaneously (established starters included), not
            just the rare in-season call-up/waiver-claim debuts that
            actually produce a "no_data" projection row in-season. Pooling
            week-1 debuts into the "debut" population was confirmed
            (empirically, this exact backend/.venv run) to badly overshoot:
            week-1 debuts across the full pool averaged ~6.6 points, week
            5+ debuts averaged ~2.0 -- much closer to the no_data tier's
            actual ~1.8-1.85. See
            docs/research/projection-model-backtest-findings.md's Round 4.

    Returns:
        {position: [points, ...]}. A position with no eligible games at all
        is simply absent from the dict (not an empty list) -- callers
        distinguish "no games" from "computed baseline of 0.0" this way.
    """
    if population not in _VALID_POPULATIONS:
        raise ValueError(f"population must be one of {_VALID_POPULATIONS}, got {population!r}")

    points_by_position: dict[str, list[float]] = {}
    for game_log in game_logs_by_player.values():
        position = _player_position(game_log)
        if position is None:
            continue

        if before_week is None:
            season_games = sorted((g for g in game_log if g["season"] == season), key=lambda g: g["week"])
        else:
            season_games = sorted(games_before(game_log, season, before_week), key=lambda g: g["week"])
        for games_played_before, g in enumerate(season_games):
            if population == "thin" and games_played_before >= window:
                continue
            if population == "debut" and games_played_before != 0:
                continue
            if g["week"] < min_week:
                continue
            points_by_position.setdefault(position, []).append(float(compute_league_points(g, scoring_config)))

    return points_by_position


def positional_baselines(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    season: int,
    as_of_week: int,
    window: int = DEFAULT_WINDOW,
    population: str = DEFAULT_BASELINE_POPULATION,
    statistic: str = DEFAULT_BASELINE_STATISTIC,
    min_week: int = 1,
) -> dict[str, dict[str, Any]]:
    """Single-week baseline per position, as-of `as_of_week`. Reference
    implementation -- straightforward, not optimized for repeated calls
    across many weeks (see positional_baselines_by_week for that; the two
    are required to agree, which is exactly what
    tests/test_baseline.py checks).

    min_week: see per_game_points_by_position's docstring -- excludes
    early-season games (week 1 in particular) from the population.

    Returns:
        {position: {"baseline": float | None, "n": int, "source": str}}.
        source is "current_season", "prior_season" (week-1-style fallback,
        see module docstring), or "no_data" (neither season has any
        eligible games for this position -- returns baseline=None rather
        than guessing; callers should treat None the same as "no baseline
        supplied," i.e. project_player's default no-shrinkage behavior).
    """
    if statistic not in _VALID_STATISTICS:
        raise ValueError(f"statistic must be one of {_VALID_STATISTICS}, got {statistic!r}")

    positions = sorted({
        _player_position(log) for log in game_logs_by_player.values() if _player_position(log)
    })

    current_points = per_game_points_by_position(
        game_logs_by_player, scoring_config, season, before_week=as_of_week, window=window, population=population,
        min_week=min_week,
    )

    out: dict[str, dict[str, Any]] = {}
    for position in positions:
        pts = current_points.get(position, [])
        if pts:
            out[position] = {"baseline": _apply_statistic(pts, statistic), "n": len(pts), "source": "current_season"}
            continue

        prior_pts = per_game_points_by_position(
            game_logs_by_player, scoring_config, season - 1, before_week=None, window=window, population=population,
            min_week=min_week,
        ).get(position, [])
        if prior_pts:
            out[position] = {"baseline": _apply_statistic(prior_pts, statistic), "n": len(prior_pts), "source": "prior_season"}
        else:
            out[position] = {"baseline": None, "n": 0, "source": "no_data"}

    return out


def positional_baselines_by_week(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    season: int,
    weeks: list[int],
    window: int = DEFAULT_WINDOW,
    population: str = DEFAULT_BASELINE_POPULATION,
    statistic: str = DEFAULT_BASELINE_STATISTIC,
    min_week: int = 1,
) -> dict[int, dict[str, dict[str, Any]]]:
    """Batch version of positional_baselines across many weeks -- what
    backtest.py's main() actually calls. Must return results identical to
    calling positional_baselines(...) once per week (tested directly), but
    does it in one chronological pass instead of one full pass per week:
    calling positional_baselines per week from compare_variants' sweep loop
    would be O(weeks x pool size) of redundant compute_league_points calls
    across a 611-player pool.

    min_week: see per_game_points_by_position's docstring.

    The prior-season fallback population doesn't depend on `weeks` at all
    (a completed season is a completed season), so it's computed once
    up front rather than inside the per-week loop.
    """
    if population not in _VALID_POPULATIONS:
        raise ValueError(f"population must be one of {_VALID_POPULATIONS}, got {population!r}")
    if statistic not in _VALID_STATISTICS:
        raise ValueError(f"statistic must be one of {_VALID_STATISTICS}, got {statistic!r}")

    prior_points = per_game_points_by_position(
        game_logs_by_player, scoring_config, season - 1, before_week=None, window=window, population=population,
        min_week=min_week,
    )
    prior_baselines = {
        position: {"baseline": _apply_statistic(pts, statistic), "n": len(pts), "source": "prior_season"}
        for position, pts in prior_points.items()
    }

    # One flat (week, position, points) table for the current season, built
    # per player (so the "thin" games_played_before index is computed
    # against each player's own chronological log) then sorted globally by
    # week so the accumulator below only ever walks forward.
    entries: list[tuple[int, str, float]] = []
    positions_seen: set[str] = set()
    for game_log in game_logs_by_player.values():
        position = _player_position(game_log)
        if position is None:
            continue
        positions_seen.add(position)

        season_games = sorted((g for g in game_log if g["season"] == season), key=lambda g: g["week"])
        for games_played_before, g in enumerate(season_games):
            if population == "thin" and games_played_before >= window:
                continue
            if population == "debut" and games_played_before != 0:
                continue
            if g["week"] < min_week:
                continue
            entries.append((g["week"], position, float(compute_league_points(g, scoring_config))))
    entries.sort(key=lambda e: e[0])

    result: dict[int, dict[str, dict[str, Any]]] = {}
    accumulator: dict[str, list[float]] = {}
    idx = 0
    for target_week in sorted(weeks):
        while idx < len(entries) and entries[idx][0] < target_week:
            _, position, pts = entries[idx]
            accumulator.setdefault(position, []).append(pts)
            idx += 1

        week_result: dict[str, dict[str, Any]] = {}
        for position in positions_seen:
            current_pts = accumulator.get(position, [])
            if current_pts:
                week_result[position] = {
                    "baseline": _apply_statistic(current_pts, statistic),
                    "n": len(current_pts),
                    "source": "current_season",
                }
            elif position in prior_baselines:
                week_result[position] = prior_baselines[position]
            else:
                week_result[position] = {"baseline": None, "n": 0, "source": "no_data"}
        result[target_week] = week_result

    return result


def baselines_by_player_week(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    baselines_by_week: dict[int, dict[str, dict[str, Any]]],
) -> dict[str, dict[int, float]]:
    """Resolves each player's position to the scalar baseline for that
    position at each week -- the {player_id: {week: float}} shape
    backtest_player/compare_variants inject per player, exactly mirroring
    opponent_by_week_by_player's shape for the matchup multiplier.

    Weeks where the resolved baseline is None (source="no_data" -- neither
    season had eligible games for that position) are omitted rather than
    included as None, so callers can use a plain `.get(week)` and treat a
    missing entry as "no baseline available," the same convention
    backtest_player already uses for a missing opponent_by_week entry.
    """
    out: dict[str, dict[int, float]] = {}
    for player_id, game_log in game_logs_by_player.items():
        position = _player_position(game_log)
        if position is None:
            continue
        by_week = {
            week: week_baselines[position]["baseline"]
            for week, week_baselines in baselines_by_week.items()
            if position in week_baselines and week_baselines[position]["baseline"] is not None
        }
        out[player_id] = by_week
    return out


def _games_used_at(game_log: list[dict[str, Any]], season: int, week: int, window: int) -> int:
    """How many games project_player would actually use for this player at
    (season, week) -- mirrors project_player's own
    `games_before(...)[-window:]` computation exactly, so the population
    picked below (debut vs thin) matches what project_player will do with
    the result, not an approximation of it."""
    eligible = games_before(game_log, season, week)
    return len(eligible[-window:])


def baselines_by_player_week_for_shrinkage(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    season: int,
    window: int,
    debut_baselines_by_week: dict[int, dict[str, dict[str, Any]]],
    thin_baselines_by_week: dict[int, dict[str, dict[str, Any]]],
) -> dict[str, dict[int, float]]:
    """The production resolver: picks, per player and per week, whichever
    of the two baseline populations actually matches that player's
    games_used at that week -- population="debut" (games_played_before==0)
    for a player who will be in the no_data tier, population="thin"
    (0..window-1) for a player who will be in the low tier. See the module
    docstring for why a single pooled "thin" population isn't precise
    enough for the no_data tier specifically.

    A player with games_used >= window (the "full" tier) is omitted
    entirely -- project_player never shrinks a full-window sample (see
    _shrinkage_weight), so there is nothing meaningful to resolve for them.
    """
    out: dict[str, dict[int, float]] = {}
    for player_id, game_log in game_logs_by_player.items():
        position = _player_position(game_log)
        if position is None:
            continue

        by_week: dict[int, float] = {}
        weeks = set(debut_baselines_by_week) | set(thin_baselines_by_week)
        for week in weeks:
            games_used = _games_used_at(game_log, season, week, window)
            if games_used == 0:
                source_week = debut_baselines_by_week.get(week, {})
            elif games_used < window:
                source_week = thin_baselines_by_week.get(week, {})
            else:
                continue  # full tier -- never shrunk, nothing to resolve

            entry = source_week.get(position)
            if entry and entry["baseline"] is not None:
                by_week[week] = entry["baseline"]
        out[player_id] = by_week
    return out
