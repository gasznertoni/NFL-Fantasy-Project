"""
In-house rolling-average projection model for bench/waiver-tier players --
everyone FantasyPros' free-tier top-10-per-position cap doesn't cover.

Implements docs/design/in-house-projection-model-spec.md sections 3.2-3.3
and 5 (the matchup-difficulty adjustment in section 3.4 is deliberately
deferred, per that doc's own recommendation -- ship the rolling average
alone first). Data loading (nflreadpy) is injected by the caller rather
than called directly from here, so this module is unit-testable with
synthetic game logs and has no network dependency -- see
load_recent_games_nflreadpy() at the bottom for the real adapter, which is
NOT exercised by the test suite (requires network/local nflreadpy).
"""

from __future__ import annotations

from typing import Any, Optional

from scoring import compute_league_points

DEFAULT_WINDOW = 6  # games played -- design spec section 3.2's starting guess was 4;
# raised to 6 per docs/research/projection-model-backtest-findings.md's Round 3
# (2026-08-11, full 611-player pool): window=6 beat window=4 with significance on
# both tuning weeks (p=0.0003) and holdout weeks (p=0.0325), the first time this
# choice cleared a significance bar in either direction.
DEFAULT_DECAY = 1.0  # unweighted mean by default -- see _rolling_average()


def _rolling_average(per_game_points: list[float], decay: float = DEFAULT_DECAY) -> float:
    """Exponentially-decayed average over `per_game_points` (chronological
    order, oldest first -- matches project_player's existing sort). The
    most recent game gets weight 1.0; each game further back is multiplied
    by one more factor of `decay`. decay=1.0 (the default) makes every
    weight equal to 1.0, which is arithmetically identical to the plain
    unweighted mean this function replaces -- so the existing default
    behavior (and TestRollingWindow.test_unweighted_mean_not_recency_weighted
    in tests/test_projections.py) is preserved exactly, not approximated.

    Motivated by the bias sign-flip in
    docs/research/projection-model-backtest-findings.md ("Ideas for
    improving accuracy" #1): an unweighted mean lags a player heating up
    or cooling off, since a game from `window` games ago counts exactly as
    much as last week's. Decay < 1.0 discounts older games so the average
    reacts faster to a recent form change -- the tradeoff the findings doc
    flags is reacting to noise (one big or bad game) rather than a real
    trend, which is why decay is swept and paired-tested in backtest.py
    rather than picked by feel.
    """
    if not (0 < decay <= 1):
        raise ValueError(f"decay must be in (0, 1], got {decay}")
    if not per_game_points:
        return 0.0
    n = len(per_game_points)
    weights = [decay ** (n - 1 - i) for i in range(n)]
    return sum(w * p for w, p in zip(weights, per_game_points)) / sum(weights)


def _games_before(game_log: list[dict[str, Any]], season: int, week: int) -> list[dict[str, Any]]:
    """As-of filter (design spec sections 3.1 and 3.2): games strictly
    before the target week, same season only. A bye week or missed game
    simply isn't in game_log at all (it's a log of games *played*), so no
    zero-filling is needed here -- the caller's game log is assumed to
    already be "games actually played," not a padded weekly calendar.

    Same-season-only is section 3.2's explicit default ("Don't reach into
    the prior season by default -- a player's role can change completely
    between seasons"), not just a temporal-ordering filter -- a week-1
    projection with no current-season games yet correctly returns zero
    eligible games (cold start), not last season's log."""
    return [
        g for g in game_log
        if g["season"] == season and g["week"] < week
    ]


def _confidence(games_used: int, window: int) -> str:
    """Cold-start signal (design spec section 3.2): don't silently average
    over fewer games than the window -- tag it so a report view can show
    "based on 1 game" instead of presenting it with the same visual weight
    as a full window."""
    if games_used == 0:
        return "no_data"
    if games_used < window:
        return "low"
    return "full"


