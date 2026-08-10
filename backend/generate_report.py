"""
Orchestration script: wires scoring.py + projections.py + news.py +
waiver_targets.py together and writes weekly-report-week-N.json and
player-pool.json in the exact shape frontend/src/lib/api.js already
expects -- the "generation script, not a live API" architecture from
docs/design/backend-frontend-integration-plan.md.

Follows that plan's v0 build order exactly:
  - Real player IDs (nflreadpy's gsis_id) as `playerId` directly, no
    invented p_00123-style scheme and no permanent crosswalk table (the
    plan's recommended resolution to its own "player-ID gap" open
    question -- this project has no real users with saved localStorage
    data yet to migrate).
  - Every player in the `in_house_estimate` tier (gap #2 -- an actual
    FantasyPros pull and top-10-per-position tier selection -- is
    deliberately deferred, per the plan's own explicit v0 scoping: "ship
    100% in-house first ... avoids blocking the whole integration on a
    new data source").
  - Waiver targets via waiver_targets.py's templated (non-LLM) rationale,
    also per the plan's suggested v0 shortcut.

Only QB/RB/WR/TE are covered -- same limitation backtest.py already
documents: scoring.py's NFLREADPY_OFFENSE_COLUMN_MAP has no stat-line
assembly for DST (needs the self-join/schedule-join logic in
docs/research/dst-scoring-fields.md, explicitly out of scope for this
plan) or K (no nflreadpy column mapping exists yet either -- same class of
gap, just not yet documented as its own research doc).

Split, like every other module in this backend, into pure/testable
assembly functions (this module's top half) and network adapter functions
(bottom half, prefixed `load_`) that are NOT exercised by the test suite --
see tests/test_generate_report.py, which only exercises the pure half with
synthetic data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from news import DEFAULT_NEWS_FLAG
from projections import DEFAULT_WINDOW, project_player
from waiver_targets import generate_rationale, select_waiver_targets

# nflreadpy (2025 season, confirmed hands-on) uses "LA" for the Rams; the
# frontend's mock fixtures and the rest of this project's docs use "LAR".
# One-entry map so this doesn't need to grow into a full alias table for a
# single known mismatch.
TEAM_ABBR_DISPLAY_MAP = {"LA": "LAR"}

# scoring.py's NFLREADPY_OFFENSE_COLUMN_MAP only covers these; DST and K
# both need stat-line assembly work that doesn't exist yet (see module
# docstring) so they're excluded from the generated pool entirely rather
# than included with a silently-wrong zero projection.
POOL_POSITIONS = ("QB", "RB", "WR", "TE")

TIER_METADATA = {
    "consensus": {"tierLabel": "Consensus projection", "source": "FantasyPros"},
    "in_house_estimate": {"tierLabel": "Our estimate", "source": "In-house model"},
}

# Matches scoring_config.placeholder.json's reception=0.5 note ("matches
# the frontend's existing half_ppr placeholder assumption") -- both
# placeholders deliberately agree with each other until real league
# scoring values land (CLAUDE.md Next Steps item 2).
LEAGUE_FORMAT_ASSUMPTION = "half_ppr"

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public" / "mock"
DEFAULT_SCORING_CONFIG_PATH = Path(__file__).resolve().parent / "scoring_config.placeholder.json"


def normalize_team(team: Optional[str]) -> Optional[str]:
    if team is None:
        return None
    return TEAM_ABBR_DISPLAY_MAP.get(team, team)


# ---------------------------------------------------------------------------
# Pure assembly logic -- exercised directly by tests/test_generate_report.py
# with synthetic pool/schedule/game_log/news_flag data, no network involved.
# ---------------------------------------------------------------------------


def opponent_for_team_week(
    schedule_games: list[dict[str, Any]], team: str, season: int, week: int
) -> Optional[str]:
    """The other team in this team's game for (season, week), or None if
    there's no scheduled game (a bye week, or a week past the loaded
    schedule) -- callers use None to mean "not playable this week," not
    an error."""
    for game in schedule_games:
        if game.get("season") != season or game.get("week") != week:
            continue
        home, away = game.get("home_team"), game.get("away_team")
        if home == team:
            return away
        if away == team:
            return home
    return None


def build_player_pool_entry(player: dict[str, Any], news_flag: dict[str, Any]) -> dict[str, Any]:
    return {
        "playerId": player["playerId"],
        "name": player["name"],
        "position": player["position"],
        "team": player["team"],
        "newsFlag": news_flag,
    }


def _projection_object(points: float, tier: str) -> dict[str, Any]:
    meta = TIER_METADATA[tier]
    return {"tier": tier, "points": points, "tierLabel": meta["tierLabel"], "source": meta["source"]}


def build_projection_entry(candidate: dict[str, Any], tier: str = "in_house_estimate") -> dict[str, Any]:
    """`candidate` is the internal per-player shape assembled in
    build_weekly_report_and_pool below (playerId/name/position/team/
    opponent/points/news_flag/...) -- this reshapes it into the public
    WeeklyReport `projections[]` entry shape api.js's callers expect."""
    return {
        "playerId": candidate["playerId"],
        "name": candidate["name"],
        "position": candidate["position"],
        "team": candidate["team"],
        "opponent": candidate["opponent"],
        "projection": _projection_object(candidate["points"], tier),
        "newsFlag": candidate["news_flag"],
    }


