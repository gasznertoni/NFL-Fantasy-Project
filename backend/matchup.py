"""Opponent win-record multiplier (built, not wired in). See ARCHITECTURE.md §8."""

from __future__ import annotations

from typing import Any, Optional

DEFAULT_RECORD_MULTIPLIER_TIERS = [
    {"below": 0.4, "multiplier": 1.08},
    {"below": 0.6, "multiplier": 1.00},
    {"below": 0.75, "multiplier": 0.98},
    {"below": None, "multiplier": 0.95},
]

MIN_GAMES_FOR_CURRENT_SEASON = 4


def win_pct(wins: float, losses: float, ties: float = 0) -> float:
    """Win percentage with ties as half; 0.0 when no games played."""
    total = wins + losses + ties
    if total == 0:
        return 0.0
    return (wins + 0.5 * ties) / total


def team_record(
    schedule_games: list[dict[str, Any]],
    team: str,
    season: int,
    before_week: Optional[int] = None,
) -> dict[str, Any]:
    """Aggregate a team's win/loss/tie record from raw schedule rows."""
    wins = losses = ties = 0
    for g in schedule_games:
        if g.get("season") != season:
            continue
        if team not in (g.get("home_team"), g.get("away_team")):
            continue
        if before_week is not None and g.get("week", 0) >= before_week:
            continue
        home_score, away_score = g.get("home_score"), g.get("away_score")
        if home_score is None or away_score is None:
            continue

        team_score, opp_score = (
            (home_score, away_score) if g["home_team"] == team else (away_score, home_score)
        )
        if team_score > opp_score:
            wins += 1
        elif team_score < opp_score:
            losses += 1
        else:
            ties += 1

    games_played = wins + losses + ties
    return {
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "games_played": games_played,
        "win_pct": win_pct(wins, losses, ties),
    }


def multiplier_from_win_pct(pct: float, tiers: list[dict[str, Any]] = DEFAULT_RECORD_MULTIPLIER_TIERS) -> float:
    """Multiplier from the first tier whose `below` bound win_pct is under."""
    for tier in tiers:
        if tier["below"] is None or pct < tier["below"]:
            return tier["multiplier"]
    return 1.0


def compute_opponent_multiplier(
    schedule_games: list[dict[str, Any]],
    opponent_team: str,
    target_season: int,
    target_week: int,
    tiers: list[dict[str, Any]] = DEFAULT_RECORD_MULTIPLIER_TIERS,
    min_games_for_current_season: int = MIN_GAMES_FOR_CURRENT_SEASON,
) -> dict[str, Any]:
    """Opponent multiplier from this season's record after 4 games, else last season's."""
    current = team_record(schedule_games, opponent_team, target_season, before_week=target_week)

    if current["games_played"] >= min_games_for_current_season:
        pct, source = current["win_pct"], "current_season"
    else:
        prior = team_record(schedule_games, opponent_team, target_season - 1, before_week=None)
        if prior["games_played"] > 0:
            pct, source = prior["win_pct"], "prior_season"
        else:
            return {
                "multiplier": 1.0,
                "win_pct": None,
                "source": "no_data",
                "games_played_this_season": current["games_played"],
            }

    return {
        "multiplier": multiplier_from_win_pct(pct, tiers),
        "win_pct": pct,
        "source": source,
        "games_played_this_season": current["games_played"],
    }
