"""
CLI: assembles track-record.json from already-generated weekly-report-
week-N.json snapshots plus real actual results, via track_record.py.

Kept as a separate script from generate_report.py per the integration
plan's orchestration section point 6 ("can run on the same cadence or
independently") -- grading past weeks doesn't need to happen every time a
new week's report is generated.

Only weeks strictly before `--as-of-week` are graded against real
results; the current week's predictions are included in the output
(actualPoints/outcomeCorrect null) but never graded, since that week
hasn't been played yet -- matching the existing track-record.json mock
fixture's own pattern (asOfWeek=3, week-3 entries all null).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from track_record import build_track_record

DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public" / "mock"
DEFAULT_SCORING_CONFIG_PATH = Path(__file__).resolve().parent / "scoring_config.placeholder.json"


def load_weekly_reports(reports_dir: Path, up_to_week: int) -> dict[int, dict[str, Any]]:
    """{week: WeeklyReport dict} for every weekly-report-week-N.json found
    in `reports_dir` for weeks 1..up_to_week. A missing week's file is
    just absent from the returned dict, not an error -- generate_report.py
    may not have been run for every week."""
    reports = {}
    for week in range(1, up_to_week + 1):
        path = reports_dir / f"weekly-report-week-{week}.json"
        if path.exists():
            reports[week] = json.loads(path.read_text())
    return reports


# ---------------------------------------------------------------------------
# Real data adapter -- NOT exercised by the test suite (network + nflreadpy
# required, same caveat as every other load_* function in this backend).
# ---------------------------------------------------------------------------


def load_actual_points_nflreadpy(
    season: int, weeks: list[int], scoring_config: dict[str, Any]
) -> dict[int, dict[str, float]]:
    """{week: {playerId: actual league points}} for the given weeks, via
    the same bulk game-log loaders generate_report.py uses for
    projections -- reused here rather than a second nflreadpy call, since
    they already group each player's/team's real stat lines by (season,
    week).

    Merges in DST actuals (added 2026-08-12, alongside dst.py itself) the
    same way generate_report.main() merges DST into its own game_logs --
    without this, every DST row a weekly report now contains would have
    no matching actual here (dst.py's stat lines are keyed by team
    abbreviation, entirely absent from load_all_game_logs_nflreadpy's
    player-stats-only source), so track_record.history_from_weekly_report
    would look up a DST playerId here, get nothing back, and silently
    leave every DST prediction ungraded forever -- indistinguishable from
    "hasn't happened yet" even after the real week has been played."""
    from dst import build_dst_game_logs, load_schedule_with_scores_nflreadpy, load_team_stats_nflreadpy
    from generate_report import load_all_game_logs_nflreadpy
    from scoring import compute_league_points

    game_logs = load_all_game_logs_nflreadpy(season)
    try:
        team_stats_rows = load_team_stats_nflreadpy(season)
        schedule_games = load_schedule_with_scores_nflreadpy(season)
        game_logs.update(build_dst_game_logs(team_stats_rows, schedule_games))
    except ConnectionError:
        # Same "no stats file published yet" cold-start case
        # generate_report.load_dst_pool_and_game_logs_nflreadpy already
        # handles -- degrade to "no DST actuals this run" rather than
        # failing grading for every other position too.
        pass

    weeks_set = set(weeks)
    actuals: dict[int, dict[str, float]] = {week: {} for week in weeks}
    for player_id, game_log in game_logs.items():
        for game in game_log:
            week = game.get("week")
            if week not in weeks_set:
                continue
            actuals[week][player_id] = float(compute_league_points(game, scoring_config))
    return actuals


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Assemble track-record.json from already-generated weekly-report-week-N.json "
            "snapshots plus real actual results, per docs/design/backend-frontend-integration-plan.md."
        )
    )
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--as-of-week", type=int, required=True)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--out-path", type=Path, default=None)
    parser.add_argument("--scoring-config", type=Path, default=DEFAULT_SCORING_CONFIG_PATH)
    args = parser.parse_args(argv)

    out_path = args.out_path or (args.reports_dir / "track-record.json")
    scoring_config = json.loads(args.scoring_config.read_text())

    weekly_reports = load_weekly_reports(args.reports_dir, args.as_of_week)
    if not weekly_reports:
        print(f"No weekly-report-week-N.json files found in {args.reports_dir} for weeks 1..{args.as_of_week}.")

    weeks_to_grade = list(range(1, args.as_of_week))  # strictly before the current week
    if weeks_to_grade:
        print(f"Loading {args.season} actual results for weeks {weeks_to_grade}...")
        actual_points = load_actual_points_nflreadpy(args.season, weeks_to_grade, scoring_config)
    else:
        actual_points = {}

    track_record = build_track_record(args.season, args.as_of_week, weekly_reports, actual_points)

    out_path.write_text(json.dumps(track_record, indent=2) + "\n")
    print(f"Wrote {out_path} ({len(track_record['history'])} history rows across weeks 1-{args.as_of_week})")


if __name__ == "__main__":
    main()