def build_waiver_target_entry(
    candidate: dict[str, Any], rationale: str, tier: str = "in_house_estimate"
) -> dict[str, Any]:
    entry = build_projection_entry(candidate, tier)
    entry["rationale"] = rationale
    return entry


def assemble_weekly_report(
    week: int,
    generated_at: str,
    projection_entries: list[dict[str, Any]],
    waiver_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "week": week,
        "leagueFormatAssumption": LEAGUE_FORMAT_ASSUMPTION,
        "generatedAt": generated_at,
        "projections": projection_entries,
        "waiverTargets": waiver_entries,
    }


def assemble_player_pool(pool_entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"players": pool_entries}


def build_weekly_report_and_pool(
    season: int,
    week: int,
    scoring_config: dict[str, Any],
    pool: list[dict[str, Any]],
    schedule_games: list[dict[str, Any]],
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    news_flags_by_player: dict[str, dict[str, Any]],
    window: int = DEFAULT_WINDOW,
    generated_at: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The main assembly entry point, fully pure/injectable (all data
    already loaded by the caller) -- see the `load_*` adapters below for
    where `pool`/`schedule_games`/`game_logs_by_player`/
    `news_flags_by_player` come from in a real run.

    Args:
        pool: [{"playerId", "name", "position", "team"}, ...] -- the
            season-wide player identity list (player-pool.json's source).
        schedule_games: [{"season", "week", "home_team", "away_team"}, ...].
        game_logs_by_player: {playerId: [{"season", "week", **stat_line}]}
            -- projections.py's game_log shape, per player.
        news_flags_by_player: {playerId: newsFlag}. A player missing here
            gets DEFAULT_NEWS_FLAG, not an error -- "no news layer ran for
            this player" degrades the same way news.py itself degrades on
            a failed fetch.

    Returns:
        (weekly_report, player_pool) -- weekly_report covers only players
        whose team has a scheduled game this week (bye-week players are
        skipped, not zero-projected); player_pool covers the full
        season-wide pool regardless of this week's schedule.
    """
    candidates = []
    for player in pool:
        opponent = opponent_for_team_week(schedule_games, player["team"], season, week)
        if opponent is None:
            continue  # bye week (or a week outside the loaded schedule) -- not playable
        game_log = game_logs_by_player.get(player["playerId"], [])
        projection = project_player(game_log, scoring_config, season, week, window=window)
        news_flag = news_flags_by_player.get(player["playerId"]) or dict(DEFAULT_NEWS_FLAG)
        candidates.append(
            {
                "playerId": player["playerId"],
                "name": player["name"],
                "position": player["position"],
                "team": player["team"],
                "opponent": opponent,
                "points": projection["projected_points"],
                "games_used": projection["games_used"],
                "confidence": projection["confidence"],
                "per_game_points": projection["per_game_points"],
                "news_flag": news_flag,
            }
        )

    projection_entries = [build_projection_entry(c) for c in candidates]

    waiver_candidates = select_waiver_targets(candidates)
    waiver_entries = [build_waiver_target_entry(c, generate_rationale(c)) for c in waiver_candidates]

    weekly_report = assemble_weekly_report(week, generated_at or now_iso(), projection_entries, waiver_entries)

    pool_entries = [
        build_player_pool_entry(p, news_flags_by_player.get(p["playerId"]) or dict(DEFAULT_NEWS_FLAG))
        for p in pool
    ]
    player_pool = assemble_player_pool(pool_entries)

    return weekly_report, player_pool


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Real data adapters -- NOT exercised by the test suite (network + nflreadpy
# required, same caveat as projections.load_recent_games_nflreadpy and
# news.fetch_espn_news). Run generate_report.py directly to exercise these.
# ---------------------------------------------------------------------------


def load_player_pool_nflreadpy(season: int) -> list[dict[str, Any]]:
    """Interim player-pool source per the integration plan's orchestration
    section: real ESPN league roster access doesn't exist yet, so this
    uses nflreadpy's full active-roster snapshot as a stand-in -- covers
    the general player pool, not this specific league's rostered players."""
    import nflreadpy as nfl

    try:
        rosters = nfl.load_rosters(seasons=[season])
        df = rosters.to_pandas() if hasattr(rosters, "to_pandas") else rosters
        df = df[(df["status"] == "ACT") & (df["position"].isin(POOL_POSITIONS))]

        pool = []
        seen_ids = set()
        for _, row in df.iterrows():
            player_id = row.get("gsis_id")
            if not player_id or player_id in seen_ids:
                # A mid-season trade can leave a player with more than one
                # row in a season-level roster snapshot -- keep the first.
                continue
            seen_ids.add(player_id)
            pool.append(
                {
                    "playerId": player_id,
                    "name": row.get("full_name"),
                    "position": row["position"],
                    "team": normalize_team(row.get("team")),
                    "espnId": row.get("espn_id"),
                }
            )
        return pool
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_player_pool_nflreadpy: nflreadpy's load_rosters() response shape didn't "
            "match this adapter's assumptions (expected columns include 'gsis_id', "
            "'full_name', 'position', 'team', 'status', 'espn_id'). Check nflreadpy's "
            "actual column names for your installed version and update this function."
        ) from exc


def load_schedule_nflreadpy(season: int) -> list[dict[str, Any]]:
    import nflreadpy as nfl

    try:
        schedules = nfl.load_schedules(seasons=[season])
        df = schedules.to_pandas() if hasattr(schedules, "to_pandas") else schedules
        return [
            {
                "season": int(row["season"]),
                "week": int(row["week"]),
                "home_team": normalize_team(row.get("home_team")),
                "away_team": normalize_team(row.get("away_team")),
            }
            for _, row in df.iterrows()
        ]
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_schedule_nflreadpy: nflreadpy's load_schedules() response shape didn't "
            "match this adapter's assumptions (expected columns include 'season', 'week', "
            "'home_team', 'away_team'). Check nflreadpy's actual column names for your "
            "installed version and update this function."
        ) from exc


