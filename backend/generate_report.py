"""
Orchestration script: wires scoring.py + projections.py + fantasypros.py +
news.py + waiver_targets.py together and writes weekly-report-week-N.json
and player-pool.json in the exact shape frontend/src/lib/api.js already
expects -- the "generation script, not a live API" architecture from
docs/design/backend-frontend-integration-plan.md.

Follows that plan's v0 build order, with one update since the plan was
written: real player IDs (nflreadpy's gsis_id) as `playerId` directly, no
invented p_00123-style scheme and no permanent crosswalk table (the plan's
recommended resolution to its own "player-ID gap" open question -- this
project has no real users with saved localStorage data yet to migrate).
Gap #2 -- an actual FantasyPros pull and top-10-per-position tier
selection, originally deferred per the plan's v0 scoping ("ship 100%
in-house first") -- is now wired in via fantasypros.py (CLAUDE.md v8 Next
Steps item 3): a player who resolves to FantasyPros' top-10-for-position
consensus tier gets that projection; everyone else still gets the
in-house estimate. Waiver targets still use waiver_targets.py's templated
(non-LLM) rationale, per the plan's suggested v0 shortcut.

DST and K are now covered too (2026-08-12, closing CLAUDE.md Next Steps
item 3 -- see dst.py and kicker.py), both in the in_house_estimate tier
only: FantasyPros' free-tier consensus pull (fantasypros.py) stays scoped
to its confirmed QB/RB/WR/TE top-10-per-position coverage, not extended to
DST/K here (a separate, unscoped decision -- see backend/README.md).
projections.py's rolling-average/shrinkage model is wired through
unmodified for DST/K (no new modeling logic), but it was only backtested
against QB/RB/WR/TE (docs/research/projection-model-backtest-findings.md)
-- DST/K projections haven't been through that same rigor, an honest scope
note, not a blocker. A DST's `playerId` is its team abbreviation (e.g.
"BUF"), not a gsis_id -- a DST isn't a per-player entity, and a real,
stable ID already exists for it (see load_dst_pool_nflreadpy below and
dst.py's own docstring). A K's `playerId` is a real gsis_id, same
mechanism QB/RB/WR/TE already use.

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
from projections import DEFAULT_WINDOW, POSITION_CALIBRATION_SCALE, project_player
from waiver_targets import generate_rationale, select_waiver_targets

# nflreadpy (2025 season, confirmed hands-on) uses "LA" for the Rams; the
# frontend's mock fixtures and the rest of this project's docs use "LAR".
# One-entry map so this doesn't need to grow into a full alias table for a
# single known mismatch.
TEAM_ABBR_DISPLAY_MAP = {"LA": "LAR"}

# The positions projections.py's model was actually backtested against
# (docs/research/projection-model-backtest-findings.md) and the scope of
# fantasypros.py's consensus tier (fantasypros.POSITIONS mirrors this).
# Kept separate from ROSTER_POSITIONS below since K resolves to a real
# player via load_rosters() the same way these four do, but is excluded
# from the backtest/FantasyPros scope both still describe.
POOL_POSITIONS = ("QB", "RB", "WR", "TE")

KICKER_POSITION = "K"

# Positions resolvable via nflreadpy's load_rosters() with a real gsis_id
# -- K joins QB/RB/WR/TE here since it's a per-player entity too, just not
# part of POOL_POSITIONS' backtest/FantasyPros scope. DST is NOT in this
# list -- it isn't a per-player roster entity at all, see
# load_dst_pool_nflreadpy below.
ROSTER_POSITIONS = POOL_POSITIONS + (KICKER_POSITION,)

TIER_METADATA = {
    "consensus": {"tierLabel": "Consensus projection", "source": "FantasyPros"},
    "in_house_estimate": {"tierLabel": "Our estimate", "source": "In-house model"},
}

# Updated 2026-08-16 once real league values landed: reception=1 in
# leagues/league-1/scoring-config.json (was the half_ppr placeholder's 0.5) --
# this is a full-PPR league, confirmed hands-on from the real ESPN
# settings, not a guess. "ppr" is already a recognized key in the
# frontend's WeeklyReportView.jsx FORMAT_LABEL map (renders as "PPR"),
# so this is the only code change needed -- no frontend edit required.
# Kept as a module-level constant for backward compatibility; in the
# multi-league path (--leagues-config), each league's leagueFormat from
# leagues.json is passed explicitly and this constant is not used.
LEAGUE_FORMAT_ASSUMPTION = "ppr"

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public" / "mock"
DEFAULT_SCORING_CONFIG_PATH = Path(__file__).resolve().parent / "leagues" / "league-1" / "scoring-config.json"


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
    league_id: str = "league-1",
    league_format: str = "ppr",
) -> dict[str, Any]:
    """Assemble the weekly-report JSON dict.

    Args:
        league_id: stable string identifier for the league (keys output
            directories and localStorage). Defaults to "league-1" so callers
            that don't pass the kwarg get the same behavior as before.
        league_format: one of "ppr", "half_ppr", "standard". Passed through
            from the manifest's leagueFormat in the multi-league path so
            the report's leagueFormatAssumption field reflects the real
            per-league setting rather than the module-level constant.
    """
    return {
        "week": week,
        "leagueId": league_id,
        "leagueFormatAssumption": league_format,
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
    consensus_projections: Optional[dict[str, dict[str, Any]]] = None,
    league_id: str = "league-1",
    league_format: str = "ppr",
    rostered_rank_cutoff: Optional[dict[str, int]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The main assembly entry point, fully pure/injectable (all data
    already loaded by the caller) -- see the `load_*` adapters below for
    where `pool`/`schedule_games`/`game_logs_by_player`/
    `news_flags_by_player`/`consensus_projections` come from in a real run.

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
        consensus_projections: {playerId: {"projected_points", ...}} --
            fantasypros.build_consensus_tier's output. A player present
            here gets the FantasyPros consensus tier instead of the
            in-house estimate; a player absent (the common case -- this
            is only ever FantasyPros' top 10 per position) falls back to
            the in-house tier exactly as before this parameter existed.
            None/omitted (the default) means nobody gets the consensus
            tier -- same "off means arithmetically identical to before"
            contract as project_player's own optional parameters.
        league_id: stable string identifier passed through to
            assemble_weekly_report; appears as "leagueId" in the report
            JSON so the track-record layer and frontend can identify which
            league a file belongs to without depending on its directory
            path. Defaults to "league-1" for backward compatibility.
        league_format: one of "ppr", "half_ppr", "standard"; passed
            through to assemble_weekly_report as leagueFormatAssumption.
            In the multi-league path this comes from the manifest's
            leagueFormat field, not the module-level constant.
        rostered_rank_cutoff: per-position dict passed to
            select_waiver_targets to define the boundary between "rostered"
            and "waiver-eligible" players. None (the default) falls back to
            waiver_targets.py's DEFAULT_ROSTERED_RANK_CUTOFF (the 14-team
            default). The multi-league caller derives this from teamCount.

    Returns:
        (weekly_report, player_pool) -- weekly_report covers only players
        whose team has a scheduled game this week (bye-week players are
        skipped, not zero-projected); player_pool covers the full
        season-wide pool regardless of this week's schedule.
    """
    consensus_projections = consensus_projections or {}
    candidates = []
    for player in pool:
        opponent = opponent_for_team_week(schedule_games, player["team"], season, week)
        if opponent is None:
            continue  # bye week (or a week outside the loaded schedule) -- not playable
        news_flag = news_flags_by_player.get(player["playerId"]) or dict(DEFAULT_NEWS_FLAG)
        consensus = consensus_projections.get(player["playerId"])
        if consensus is not None:
            candidate = {
                "playerId": player["playerId"],
                "name": player["name"],
                "position": player["position"],
                "team": player["team"],
                "opponent": opponent,
                "points": consensus["projected_points"],
                "tier": "consensus",
                # Not meaningful for the consensus tier (FantasyPros doesn't
                # expose a game-by-game history, only the projection) --
                # harmless placeholders, not read by build_projection_entry.
                # waiver_targets.py never sees these values in practice:
                # its rostered-rank cutoff (>=14 per position) always
                # exceeds FantasyPros' top-10-per-position depth, so a
                # consensus-tier player is never in the waiver-eligible
                # pool to begin with.
                "games_used": 0,
                "confidence": None,
                "per_game_points": [],
                "news_flag": news_flag,
            }
        else:
            game_log = game_logs_by_player.get(player["playerId"], [])
            cal_scale = POSITION_CALIBRATION_SCALE.get(player["position"], 1.0)
            projection = project_player(
                game_log, scoring_config, season, week,
                window=window, calibration_scale=cal_scale,
            )
            candidate = {
                "playerId": player["playerId"],
                "name": player["name"],
                "position": player["position"],
                "team": player["team"],
                "opponent": opponent,
                "points": projection["projected_points"],
                "tier": "in_house_estimate",
                "games_used": projection["games_used"],
                "confidence": projection["confidence"],
                "per_game_points": projection["per_game_points"],
                "news_flag": news_flag,
            }
        candidates.append(candidate)

    projection_entries = [build_projection_entry(c, c["tier"]) for c in candidates]

    # Pass rostered_rank_cutoff through only when explicitly provided; None
    # means "use waiver_targets.py's own DEFAULT_ROSTERED_RANK_CUTOFF" so
    # the single-league code path is arithmetically identical to before.
    if rostered_rank_cutoff is not None:
        waiver_candidates = select_waiver_targets(candidates, rostered_rank_cutoff=rostered_rank_cutoff)
    else:
        waiver_candidates = select_waiver_targets(candidates)
    waiver_entries = [build_waiver_target_entry(c, generate_rationale(c), c["tier"]) for c in waiver_candidates]

    weekly_report = assemble_weekly_report(
        week, generated_at or now_iso(), projection_entries, waiver_entries,
        league_id=league_id, league_format=league_format,
    )

    pool_entries = [
        build_player_pool_entry(p, news_flags_by_player.get(p["playerId"]) or dict(DEFAULT_NEWS_FLAG))
        for p in pool
    ]
    player_pool = assemble_player_pool(pool_entries)

    return weekly_report, player_pool


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _update_manifest(manifest_path: "Path", week: int) -> None:
    """Write/update manifest.json for a league's fixture directory.

    Tracks the set of generated weeks and the latest one so the frontend can
    auto-select the most recent report without a hardcoded constant.
    """
    from pathlib import Path as _Path

    existing: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    weeks: list[int] = existing.get("weeks", [])
    if week not in weeks:
        weeks.append(week)
    weeks.sort()
    manifest = {"latestWeek": weeks[-1], "weeks": weeks}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Real data adapters -- NOT exercised by the test suite (network + nflreadpy
# required, same caveat as projections.load_recent_games_nflreadpy and
# news.fetch_espn_news). Run generate_report.py directly to exercise these.
# ---------------------------------------------------------------------------


def load_player_pool_nflreadpy(season: int) -> list[dict[str, Any]]:
    """Interim player-pool source per the integration plan's orchestration
    section: real ESPN league roster access doesn't exist yet, so this
    uses nflreadpy's full active-roster snapshot as a stand-in -- covers
    the general player pool, not this specific league's rostered players.

    Covers ROSTER_POSITIONS (QB/RB/WR/TE/K), not just POOL_POSITIONS --
    kickers resolve to a real gsis_id via this exact same roster snapshot
    (confirmed hands-on 2026-08-12: 33 real active 2025 kickers, e.g.
    Chris Boswell -> 00-0031136), so there's no reason to give them a
    separate pool-loading path the way DST needs (see
    load_dst_pool_nflreadpy, which is NOT sourced from this function --
    a DST is a team, not a roster entry)."""
    import nflreadpy as nfl

    try:
        rosters = nfl.load_rosters(seasons=[season])
        df = rosters.to_pandas() if hasattr(rosters, "to_pandas") else rosters
        # Include all 53-man rostered players, not just active — IR, inactive,
        # PUP, etc. should still appear in the report with their injury status
        # visible rather than silently vanishing from every list. Practice
        # squad players (TRC) are excluded because they're not eligible to
        # play or be rostered in most fantasy formats.
        PRACTICE_SQUAD_STATUSES = {"TRC", "PS"}
        df = df[~df["status"].isin(PRACTICE_SQUAD_STATUSES) & df["position"].isin(ROSTER_POSITIONS)]

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
    network call each would be needlessly slow.

    Merges scoring.nflreadpy_row_to_stat_line (offense) with
    kicker.nflreadpy_kicker_row_to_stat_line (K) on every row rather than
    branching on position: a QB/RB/WR/TE row has none of the kicker
    columns and a K row has none of the offense columns, and the two
    modules' output category names are confirmed disjoint (see
    tests/test_kicker.py), so the union is always exactly the row's real
    stat line either way -- no position check needed. This function
    already iterates every position in load_player_stats(), not just
    ROSTER_POSITIONS, so K rows were already reaching this loop before
    today; they just produced an empty stat_line (silently unused, since
    K wasn't in the pool) until this merge."""
    import nflreadpy as nfl

    from kicker import nflreadpy_kicker_row_to_stat_line
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
            row_dict = row.to_dict()
            stat_line = {**nflreadpy_row_to_stat_line(row_dict), **nflreadpy_kicker_row_to_stat_line(row_dict)}
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


def load_dst_pool_and_game_logs_nflreadpy(season: int) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """DST equivalent of load_player_pool_nflreadpy + load_all_game_logs_nflreadpy
    combined into one adapter, since dst.py's pool and game-log sources
    (load_schedules(), load_teams(), load_team_stats()) are distinct calls
    from the player-roster/player-stats ones those two functions use, and
    a DST's game log needs the pool's own team list anyway to know which
    teams to build for.

    Same season-not-published-yet degrade-gracefully contract as
    load_all_game_logs_nflreadpy: nflverse's team-stats file 404s
    (ConnectionError) exactly when its player-stats file does, for the
    same reason (no games played yet this season) -- the DST pool itself
    still loads fine (it only needs the schedule + team list, both
    published pre-season), it's only the game logs that go empty, giving
    every DST the same week-1 cold start every offensive player already
    gets in that scenario."""
    from dst import build_dst_game_logs, load_dst_pool_nflreadpy, load_schedule_with_scores_nflreadpy, load_team_stats_nflreadpy

    pool = load_dst_pool_nflreadpy(season)
    try:
        team_stats_rows = load_team_stats_nflreadpy(season)
    except ConnectionError:
        print(f"  no nflreadpy team-stats file for season {season} yet -- DST treated as a full cold start too.")
        return pool, {}

    schedule_games = load_schedule_with_scores_nflreadpy(season)
    game_logs = build_dst_game_logs(team_stats_rows, schedule_games)
    return pool, game_logs


def build_consensus_tier_or_empty(season: int, week: int, scoring_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """FANTASYPROS_API_KEY missing, or any part of the fetch/crosswalk
    pipeline failing, degrades to an empty consensus tier -- every player
    falls back to the in-house estimate. Same "skip cleanly" contract
    build_news_client_or_none already establishes for the news layer,
    extended to cover network/API failures too, not just a missing key --
    a real, if unlikely, way for this to fail on the day of an actual
    weekly-report run, since the whole point of running this script is
    getting *a* report out, not blocking on one vendor's tier."""
    import os

    from fantasypros import build_consensus_tier, fetch_consensus_tier, load_fantasypros_id_crosswalk_nflreadpy

    api_key = os.environ.get("FANTASYPROS_API_KEY")
    if not api_key:
        return {}
    try:
        raw_players_by_position = fetch_consensus_tier(season, week, api_key)
        crosswalk = load_fantasypros_id_crosswalk_nflreadpy()
        return build_consensus_tier(raw_players_by_position, scoring_config, crosswalk)
    except Exception as exc:  # noqa: BLE001 -- degrade gracefully, don't block the report
        print(f"  FantasyPros consensus tier fetch failed ({exc}) -- falling back to in-house estimate for everyone.")
        return {}


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
    import os

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
    parser.add_argument(
        "--skip-news",
        action="store_true",
        help="skip the ESPN news fetch + LLM summarization step; every player gets the default healthy newsFlag",
    )
    parser.add_argument(
        "--skip-fantasypros",
        action="store_true",
        help="skip the FantasyPros consensus-tier pull; every player gets the in-house estimate tier",
    )
    # --leagues-config and --scoring-config are mutually exclusive: passing
    # both is an argparse error. When neither is provided, --scoring-config
    # defaults to DEFAULT_SCORING_CONFIG_PATH (single-league path, same
    # behavior as before this argument existed).
    config_group = parser.add_mutually_exclusive_group()
    config_group.add_argument(
        "--leagues-config",
        type=Path,
        default=None,
        help=(
            "path to leagues.json manifest; when set, generates one report per "
            "league defined in the manifest. Mutually exclusive with --scoring-config."
        ),
    )
    config_group.add_argument(
        "--scoring-config",
        type=Path,
        default=DEFAULT_SCORING_CONFIG_PATH,
        help=(
            "path to a single-league scoring config JSON. "
            "Mutually exclusive with --leagues-config."
        ),
    )
    args = parser.parse_args(argv)

    # ---------------------------------------------------------------------------
    # Shared data loading -- happens once regardless of single vs multi-league.
    # Scoring-config-dependent steps (consensus-tier scoring) happen per-league.
    # ---------------------------------------------------------------------------

    print(f"Loading {args.season} player pool (nflreadpy rosters)...")
    pool = load_player_pool_nflreadpy(args.season)
    print(f"  {len(pool)} active {'/'.join(ROSTER_POSITIONS)} players")

    print(f"Loading {args.season} DST pool (nflreadpy schedules/teams)...")
    dst_pool, dst_game_logs = load_dst_pool_and_game_logs_nflreadpy(args.season)
    print(f"  {len(dst_pool)} DSTs, {len(dst_game_logs)} with at least one game logged")
    pool = pool + dst_pool

    print(f"Loading {args.season} schedule...")
    schedule_games = load_schedule_nflreadpy(args.season)

    print(f"Loading {args.season} game logs (nflreadpy player stats)...")
    game_logs = load_all_game_logs_nflreadpy(args.season)
    game_logs.update(dst_game_logs)  # DST keyed by team abbreviation, players by gsis_id -- no collision

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

    # Fetch raw FantasyPros data once (network call); scoring is applied per-
    # league below so each league's projected_points reflect its own config.
    raw_fp_players_by_position: Optional[dict[str, Any]] = None
    fp_crosswalk: Optional[dict[str, Any]] = None
    if args.skip_fantasypros:
        print("--skip-fantasypros set: every player gets the in-house estimate tier.")
    elif os.environ.get("FANTASYPROS_API_KEY") is None:
        print("FANTASYPROS_API_KEY not set -- consensus tier skipped, in-house estimate for everyone.")
    else:
        print(f"Fetching FantasyPros consensus tier ({'/'.join(POOL_POSITIONS)})...")
        try:
            from fantasypros import fetch_consensus_tier, load_fantasypros_id_crosswalk_nflreadpy

            raw_fp_players_by_position = fetch_consensus_tier(
                args.season, args.week, os.environ["FANTASYPROS_API_KEY"]
            )
            fp_crosswalk = load_fantasypros_id_crosswalk_nflreadpy()
        except Exception as exc:  # noqa: BLE001
            print(f"  FantasyPros fetch failed ({exc}) -- falling back to in-house estimate for everyone.")
            raw_fp_players_by_position = None
            fp_crosswalk = None

    def _build_consensus_for_config(scoring_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Apply the given scoring config to the already-fetched raw FantasyPros
        data. Returns an empty dict (all in-house) if the raw fetch didn't run
        or failed."""
        if raw_fp_players_by_position is None:
            return {}
        try:
            from fantasypros import build_consensus_tier

            result = build_consensus_tier(raw_fp_players_by_position, scoring_config, fp_crosswalk)
            print(f"  {len(result)} players resolved to the consensus tier")
            return result
        except Exception as exc:  # noqa: BLE001
            print(f"  FantasyPros consensus scoring failed ({exc}) -- falling back to in-house for this league.")
            return {}

    # ---------------------------------------------------------------------------
    # Multi-league path: load manifest, loop over leagues.
    # ---------------------------------------------------------------------------

    if args.leagues_config:
        leagues_manifest = json.loads(args.leagues_config.read_text())
        backend_dir = Path(__file__).resolve().parent

        for league in leagues_manifest["leagues"]:
            league_id: str = league["leagueId"]
            league_format: str = league["leagueFormat"]
            team_count: int = league["teamCount"]

            scoring_config_path = backend_dir / league["scoringConfigPath"]
            scoring_config = json.loads(scoring_config_path.read_text())

            if scoring_config.get("_PLACEHOLDER"):
                print(
                    f"WARNING: {league_id} scoring config is still a placeholder -- "
                    "projections will be inaccurate until real values are entered"
                )

            # Per-position waiver cutoff derived from team count -- reproduces
            # DEFAULT_ROSTERED_RANK_CUTOFF's formula (see waiver_targets.py).
            rostered_rank_cutoff = {
                "QB": team_count,
                "RB": team_count * 2,
                "WR": team_count * 2,
                "TE": team_count,
                "DST": team_count,
                "K": team_count,
            }

            print(f"\n--- Generating report for {league_id} ({league['displayName']}) ---")
            consensus_projections = _build_consensus_for_config(scoring_config)

            out_dir = backend_dir / league["outDir"]
            out_dir.mkdir(parents=True, exist_ok=True)

            weekly_report, player_pool = build_weekly_report_and_pool(
                args.season,
                args.week,
                scoring_config,
                pool,
                schedule_games,
                game_logs,
                news_flags,
                window=args.window,
                consensus_projections=consensus_projections,
                league_id=league_id,
                league_format=league_format,
                rostered_rank_cutoff=rostered_rank_cutoff,
            )

            report_path = out_dir / f"weekly-report-week-{args.week}.json"
            pool_path = out_dir / "player-pool.json"
            report_path.write_text(json.dumps(weekly_report, indent=2) + "\n")
            pool_path.write_text(json.dumps(player_pool, indent=2) + "\n")
            manifest_path = out_dir / "manifest.json"
            _update_manifest(manifest_path, args.week)
            print(
                f"Wrote {report_path} "
                f"({len(weekly_report['projections'])} projections, {len(weekly_report['waiverTargets'])} waiver targets)"
            )
            print(f"Wrote {pool_path} ({len(player_pool['players'])} players)")

    # ---------------------------------------------------------------------------
    # Single-league path: backward-compatible behavior, unchanged.
    # ---------------------------------------------------------------------------

    else:
        scoring_config = json.loads(args.scoring_config.read_text())
        consensus_projections = _build_consensus_for_config(scoring_config)

        weekly_report, player_pool = build_weekly_report_and_pool(
            args.season,
            args.week,
            scoring_config,
            pool,
            schedule_games,
            game_logs,
            news_flags,
            window=args.window,
            consensus_projections=consensus_projections,
        )

        args.out_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.out_dir / f"weekly-report-week-{args.week}.json"
        pool_path = args.out_dir / "player-pool.json"
        report_path.write_text(json.dumps(weekly_report, indent=2) + "\n")
        pool_path.write_text(json.dumps(player_pool, indent=2) + "\n")
        manifest_path = args.out_dir / "manifest.json"
        _update_manifest(manifest_path, args.week)

        print(
            f"Wrote {report_path} "
            f"({len(weekly_report['projections'])} projections, {len(weekly_report['waiverTargets'])} waiver targets)"
        )
        print(f"Wrote {pool_path} ({len(player_pool['players'])} players)")


if __name__ == "__main__":
    main()
