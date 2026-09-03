"""
Determines "the current NFL week to generate a report for" from real
schedule data -- closes the automation gap CLAUDE.md's Next Steps item 3
identified: generate_report.py's `--week` is a required, manually-supplied
argument with no auto-detection, which is fine for a human running it
locally but blocks a scheduled CI job
(.github/workflows/weekly-report.yml) from running unattended.

Pure logic (current_week_from_schedule) is separate from the network
adapter (load_current_week_nflreadpy), same split as every other module in
this backend -- see generate_report.py's own module docstring.
"""

from __future__ import annotations

from typing import Any, Optional


def current_week_from_schedule(schedule_games: list[dict[str, Any]], season: int) -> Optional[int]:
    """Returns the earliest week in `season` that still has at least one
    game with no final score yet -- i.e. "the next week worth generating
    a report for" (games not yet played, so start/sit projections are
    still useful). Falls back to the season's last loaded week if every
    game already has a score (report generation for a fully played
    season, e.g. a hand sanity-check against a past year -- see
    docs/research/hand-sanity-check-2025-weeks-8-12-16.md), and to None
    if no games for `season` are loaded at all.

    Expects dst.load_schedule_with_scores_nflreadpy's row shape (`season`,
    `week`, `home_score`, `away_score`) -- reused rather than duplicated,
    since DST's points-allowed join already needed exactly this superset
    of generate_report.load_schedule_nflreadpy's smaller column set.
    """
    season_games = [g for g in schedule_games if g["season"] == season]
    if not season_games:
        return None

    unplayed_weeks = {g["week"] for g in season_games if not _has_final_score(g)}
    if unplayed_weeks:
        return min(unplayed_weeks)

    return max(g["week"] for g in season_games)


def _has_final_score(schedule_game: dict[str, Any]) -> bool:
    """Whether both teams' final scores are actually present.

    Deliberately not an `is None` check. load_schedules() hands back
    float("nan") -- not None -- for a game that has not been played, and
    NaN is not None, so `score is None` reads every unplayed game as
    played. That inverts this module's whole answer: a season that has not
    kicked off yet has NO unplayed weeks, current_week_from_schedule falls
    through to its "fully played season" branch, and the scheduled run in
    .github/workflows/weekly-report.yml asks for week 18 every week of the
    season instead of the upcoming one. Confirmed against the real 2026
    feed on 2026-09-02: all 272 games carry NaN scores and the function
    returned 18 with week 1 still a week away.

    Third instance of this trap here -- see scoring.py's NaN guard and
    load_player_pool_nflreadpy's `playerId` check. NaN is truthy, NaN is
    not None, and NaN is the one value that does not equal itself, which
    is why that is the test.
    """
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
    """CLI entry point: prints the current week to stdout and nothing
    else, so a GitHub Actions step can capture it directly, e.g.
    `week=$(python3 season_week.py --season 2026)`."""
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
