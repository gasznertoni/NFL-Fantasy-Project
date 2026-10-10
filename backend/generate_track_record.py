"""CLI: builds track-record.json from weekly-report fixtures and actual results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from track_record import build_track_record

DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public" / "mock" / "league-1"
DEFAULT_SCORING_CONFIG_PATH = Path(__file__).resolve().parent / "leagues" / "league-1" / "scoring-config.json"


def load_weekly_reports(reports_dir: Path, up_to_week: int) -> dict[int, dict[str, Any]]:
    """{week: WeeklyReport} for each weekly-report-week-N.json found, weeks 1..N."""
    reports = {}
    for week in range(1, up_to_week + 1):
        path = reports_dir / f"weekly-report-week-{week}.json"
        if path.exists():
            reports[week] = json.loads(path.read_text())
    return reports


def load_actual_points_nflreadpy(
    season: int, weeks: list[int], scoring_config: dict[str, Any]
) -> dict[int, dict[str, float]]:
    """{week: {playerId: actual league points}}, D/ST included."""
    from dst import build_dst_game_logs, load_schedule_with_scores_nflreadpy, load_team_stats_nflreadpy
    from generate_report import load_all_game_logs_nflreadpy
    from scoring import compute_league_points

    game_logs = load_all_game_logs_nflreadpy(season)
    try:
        team_stats_rows = load_team_stats_nflreadpy(season)
        schedule_games = load_schedule_with_scores_nflreadpy(season)
        game_logs.update(build_dst_game_logs(team_stats_rows, schedule_games))
    except ConnectionError:
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
    parser.add_argument("--out-path", type=Path, default=None)
    parser.add_argument("--scoring-config", type=Path, default=DEFAULT_SCORING_CONFIG_PATH)
    dir_group = parser.add_mutually_exclusive_group()
    dir_group.add_argument(
        "--leagues-config",
        type=Path,
        default=None,
        help=(
            "path to leagues.json manifest; when set, generates one track-record.json "
            "per league. Mutually exclusive with --reports-dir."
        ),
    )
    dir_group.add_argument(
        "--reports-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help=(
            "directory containing weekly-report-week-N.json snapshots (single-league). "
            "Mutually exclusive with --leagues-config."
        ),
    )
    args = parser.parse_args(argv)

    if args.leagues_config:
        leagues_manifest = json.loads(args.leagues_config.read_text())
        backend_dir = Path(__file__).resolve().parent

        for league in leagues_manifest["leagues"]:
            league_id: str = league["leagueId"]
            out_dir = backend_dir / league["outDir"]
            scoring_config_path = backend_dir / league["scoringConfigPath"]
            scoring_config = json.loads(scoring_config_path.read_text())

            reports_dir = out_dir
            out_path = out_dir / "track-record.json"

            print(f"\n--- Track record for {league_id} ({league['displayName']}) ---")

            weekly_reports = load_weekly_reports(reports_dir, args.as_of_week)
            if not weekly_reports:
                print(
                    f"  No weekly-report-week-N.json files found in {reports_dir} "
                    f"for weeks 1..{args.as_of_week} -- skipping."
                )
                continue

            weeks_to_grade = list(range(1, args.as_of_week))
            if weeks_to_grade:
                print(f"  Loading {args.season} actual results for weeks {weeks_to_grade}...")
                actual_points = load_actual_points_nflreadpy(args.season, weeks_to_grade, scoring_config)
            else:
                actual_points = {}

            track_record = build_track_record(args.season, args.as_of_week, weekly_reports, actual_points)

            out_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(track_record, indent=2) + "\n")
            print(f"  Wrote {out_path} ({len(track_record['history'])} history rows across weeks 1-{args.as_of_week})")

    else:
        out_path = args.out_path or (args.reports_dir / "track-record.json")
        scoring_config = json.loads(args.scoring_config.read_text())

        weekly_reports = load_weekly_reports(args.reports_dir, args.as_of_week)
        if not weekly_reports:
            print(f"No weekly-report-week-N.json files found in {args.reports_dir} for weeks 1..{args.as_of_week}.")

        weeks_to_grade = list(range(1, args.as_of_week))
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
