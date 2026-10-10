"""Determines the current NFL week from real schedule data. See ARCHITECTURE.md §10."""

from __future__ import annotations

from typing import Any, Optional


def current_week_from_schedule(schedule_games: list[dict[str, Any]], season: int) -> Optional[int]:
    """Earliest week with an unplayed game; last week if all played; else None."""
    season_games = [g for g in schedule_games if g["season"] == season]
    if not season_games:
        return None

    unplayed_weeks = {g["week"] for g in season_games if not _has_final_score(g)}
    if unplayed_weeks:
        return min(unplayed_weeks)

    return max(g["week"] for g in season_games)


def _has_final_score(schedule_game: dict[str, Any]) -> bool:
    """Whether both final scores are present (unplayed is NaN, not None)."""
    for key in ("home_score", "away_score"):
        value = schedule_game.get(key)
        if value is None or value != value:
            return False
    return True


def load_current_week_nflreadpy(season: int) -> Optional[int]:
    from dst import load_schedule_with_scores_nflreadpy

    schedule_games = load_schedule_with_scores_nflreadpy(season)
    return current_week_from_schedule(schedule_games, season)


def main(argv: Optional[list[str]] = None) -> None:
    """CLI: print the current week and nothing else."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Print the current NFL week for --season, per real nflreadpy schedule data."
    )
    parser.add_argument("--season", type=int, default=2026)
    args = parser.parse_args(argv)

    week = load_current_week_nflreadpy(args.season)
    if week is None:
        raise SystemExit(f"No schedule data loaded for season {args.season}")
    print(week)


if __name__ == "__main__":
    main()