def load_all_game_logs_nflreadpy(season: int) -> dict[str, list[dict[str, Any]]]:
    """Bulk equivalent of projections.load_recent_games_nflreadpy: one
    load_player_stats() call for the whole season instead of one per
    player, then grouped by player_id -- generating a full weekly report
    means every player in the pool needs a game log, so a per-player
    network call each would be needlessly slow."""
    import nflreadpy as nfl

    from scoring import nflreadpy_row_to_stat_line

    try:
        stats = nfl.load_player_stats(seasons=[season])
    except ConnectionError:
        # nflverse hasn't published a stats file for this season yet --
        # confirmed for real against season=2026 before Week 1: the
        # current season's parquet 404s until games have actually been
        # played. Not a bug -- every player is a legitimate week-1
        # cold-start per projections.py's own as-of discipline ("a week 1
        # projection with no current-season games yet correctly returns
        # zero eligible games"), so degrade to "no game logs" rather than
        # crashing the whole report.
        print(f"  no nflreadpy player-stats file for season {season} yet -- treating as a full cold start.")
        return {}

    try:
        df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
        if "season_type" in df.columns:
            # Postseason weeks restart at 1 -- mixing them into a REG-season
            # game log would corrupt the as-of week ordering project_player
            # relies on.
            df = df[df["season_type"] == "REG"]

        game_logs: dict[str, list[dict[str, Any]]] = {}
        for _, row in df.iterrows():
            player_id = row.get("player_id")
            if not player_id:
                continue
            stat_line = nflreadpy_row_to_stat_line(row.to_dict())
            stat_line["season"] = season
            stat_line["week"] = int(row["week"])
            stat_line["opponent_team"] = normalize_team(row.get("opponent_team"))
            game_logs.setdefault(player_id, []).append(stat_line)
        return game_logs
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_all_game_logs_nflreadpy: nflreadpy's load_player_stats() response shape "
            "didn't match this adapter's assumptions (expected columns include 'player_id', "
            "'week', 'season_type', 'opponent_team'). Check nflreadpy's actual column names "
            "for your installed version and update this function."
        ) from exc


