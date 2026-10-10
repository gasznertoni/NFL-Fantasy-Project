"""Game context: Vegas implied team totals. See ARCHITECTURE.md §4."""

from __future__ import annotations

from typing import Any, Optional

from ridge import num

DEFAULT_TOTAL_LINE = 44.5
DEFAULT_IMPLIED_TOTAL = DEFAULT_TOTAL_LINE / 2


def implied_totals(
    total_line: Optional[float], spread_line: Optional[float]
) -> Optional[tuple[float, float]]:
    """(home_implied, away_implied) from a game's line, or None if unpriced."""
    total, spread = num(total_line), num(spread_line)
    if total is None or spread is None:
        return None
    return (total + spread) / 2.0, (total - spread) / 2.0


def game_context_by_team(games: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{team: context} for one week's slate."""
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
    """Numeric context features, with league-average fallbacks for an unpriced game."""
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
        return {}
    return game_context_by_team(frame.to_dict("records"))