def project_player(
    game_log: list[dict[str, Any]],
    scoring_config: dict[str, Any],
    as_of_season: int,
    as_of_week: int,
    window: int = DEFAULT_WINDOW,
    opponent_multiplier: float = 1.0,
    decay: float = DEFAULT_DECAY,
) -> dict[str, Any]:
    """Project one player's fantasy points for (as_of_season, as_of_week).

    Args:
        game_log: list of {"season": int, "week": int, **raw_stat_line}
            dicts for games this player has actually played, in any order.
            Stat-line keys must match scoring_config's category names (see
            scoring.py's NFLREADPY_OFFENSE_COLUMN_MAP if feeding this from
            raw nflreadpy rows).
        scoring_config: passed straight through to compute_league_points --
            same config the FantasyPros tier uses, so both tiers are
            comparable and a config swap updates both at once.
        as_of_season, as_of_week: the week being projected. Only games
            strictly before this are used (section 3.1's as-of discipline)
            -- required parameters, not optional, so leaking future data is
            a type error, not an easy-to-miss default.
        window: trailing games-played window size. Default 4 per the design
            spec's starting guess (section 3.2) -- explicitly flagged there
            as unresearched, worth revisiting once real data exists.
        opponent_multiplier: defense-vs-position adjustment (design spec
            section 3.4). Defaults to 1.0 (no adjustment) since that piece
            is deferred -- passed as a parameter so it's a one-line addition
            later without changing this function's shape.
        decay: exponential recency-decay factor for the rolling average,
            per docs/research/projection-model-backtest-findings.md's
            "ideas for improving accuracy" #1. Must be in (0, 1]. Defaults
            to 1.0 (unweighted mean, i.e. today's behavior) -- see
            _rolling_average()'s docstring for why 1.0 is exactly
            equivalent, not just close, to the old plain-mean code path.

    Returns:
        Output contract per design spec section 5: player_id (left to the
        caller to attach -- this function is per-player already),
        season, week, projected_points, games_used, confidence,
        opponent_multiplier, per_game_points (the raw points used to build
        the average, for a report view to show the underlying game log
        next to the projection -- design spec section 3.2's mitigation for
        the role-change-discontinuity failure mode), source (CLAUDE.md's
        "keep both tiers clearly labeled" rule -- a fixed marker so a
        report/eval layer merging this with the FantasyPros tier later can
        tell them apart from the data alone, not just from which function
        produced it).

    Raises:
        ValueError: if window is not a positive integer. Python slicing
        makes eligible[-0:] return the *whole* list rather than an empty
        one, so window=0 would otherwise silently mean "no limit" instead
        of "no games" -- reject it outright rather than let that surprise
        a future caller.
    """
    if window <= 0:
        raise ValueError(f"window must be a positive integer, got {window}")

    eligible = _games_before(game_log, as_of_season, as_of_week)
    eligible.sort(key=lambda g: (g["season"], g["week"]))
    recent = eligible[-window:]

    per_game_points = [float(compute_league_points(g, scoring_config)) for g in recent]
    games_used = len(per_game_points)
    rolling_avg = _rolling_average(per_game_points, decay)

    return {
        "source": "in_house_estimate",
        "season": as_of_season,
        "week": as_of_week,
        "games_used": games_used,
        "confidence": _confidence(games_used, window),
        "rolling_avg": round(rolling_avg, 2),
        "decay": decay,
        "opponent_multiplier": opponent_multiplier,
        "projected_points": round(rolling_avg * opponent_multiplier, 2),
        "per_game_points": per_game_points,
    }


def project_players(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    as_of_season: int,
    as_of_week: int,
    window: int = DEFAULT_WINDOW,
    opponent_multipliers: Optional[dict[str, float]] = None,
    decay: float = DEFAULT_DECAY,
) -> dict[str, dict[str, Any]]:
    """Batch wrapper over project_player for a slate of players. Kept
    separate from project_player so the single-player function stays the
    one unit tests exercise directly and this stays a thin loop."""
    opponent_multipliers = opponent_multipliers or {}
    return {
        player_id: project_player(
            game_log,
            scoring_config,
            as_of_season,
            as_of_week,
            window=window,
            opponent_multiplier=opponent_multipliers.get(player_id, 1.0),
            decay=decay,
        )
        for player_id, game_log in game_logs_by_player.items()
    }


# ---------------------------------------------------------------------------
# Real data adapter -- NOT exercised by the test suite (network + nflreadpy
# required; confirmed unreachable from the Cowork cloud sandbox, per
# scripts/probe_sources.py's own header note -- run this adapter locally).
# ---------------------------------------------------------------------------
def load_recent_games_nflreadpy(player_id: str, season: int, before_week: int) -> list[dict[str, Any]]:
    """Adapter from nflreadpy's load_player_stats() to this module's
    game_log shape. Intentionally thin and isolated: if nflreadpy's column
    names drift (flagged as a real risk in the design spec section 2),
    only this function needs to change.
    """
    import nflreadpy as nfl  # local import: keeps this an optional/local-only dependency
    from scoring import nflreadpy_row_to_stat_line

    try:
        stats = nfl.load_player_stats(seasons=[season])
        df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
        df = df[(df["player_id"] == player_id) & (df["week"] < before_week)]

        game_log = []
        for _, row in df.iterrows():
            stat_line = nflreadpy_row_to_stat_line(row.to_dict())
            stat_line["season"] = season
            stat_line["week"] = int(row["week"])
            # Not a scoring category (ignored by compute_league_points, which
            # only reads keys present in scoring_config) -- carried through so
            # backtest.py can build a matchup-multiplier lookup straight from
            # the game log, no separate roster/schedule join needed.
            stat_line["opponent_team"] = row.get("opponent_team")
            game_log.append(stat_line)
        return game_log
    except (KeyError, AttributeError) as exc:
        # Column names/shape here were never confirmed against a live call
        # (design spec section 2 flags this as a real drift risk -- this
        # was built where nflreadpy's data host was unreachable). Re-raise
        # with a pointer at this function instead of a bare pandas
        # KeyError, so whoever runs this first locally isn't left guessing
        # whether it's a real bug or a known nflreadpy-version mismatch.
        raise RuntimeError(
            "load_recent_games_nflreadpy: nflreadpy's load_player_stats() "
            "response shape didn't match this adapter's assumptions "
            "(expected columns include 'player_id', 'week', 'opponent_team'). "
            "This adapter was never run against live data -- check "
            "nflreadpy's actual column names for your installed version "
            "and update this function accordingly."
        ) from exc
