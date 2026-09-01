"""
Game context: the market's own forecast of how many points a team will score.

`nflreadpy.load_schedules()` carries `spread_line` and `total_line` for free,
for every week including week 1 before any football has been played. From those
two numbers:

    implied_team_total = (total_line +/- spread_line) / 2

which is a direct, current estimate of the offensive environment a player is
walking into -- opponent quality, pace, and expected game script all priced in
at once by a market that updates continuously.

The audit found this is the *right* way to encode "opponent strength" for this
tool. A defence-vs-position rate computed from prior games adds essentially
nothing once the line is present (in-season it is worth ~0.003 RMSE for QB and
nothing elsewhere; for week 1, the block ablation puts it at -0.000). The line
already prices the opponent, and prices this season's version of them rather
than a stale average. That is why matchup.py stays unwired and this module
exists instead.

Measured value in-season, held out on 2025 (RMSE, ridge over the rolling
average plus volume): QB 10.364 -> 10.249, RB 6.630 -> 6.585, TE 5.351 ->
5.337. QB gains most, which is the position the rolling average handles worst.

Extracted so both week1.py and blend.py read the same definition -- an
implied-total sign error in one of two copies would be invisible.
"""

from __future__ import annotations

from typing import Any, Optional

from ridge import num

# Fallbacks for a team-week with no line (a cancelled or not-yet-priced game).
# The league-average total over 2016-2025 is ~44.5, so an even split is ~22.2.
DEFAULT_TOTAL_LINE = 44.5
DEFAULT_IMPLIED_TOTAL = DEFAULT_TOTAL_LINE / 2


def implied_totals(
    total_line: Optional[float], spread_line: Optional[float]
) -> Optional[tuple[float, float]]:
    """(home_implied, away_implied) from a game's line, or None if unpriced.

    nflverse quotes `spread_line` from the HOME team's perspective and
    positive-for-favourite, so the home team's implied total is
    (total + spread) / 2. Getting this backwards is a silent error -- it
    produces plausible numbers with the favourite and underdog swapped -- so it
    is defined exactly once, here.
    """
    total, spread = num(total_line), num(spread_line)
    if total is None or spread is None:
        return None
    return (total + spread) / 2.0, (total - spread) / 2.0


def game_context_by_team(games: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{team: context} for one week's slate.

    Args:
        games: dicts with home_team, away_team, total_line, spread_line.

    Returns:
        {team: {"opponent", "is_home", "implied_team_total", "opponent_implied_total",
                "total_line", "spread_line"}}. `spread_line` is flipped for the
        away team so that, for every team, a positive spread means "favoured" --
        without that flip the same number would mean opposite things depending
        on which row a player happened to be in.
    """
    out: dict[str, dict[str, Any]] = {}
    for game in games:
        home, away = game.get("home_team"), game.get("away_team")
        if not home or not away:
            continue
        total, spread = num(game.get("total_line")), num(game.get("spread_line"))
        pair = implied_totals(total, spread)
        home_implied, away_implied = pair if pair else (None, None)
        out[home] = {
            "opponent": away,
            "is_home": 1.0,
            "implied_team_total": home_implied,
            "opponent_implied_total": away_implied,
            "total_line": total,
            "spread_line": spread,
        }
        out[away] = {
            "opponent": home,
            "is_home": 0.0,
            "implied_team_total": away_implied,
            "opponent_implied_total": home_implied,
            "total_line": total,
            "spread_line": None if spread is None else -spread,
        }
    return out


def context_features(context: Optional[dict[str, Any]]) -> dict[str, float]:
    """The context dict flattened to the numeric features a model consumes,
    with league-average fallbacks for an unpriced game."""
    context = context or {}
    implied = num(context.get("implied_team_total"))
    total = num(context.get("total_line"))
    spread = num(context.get("spread_line"))
    return {
        "implied_team_total": DEFAULT_IMPLIED_TOTAL if implied is None else implied,
        "total_line": DEFAULT_TOTAL_LINE if total is None else total,
        "spread_line": 0.0 if spread is None else spread,
        "is_home": num(context.get("is_home")) or 0.0,
    }


# ---------------------------------------------------------------------------
# Real data adapter -- NOT exercised by the test suite (network + nflreadpy).
# ---------------------------------------------------------------------------
def load_game_context_nflreadpy(season: int, week: int) -> dict[str, dict[str, Any]]:
    """Week `week`'s context for every team, from the schedule's betting lines."""
    import nflreadpy as nfl

    try:
        frame = nfl.load_schedules()
        frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
        frame = frame[
            (frame["season"] == season)
            & (frame["week"] == week)
            & (frame["game_type"] == "REG")
        ]
    except Exception:
        # No lines means every team falls back to the league-average context,
        # which is the behaviour before this module existed -- never a failure.
        return {}
    return game_context_by_team(frame.to_dict("records"))