def build_news_client_or_none() -> Any:
    """None if ANTHROPIC_API_KEY isn't set, or if building the client
    fails for any reason -- callers treat None as "skip summarization,
    default newsFlag for everyone," per news.py's own degrade-gracefully
    contract, extended to cover the no-key case."""
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        from news import build_anthropic_client

        return build_anthropic_client()
    except Exception:
        return None


def load_news_flags_nflreadpy(pool: list[dict[str, Any]], client: Any) -> dict[str, dict[str, Any]]:
    """One fetch_espn_news() call for the whole pool, then per-player
    matching + summarization. Cost-bounded: summarize_player_news() only
    makes an LLM call for players with at least one matched article
    (articles_for_player's crude substring match is expected to miss most
    of the pool entirely, which is cheap, not a bug)."""
    from news import articles_for_player, fetch_espn_news, summarize_player_news

    articles = fetch_espn_news()
    flags: dict[str, dict[str, Any]] = {}
    for player in pool:
        player_articles = articles_for_player(articles, player["name"], player.get("espnId"))
        if not player_articles or client is None:
            flags[player["playerId"]] = dict(DEFAULT_NEWS_FLAG)
            continue
        flags[player["playerId"]] = summarize_player_news(player["name"], player_articles, client)
    return flags


def main(argv: Optional[list[str]] = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Generate weekly-report-week-N.json and player-pool.json from real data "
            "sources (nflreadpy rosters/stats/schedules + ESPN news), per "
            "docs/design/backend-frontend-integration-plan.md."
        )
    )
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--scoring-config", type=Path, default=DEFAULT_SCORING_CONFIG_PATH)
    parser.add_argument(
        "--skip-news",
        action="store_true",
        help="skip the ESPN news fetch + LLM summarization step; every player gets the default healthy newsFlag",
    )
    args = parser.parse_args(argv)

    scoring_config = json.loads(args.scoring_config.read_text())

    print(f"Loading {args.season} player pool (nflreadpy rosters)...")
    pool = load_player_pool_nflreadpy(args.season)
    print(f"  {len(pool)} active {'/'.join(POOL_POSITIONS)} players")

    print(f"Loading {args.season} schedule...")
    schedule_games = load_schedule_nflreadpy(args.season)

    print(f"Loading {args.season} game logs (nflreadpy player stats)...")
    game_logs = load_all_game_logs_nflreadpy(args.season)

    if args.skip_news:
        print("--skip-news set: every player gets the default healthy newsFlag.")
        news_flags: dict[str, dict[str, Any]] = {}
    else:
        client = build_news_client_or_none()
        if client is None:
            print("ANTHROPIC_API_KEY not set -- news summarization skipped, default newsFlag for everyone.")
        else:
            print("Fetching + summarizing ESPN news...")
        news_flags = load_news_flags_nflreadpy(pool, client)

    weekly_report, player_pool = build_weekly_report_and_pool(
        args.season,
        args.week,
        scoring_config,
        pool,
        schedule_games,
        game_logs,
        news_flags,
        window=args.window,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.out_dir / f"weekly-report-week-{args.week}.json"
    pool_path = args.out_dir / "player-pool.json"
    report_path.write_text(json.dumps(weekly_report, indent=2) + "\n")
    pool_path.write_text(json.dumps(player_pool, indent=2) + "\n")

    print(
        f"Wrote {report_path} "
        f"({len(weekly_report['projections'])} projections, {len(weekly_report['waiverTargets'])} waiver targets)"
    )
    print(f"Wrote {pool_path} ({len(player_pool['players'])} players)")


if __name__ == "__main__":
    main()
