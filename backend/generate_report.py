"""Builds the weekly-report and player-pool fixtures. See ARCHITECTURE.md §10."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import entity_prior
from news import DEFAULT_NEWS_FLAG
from blend import VOLUME_COLUMNS
from blend import build_feature_row as blend_build_feature_row
from blend import rolling_volume
from calibration import DEFAULT_SHRINKAGE_K, apply_affine
from projections import (
    DEFAULT_DECAY,
    DEFAULT_WINDOW,
    POSITION_CALIBRATION_SCALE,
    project_player,
)
from waiver_targets import generate_rationale, select_waiver_targets

TEAM_ABBR_DISPLAY_MAP = {"LA": "LAR"}

POOL_POSITIONS = ("QB", "RB", "WR", "TE")

KICKER_POSITION = "K"

ROSTER_POSITIONS = POOL_POSITIONS + (KICKER_POSITION,)

TIER_METADATA = {
    "consensus": {"tierLabel": "Consensus projection", "source": "FantasyPros + Rotowire"},
    "in_house_estimate": {"tierLabel": "Our estimate", "source": "In-house model"},
    "week1_model": {"tierLabel": "Week 1 estimate", "source": "In-house model (pre-season)"},
}

LEAGUE_FORMAT_ASSUMPTION = "ppr"

WEEK1_TRAIN_SEASONS = 4

KDST_HISTORY_SEASONS = 6

AVAILABILITY_TRAIN_SEASONS = 6

CALIBRATION_TRAIN_SEASONS = 3

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public" / "mock"
DEFAULT_SCORING_CONFIG_PATH = Path(__file__).resolve().parent / "leagues" / "league-1" / "scoring-config.json"


def normalize_team(team: Optional[str]) -> Optional[str]:
    if team is None:
        return None
    return TEAM_ABBR_DISPLAY_MAP.get(team, team)


def opponent_for_team_week(
    schedule_games: list[dict[str, Any]], team: str, season: int, week: int
) -> Optional[str]:
    """The other team in this team's game for (season, week), or None on a bye."""
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


def _store_projection_rows(weekly_report: dict[str, Any]) -> list[dict[str, Any]]:
    """Weekly-report JSON -> store `projections` rows (points = expected value)."""
    rows = []
    for entry in weekly_report.get("projections", []):
        projection = entry.get("projection") or {}
        rows.append({
            "player_id": entry.get("playerId"),
            "tier": projection.get("tier"),
            "points": projection.get("points"),
            "conditional_points": projection.get("conditionalPoints"),
            "play_probability": projection.get("playProbability"),
            "floor": projection.get("floor"),
            "ceiling": projection.get("ceiling"),
        })
    return rows


def _mixture_band(
    interval_model: Optional[Any],
    position: str,
    conditional_points: float,
    play_probability: Optional[float],
    fallback: tuple[Optional[float], Optional[float]] = (None, None),
) -> dict[str, Optional[float]]:
    """{floor, ceiling} for a projection, accounting for the chance he does not play."""
    if interval_model is not None:
        band = interval_model.interval(
            position, conditional_points, play_probability=play_probability
        )
        if band is not None:
            return {"floor": round(band[0], 2), "ceiling": round(band[1], 2)}
    lo, hi = fallback
    return {
        "floor": None if lo is None else round(lo, 2),
        "ceiling": None if hi is None else round(hi, 2),
    }


def _consensus_source_label(consensus: dict[str, Any]) -> Optional[str]:
    """Which feeds produced this consensus projection, or None for the tier default."""
    try:
        from rotowire_projections import consensus_source_label

        return consensus_source_label(consensus)
    except Exception:  # noqa: BLE001
        return None


def _projection_object(
    points: float,
    tier: str,
    floor: Optional[float] = None,
    ceiling: Optional[float] = None,
    play_probability: Optional[float] = None,
    conditional_points: Optional[float] = None,
    source: Optional[str] = None,
) -> dict[str, Any]:
    """The public projection shape (`points` is the expected value)."""
    meta = TIER_METADATA[tier]
    projection: dict[str, Any] = {
        "tier": tier,
        "points": points,
        "tierLabel": meta["tierLabel"],
        "source": source or meta["source"],
    }
    if floor is not None and ceiling is not None:
        projection["floor"] = floor
        projection["ceiling"] = ceiling
    if play_probability is not None:
        projection["playProbability"] = play_probability
        if conditional_points is not None:
            projection["conditionalPoints"] = conditional_points
    return projection


def build_projection_entry(candidate: dict[str, Any], tier: str = "in_house_estimate") -> dict[str, Any]:
    """Reshape an internal candidate into a public `projections[]` entry."""
    return {
        "playerId": candidate["playerId"],
        "name": candidate["name"],
        "position": candidate["position"],
        "team": candidate["team"],
        "opponent": candidate["opponent"],
        "projection": _projection_object(
            candidate["points"],
            tier,
            floor=candidate.get("floor"),
            ceiling=candidate.get("ceiling"),
            play_probability=candidate.get("play_probability"),
            conditional_points=candidate.get("conditional_points"),
            source=candidate.get("source"),
        ),
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
    """Assemble the weekly-report JSON dict."""
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
    rz_stats_by_player: Optional[dict[str, dict[str, Any]]] = None,
    week1_projections: Optional[dict[str, dict[str, Any]]] = None,
    play_probabilities: Optional[dict[str, float]] = None,
    positional_baselines: Optional[dict[str, float]] = None,
    entity_baselines: Optional[dict[str, float]] = None,
    shrinkage_ks: Optional[dict[str, float]] = None,
    affines: Optional[dict[str, tuple[float, float]]] = None,
    interval_model: Optional[Any] = None,
    blend_model: Optional[Any] = None,
    game_context: Optional[dict[str, dict[str, Any]]] = None,
    decay: float = DEFAULT_DECAY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Main pure assembly: all data injected; returns (weekly_report, player_pool)."""
    consensus_projections = consensus_projections or {}
    candidates = []
    blend_rows: list[dict[str, Any]] = []
    for player in pool:
        opponent = opponent_for_team_week(schedule_games, player["team"], season, week)
        if opponent is None:
            continue
        news_flag = news_flags_by_player.get(player["playerId"]) or dict(DEFAULT_NEWS_FLAG)
        consensus = consensus_projections.get(player["playerId"])
        if consensus is not None:
            candidate = {
                "playerId": player["playerId"],
                "name": player["name"],
                "position": player["position"],
                "team": player["team"],
                "opponent": opponent,
                "points": round(
                    consensus["projected_points"]
                    * ((play_probabilities or {}).get(player["playerId"], 1.0)),
                    2,
                ),
                "conditional_points": consensus["projected_points"],
                "play_probability": (
                    None
                    if (play_probabilities or {}).get(player["playerId"]) is None
                    else round(play_probabilities[player["playerId"]], 3)
                ),
                **_mixture_band(
                    interval_model,
                    player["position"],
                    consensus["projected_points"],
                    (play_probabilities or {}).get(player["playerId"]),
                ),
                "tier": "consensus",
                "source": _consensus_source_label(consensus),
                "games_used": 0,
                "confidence": None,
                "per_game_points": [],
                "news_flag": news_flag,
            }
        elif (week1_projections or {}).get(player["playerId"]) is not None:
            week1_projection = week1_projections[player["playerId"]]
            candidate = {
                "playerId": player["playerId"],
                "name": player["name"],
                "position": player["position"],
                "team": player["team"],
                "opponent": opponent,
                "points": round(
                    week1_projection["projected_points"]
                    * ((play_probabilities or {}).get(player["playerId"], 1.0)),
                    2,
                ),
                "conditional_points": week1_projection["projected_points"],
                **_mixture_band(
                    week1_projection.get("_interval_model"),
                    player["position"],
                    week1_projection["projected_points"],
                    (play_probabilities or {}).get(player["playerId"]),
                    fallback=(week1_projection.get("floor"), week1_projection.get("ceiling")),
                ),
                "play_probability": (
                    None
                    if (play_probabilities or {}).get(player["playerId"]) is None
                    else round(play_probabilities[player["playerId"]], 3)
                ),
                "tier": "week1_model",
                "games_used": 0,
                "confidence": week1_projection.get("confidence"),
                "per_game_points": [],
                "news_flag": news_flag,
            }
        else:
            player_id = player["playerId"]
            game_log = game_logs_by_player.get(player_id, [])
            cal_scale = POSITION_CALIBRATION_SCALE.get(player["position"], 1.0)
            projection = project_player(
                game_log, scoring_config, season, week,
                window=window, decay=decay, calibration_scale=cal_scale,
                positional_baseline=(
                    (entity_baselines or {}).get(player_id)
                    if (entity_baselines or {}).get(player_id) is not None
                    else (positional_baselines or {}).get(player["position"])
                ),
                # Fall back to the position's k, not the skill-position average (K needs 30).
                shrinkage_k=(shrinkage_ks or {}).get(
                    player_id, DEFAULT_SHRINKAGE_K.get(player["position"])
                ),
                play_probability=(play_probabilities or {}).get(player_id),
                affine=(affines or {}).get(player_id),
            )
            points = projection["conditional_points"]
            if blend_model is not None:
                volume = rolling_volume(game_log, season, week, window, decay)
                context = (game_context or {}).get(player["team"])
                blend_row = blend_build_feature_row(
                    player_id, player["position"], projection, volume, context
                )
                blend_rows.append(blend_row)
                points = blend_model.predict_one(blend_row)
                points = apply_affine(points, (affines or {}).get(player_id))

            probability = (play_probabilities or {}).get(player_id)
            expected = points if probability is None else points * probability

            floor = ceiling = None
            if interval_model is not None:
                band = interval_model.interval(
                    player["position"], points, play_probability=probability
                )
                if band is not None:
                    lo, hi = band
                    floor, ceiling = round(lo, 2), round(hi, 2)

            candidate = {
                "playerId": player_id,
                "name": player["name"],
                "position": player["position"],
                "team": player["team"],
                "opponent": opponent,
                "points": round(expected, 2),
                "conditional_points": round(points, 2),
                "play_probability": None if probability is None else round(probability, 3),
                "floor": floor,
                "ceiling": ceiling,
                "tier": "in_house_estimate",
                "games_used": projection["games_used"],
                "confidence": projection["confidence"],
                "per_game_points": projection["per_game_points"],
                "news_flag": news_flag,
            }
        candidates.append(candidate)

    if blend_model is not None:
        blend_model.check_coverage(blend_rows)

    projection_entries = [build_projection_entry(c, c["tier"]) for c in candidates]

    if rostered_rank_cutoff is not None:
        waiver_candidates = select_waiver_targets(candidates, rostered_rank_cutoff=rostered_rank_cutoff)
    else:
        waiver_candidates = select_waiver_targets(candidates)
    _rz = rz_stats_by_player or {}
    waiver_entries = [
        build_waiver_target_entry(
            c,
            generate_rationale(c, rz_stats=_rz.get(c["playerId"])),
            c["tier"],
        )
        for c in waiver_candidates
    ]

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


def _update_manifest(
    manifest_path: "Path",
    week: int,
    current_week: Optional[int] = None,
    team_count: Optional[int] = None,
) -> None:
    """Write/update a league's manifest.json (weeks, latestWeek, currentWeek, teamCount)."""
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
    manifest: dict[str, Any] = {"latestWeek": weeks[-1], "weeks": weeks}
    resolved = current_week if current_week is not None else existing.get("currentWeek")
    if resolved is not None:
        manifest["currentWeek"] = resolved
    teams = team_count if team_count is not None else existing.get("teamCount")
    if teams is not None:
        manifest["teamCount"] = teams
    manifest_path.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")


def load_player_pool_nflreadpy(season: int) -> list[dict[str, Any]]:
    """QB/RB/WR/TE/K pool from nflreadpy's current roster snapshot."""
    import nflreadpy as nfl

    try:
        rosters = nfl.load_rosters(seasons=[season])
        df = rosters.to_pandas() if hasattr(rosters, "to_pandas") else rosters
        PRACTICE_SQUAD_STATUSES = {"TRC", "PS"}
        df = df[~df["status"].isin(PRACTICE_SQUAD_STATUSES) & df["position"].isin(ROSTER_POSITIONS)]

        pool = []
        seen_ids = set()
        for _, row in df.iterrows():
            player_id = row.get("gsis_id")
            # A missing gsis_id arrives as NaN (truthy); require a non-empty string.
            if not isinstance(player_id, str) or not player_id.strip() or player_id in seen_ids:
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
    """Every player's regular-season game log from one load_player_stats() call."""
    import nflreadpy as nfl

    from kicker import nflreadpy_kicker_row_to_stat_line
    from scoring import nflreadpy_row_to_stat_line

    try:
        stats = nfl.load_player_stats(seasons=[season])
    except ConnectionError:
        print(f"  no nflreadpy player-stats file for season {season} yet -- treating as a full cold start.")
        return {}

    try:
        df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
        if "season_type" in df.columns:
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
            # Required: the blend trains on these columns and imputes them all if absent.
            for column in VOLUME_COLUMNS:
                if column in row_dict:
                    stat_line[column] = row_dict[column]
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
    """The D/ST pool and game logs, from schedules and team stats."""
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


def validate_scoring_config(scoring_config: dict[str, Any], league_id: str) -> None:
    """Run every config-shape validator before a single point is computed."""
    from dst import validate_dst_td_categories
    from kicker import validate_fg_band_family, validate_fg_miss_band_family

    linear = scoring_config.get("linear", {})
    try:
        validate_fg_band_family(linear)
        validate_fg_miss_band_family(linear)
        validate_dst_td_categories(linear)
    except ValueError as exc:
        raise ValueError(f"{league_id}: {exc}") from exc


def build_consensus_tier_or_empty(season: int, week: int, scoring_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The consensus tier, or {} when the key is missing or anything fails."""
    import os

    from fantasypros import build_consensus_tier, fetch_consensus_tier, load_fantasypros_id_crosswalk_nflreadpy

    api_key = os.environ.get("FANTASYPROS_API_KEY")
    if not api_key:
        return {}
    try:
        raw_players_by_position = fetch_consensus_tier(season, week, api_key)
        crosswalk = load_fantasypros_id_crosswalk_nflreadpy()
        return build_consensus_tier(raw_players_by_position, scoring_config, crosswalk)
    except Exception as exc:  # noqa: BLE001
        print(f"  FantasyPros consensus tier fetch failed ({exc}) -- falling back to in-house estimate for everyone.")
        return {}


def build_news_client_or_none() -> tuple[Any, Optional[str]]:
    """(client, reason); client is None when summarization can't run."""
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "ANTHROPIC_API_KEY not set"
    try:
        from news import build_anthropic_client

        return build_anthropic_client(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def load_news_flags_nflreadpy(
    pool: list[dict[str, Any]],
    client: Any,
    espn_injury_rows: Optional[list[dict[str, Any]]] = None,
    store: Any = None,
) -> dict[str, dict[str, Any]]:
    """News flags for the pool: ESPN injuries + news, Sleeper designations, LLM summary."""
    from news import (
        apply_sleeper_designation,
        articles_for_player,
        fetch_espn_news,
        fetch_sleeper_injury_status,
        summarize_player_news,
    )

    articles = fetch_espn_news()
    sleeper_data = fetch_sleeper_injury_status()

    espn_injury_by_player: dict[str, dict[str, Any]] = {}
    if espn_injury_rows:
        try:
            from espn_injuries import index_by_player

            espn_injury_by_player = index_by_player(espn_injury_rows, pool)
        except Exception:
            espn_injury_by_player = {}

    flags: dict[str, dict[str, Any]] = {}
    for player in pool:
        player_articles = []
        injury_row = espn_injury_by_player.get(player["playerId"])
        if injury_row:
            from espn_injuries import news_articles_from_injury

            player_articles += news_articles_from_injury(injury_row)
        player_articles += articles_for_player(articles, player["name"], player.get("espnId"))
        if not player_articles or client is None:
            flag = dict(DEFAULT_NEWS_FLAG)
        else:
            flag = summarize_player_news(
                player["name"], player_articles, client,
                store=store, player_id=player["playerId"],
            )
        flags[player["playerId"]] = apply_sleeper_designation(
            flag, player["name"], player.get("espnId"), sleeper_data
        )

    if client is not None:
        from news import SUMMARY_FAILURES

        summarized = sum(1 for f in flags.values() if f.get("summary"))
        print(
            f"  news: {len(articles)} ESPN articles, {summarized} players flagged"
            + (f", {len(SUMMARY_FAILURES)} summarization failures" if SUMMARY_FAILURES else "")
        )
        for failure in SUMMARY_FAILURES[:3]:
            print(f"    ! {failure}")
    return flags


def load_rotowire_stats_or_empty(
    pool: list[dict[str, Any]],
    season: int,
) -> dict[str, dict[str, Any]]:
    """Rotowire red-zone/route stats for the pool (24h disk cache), or {}."""
    try:
        from rotowire import fetch_and_cache_pool_stats, load_id_crosswalk

        crosswalk = load_id_crosswalk()
        if not crosswalk:
            print("  Rotowire crosswalk empty -- skipping Rotowire fetch.")
            return {}
        cache_path = Path(__file__).resolve().parent / f"rotowire_cache_{season}.json"
        return fetch_and_cache_pool_stats(pool, crosswalk, cache_path, season_year=season)
    except Exception as exc:  # noqa: BLE001
        print(f"  Rotowire fetch failed ({exc}) -- skipping Rotowire annotations.")
        return {}


def load_env_file(env_path: Optional[Path] = None) -> None:
    """Load the repo-root .env into os.environ before anything reads a key."""
    import os

    try:
        from dotenv import load_dotenv
    except ImportError:
        print(
            "  python-dotenv not installed -- .env not read. Any key not already "
            "in the environment will be treated as absent (news layer and "
            "FantasyPros tier skipped)."
        )
        return

    if env_path is None:
        env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    load_dotenv(env_path)

    present = [
        name
        for name in ("ANTHROPIC_API_KEY", "FANTASYPROS_API_KEY")
        if os.environ.get(name)
    ]
    missing = [
        name
        for name in ("ANTHROPIC_API_KEY", "FANTASYPROS_API_KEY")
        if not os.environ.get(name)
    ]
    print(
        f"  env: loaded {env_path.name}"
        + (f" -- {', '.join(present)} set" if present else "")
        + (f"; {', '.join(missing)} still missing" if missing else "")
    )


def should_skip_red_zone_fetch(
    week: int,
    game_logs: dict[str, Any],
    skip_flag: bool,
) -> tuple[bool, Optional[str]]:
    """Whether to skip the Rotowire red-zone fetch, and why."""
    if skip_flag:
        return True, "--skip-rotowire set"
    if week == 1:
        return True, "week 1"
    if not game_logs:
        return True, "no games played yet this season"
    return False, None


def main(argv: Optional[list[str]] = None) -> None:
    import argparse
    import os

    load_env_file()

    os.environ.setdefault("NFLREADPY_CACHE", "filesystem")

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
    parser.add_argument(
        "--skip-availability",
        action="store_true",
        help=(
            "skip the availability model (backend/availability.py); projections stay "
            "conditional-on-playing, i.e. the pre-2026-09-01 behaviour"
        ),
    )
    parser.add_argument(
        "--skip-calibration",
        action="store_true",
        help=(
            "skip empirical-Bayes shrinkage, the affine recalibration and the prediction "
            "interval; projections are the raw rolling average"
        ),
    )
    parser.add_argument(
        "--skip-blend",
        action="store_true",
        help="skip the rolling-volume + Vegas-context blend (backend/blend.py)",
    )
    parser.add_argument(
        "--skip-week1-model",
        action="store_true",
        help=(
            "skip the week-1 cold-start model (backend/week1.py); week-1 players fall back to "
            "project_player's flat no_data baseline. No effect for weeks 2+, where the model never runs."
        ),
    )
    parser.add_argument(
        "--skip-rotowire",
        action="store_true",
        help="skip the Rotowire red zone / route-efficiency fetch; waiver rationale omits those annotations",
    )
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

    print(f"Loading {args.season} player pool (nflreadpy rosters)...")
    pool = load_player_pool_nflreadpy(args.season)
    print(f"  {len(pool)} active {'/'.join(ROSTER_POSITIONS)} players")

    print(f"Loading {args.season} DST pool (nflreadpy schedules/teams)...")
    dst_pool, dst_game_logs = load_dst_pool_and_game_logs_nflreadpy(args.season)
    print(f"  {len(dst_pool)} DSTs, {len(dst_game_logs)} with at least one game logged")
    pool = pool + dst_pool

    print(f"Loading {args.season} schedule...")
    from store import config_hash as _config_hash
    from store import open_store, pack_bundle, unpack_bundle

    store = open_store()
    run_id = None
    if store.enabled:
        import subprocess

        try:
            git_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
            ).stdout.strip() or None
        except Exception:  # noqa: BLE001
            git_sha = None
        run_id = store.start_run(args.season, args.week, git_sha, None)
        if run_id:
            print(f"  store: run {run_id} opened")

    schedule_games = load_schedule_nflreadpy(args.season)

    current_week: Optional[int] = None
    try:
        from season_week import load_current_week_nflreadpy

        current_week = load_current_week_nflreadpy(args.season)
        print(f"  season is on week {current_week}")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not resolve the current week ({exc}) -- manifest omits it.")

    print(f"Loading {args.season} game logs (nflreadpy player stats)...")
    game_logs = load_all_game_logs_nflreadpy(args.season)
    game_logs.update(dst_game_logs)

    espn_injury_rows: list[dict[str, Any]] = []
    try:
        from espn_injuries import fetch_injury_rows

        espn_injury_rows = fetch_injury_rows()
        if espn_injury_rows:
            print(f"Loaded ESPN injury report: {len(espn_injury_rows)} listed players.")
        else:
            from espn_injuries import FETCH_FAILURES

            reason = FETCH_FAILURES[-1] if FETCH_FAILURES else "no rows returned"
            print(f"  ESPN injury report empty ({reason}) -- falling back to Sleeper alone.")
    except Exception as exc:  # noqa: BLE001
        print(f"  ESPN injury report unavailable ({exc}) -- falling back to Sleeper alone.")

    if args.skip_news:
        print("--skip-news set: every player gets the default healthy newsFlag.")
        news_flags: dict[str, dict[str, Any]] = {}
    else:
        client, client_error = build_news_client_or_none()
        if client is None:
            print(
                f"News summarization skipped ({client_error}) -- default newsFlag for everyone. "
                "Sleeper injury designations are still applied."
            )
        else:
            print("Fetching + summarizing ESPN news...")
        news_flags = load_news_flags_nflreadpy(pool, client, espn_injury_rows, store=store)

    raw_fp_players_by_position: Optional[dict[str, Any]] = None
    fp_crosswalk: Optional[dict[str, Any]] = None
    if args.skip_fantasypros:
        print("--skip-fantasypros set: every player gets the in-house estimate tier.")
    elif os.environ.get("FANTASYPROS_API_KEY") is None:
        print("FANTASYPROS_API_KEY not set -- FantasyPros consensus tier skipped.")
    else:
        print(f"Fetching FantasyPros consensus tier ({'/'.join(POOL_POSITIONS)})...")
        try:
            from fantasypros import fetch_consensus_tier, load_fantasypros_id_crosswalk_nflreadpy

            raw_fp_players_by_position = fetch_consensus_tier(
                args.season, args.week, os.environ["FANTASYPROS_API_KEY"]
            )
            fp_crosswalk = load_fantasypros_id_crosswalk_nflreadpy()
        except Exception as exc:  # noqa: BLE001
            print(f"  FantasyPros fetch failed ({exc}) -- FantasyPros tier skipped.")
            raw_fp_players_by_position = None
            fp_crosswalk = None

    raw_rw_players_by_position: Optional[dict[str, Any]] = None
    if not args.skip_fantasypros:
        print(f"Fetching Rotowire weekly projections ({'/'.join(POOL_POSITIONS)})...")
        try:
            from rotowire_projections import fetch_rotowire_projections

            raw_rw_players_by_position = fetch_rotowire_projections(args.week)
        except Exception as exc:  # noqa: BLE001
            print(f"  Rotowire projections fetch failed ({exc}) -- Rotowire tier skipped.")
            raw_rw_players_by_position = None

    def _build_consensus_for_config(
        scoring_config: dict[str, Any],
        pool_for_crosswalk: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Score and blend the already-fetched consensus feeds for one config."""
        from rotowire_projections import (
            blend_consensus_projections,
            build_name_team_crosswalk,
            build_rotowire_consensus_tier,
        )

        fp_result: dict[str, dict[str, Any]] = {}
        if raw_fp_players_by_position is not None:
            try:
                from fantasypros import build_consensus_tier

                fp_result = build_consensus_tier(raw_fp_players_by_position, scoring_config, fp_crosswalk)
            except Exception as exc:  # noqa: BLE001
                print(f"  FantasyPros scoring failed ({exc}).")

        rw_result: dict[str, dict[str, Any]] = {}
        if raw_rw_players_by_position is not None and pool_for_crosswalk:
            try:
                xwalk = build_name_team_crosswalk(pool_for_crosswalk)
                rw_result = build_rotowire_consensus_tier(raw_rw_players_by_position, scoring_config, xwalk)
            except Exception as exc:  # noqa: BLE001
                print(f"  Rotowire scoring failed ({exc}).")

        if not fp_result and not rw_result:
            return {}

        result = blend_consensus_projections(fp_result, rw_result, scoring_config)
        fp_only = len(fp_result) - len(set(fp_result) & set(rw_result))
        rw_only = len(rw_result) - len(set(fp_result) & set(rw_result))
        both = len(set(fp_result) & set(rw_result))
        print(
            f"  {len(result)} players resolved to the consensus tier "
            f"(both={both}, FP-only={fp_only}, RW-only={rw_only})"
        )
        return result

    def _build_week1_for_config(scoring_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        if args.week != 1 or args.skip_week1_model:
            return {}
        try:
            import week1 as week1_module

            train_seasons = list(range(args.season - WEEK1_TRAIN_SEASONS, args.season))
            history_seasons = list(range(train_seasons[0] - 2, args.season))
            history = week1_module.load_history_nflreadpy(history_seasons, scoring_config)
            context = {s: week1_module.load_week1_context_nflreadpy(s) for s in history_seasons + [args.season]}
            meta = {s: week1_module.load_player_meta_nflreadpy(s) for s in history_seasons + [args.season]}
            dvp = {s: week1_module.prior_season_dvp(history, s - 1) for s in history_seasons + [args.season]}

            training_rows = week1_module.training_rows_from_history(
                train_seasons, history, context, meta, dvp
            )
            model = week1_module.Week1Model().fit(training_rows)
            target_rows = week1_module.build_feature_rows(
                args.season,
                [g for g in history if int(g["season"]) in (args.season - 1, args.season - 2)],
                context.get(args.season, {}),
                meta.get(args.season, {}),
                dvp.get(args.season),
            )
            result = model.predict(target_rows)
            interval_model = week1_module.fit_interval_model(training_rows)
            if interval_model is not None:
                for row in target_rows:
                    projection = result.get(row["player_id"])
                    if projection is None:
                        continue
                    band = interval_model.interval(row["position"], projection["projected_points"])
                    if band is not None:
                        projection["floor"], projection["ceiling"] = band
                    projection["_interval_model"] = interval_model
            print(f"  {len(result)} players projected by the week-1 cold-start model "
                  f"(trained on {len(training_rows)} rows from {train_seasons[0]}-{train_seasons[-1]}"
                  f"{'; interval from held-out residuals' if interval_model else '; no interval'})")
            return result
        except Exception as exc:  # noqa: BLE001
            print(f"  Week-1 model failed ({exc}) -- falling back to the in-house no_data baseline.")
            return {}

    availability_model = None
    play_probabilities: dict[str, float] = {}
    if args.skip_availability:
        print("--skip-availability set: projections stay conditional-on-playing.")
    else:
        try:
            import availability as availability_module

            train_seasons = list(range(args.season - AVAILABILITY_TRAIN_SEASONS, args.season))
            rows = availability_module.build_training_rows_nflreadpy(train_seasons)
            availability_model = availability_module.AvailabilityModel().fit(rows)

            report_by_player, report_source = availability_module.load_current_injury_report(
                args.season, args.week, pool, espn_rows=espn_injury_rows
            )
            weeks_by_player: dict[str, set] = {}
            team_of_player: dict[str, Optional[str]] = {}
            for player_id, log in game_logs.items():
                weeks_by_player[player_id] = {
                    g["week"] for g in log if g.get("season") == args.season
                }
                team_of_player[player_id] = next(
                    (g.get("team") for g in reversed(log) if g.get("team")), None
                )
            team_weeks: dict[str, set] = {}
            for sched in schedule_games:
                if sched.get("season") != args.season or sched.get("week", 0) >= args.week:
                    continue
                for side in ("home_team", "away_team"):
                    if sched.get(side):
                        team_weeks.setdefault(sched[side], set()).add(sched["week"])

            depth_ranks: dict = {}
            try:
                from depth_charts import load_depth_ranks

                depth_ranks = load_depth_ranks([args.season])
                covered = sum(
                    1 for p in pool if (args.season, args.week, p["playerId"]) in depth_ranks
                )
                print(f"  depth chart: {covered}/{len(pool)} players ranked")
            except Exception as exc:  # noqa: BLE001
                print(f"  depth chart unavailable ({exc}) -- availability runs without it.")

            for player in pool:
                player_id = player["playerId"]
                if player["position"] in availability_module.ALWAYS_AVAILABLE_POSITIONS:
                    play_probabilities[player_id] = 1.0
                    continue
                played = weeks_by_player.get(player_id, set())
                scheduled = team_weeks.get(player["team"], set())
                observed = len(scheduled)
                rate = (len(played & scheduled) / observed) if observed else None
                designation = report_by_player.get(player_id, {})
                play_probabilities[player_id] = availability_model.predict_one(
                    {
                        "position": player["position"],
                        "prior_play_rate": rate,
                        "prior_games_observed": float(observed),
                        "report_status": designation.get("report_status"),
                        "practice_status": designation.get("practice_status"),
                        "depth_rank": depth_ranks.get((args.season, args.week, player_id)),
                    }
                )
            out_count = sum(1 for v in play_probabilities.values() if v < 0.2)
            print(
                f"  availability modelled for {len(play_probabilities)} players "
                f"({out_count} below 20% likely to play; injury report via "
                f"{report_source}, {len(report_by_player)} designations)"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  Availability model failed ({exc}) -- projections stay conditional-on-playing.")
            play_probabilities = {}

    expected_td_rows: dict[tuple[int, int, str], dict[str, Any]] = {}
    snap_shares: dict[tuple[int, int, str], float] = {}
    if not args.skip_blend:
        try:
            from expected_td import load_expected_td_rows, load_snap_shares

            seasons_for_features = [args.season - 1, args.season]
            expected_td_rows = load_expected_td_rows(seasons_for_features)
            snap_shares = load_snap_shares(seasons_for_features)
            print(
                f"  {len(expected_td_rows)} expected-TD rows, "
                f"{len(snap_shares)} snap-share rows loaded."
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  Expected-TD / snap-share feeds unavailable ({exc}) -- blend uses volume only.")

    def _game_logs_for_config(scoring_config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """Game logs with offense_pct and the per-league TD/non-TD split attached."""
        if not (expected_td_rows or snap_shares):
            return game_logs
        from expected_td import enrich_game_logs

        return enrich_game_logs(game_logs, expected_td_rows, snap_shares, scoring_config)

    game_context: dict[str, dict[str, Any]] = {}
    if not args.skip_blend:
        try:
            from context import load_game_context_nflreadpy

            game_context = load_game_context_nflreadpy(args.season, args.week)
        except Exception as exc:  # noqa: BLE001
            print(f"  Game context (Vegas lines) unavailable ({exc}) -- blend uses league averages.")

    def _build_calibration_for_config(scoring_config: dict[str, Any]) -> dict[str, Any]:
        """Fit k, affine, intervals and blend for one scoring config (cached)."""
        out: dict[str, Any] = {
            "positional_baselines": {}, "shrinkage_ks": {}, "affines": {},
            "interval_model": None, "blend_model": None,
        }
        if args.skip_calibration:
            return out
        try:
            import calibration as calibration_module
            from calibration_fit import fit_from_history

            cfg_hash = _config_hash(
                scoring_config,
                window=args.window,
                seasons=CALIBRATION_TRAIN_SEASONS,
                blend=not args.skip_blend,
            )
            cached = store.get_model_fit(
                args.season, args.week, "", cfg_hash, "calibration"
            )
            if cached is not None:
                print("  calibration: reused a cached fit (same scoring config)")
                return unpack_bundle(cached)

            fitted = fit_from_history(
                scoring_config,
                season=args.season,
                seasons=list(range(args.season - CALIBRATION_TRAIN_SEASONS, args.season)),
                window=args.window,
                fit_blend=not args.skip_blend,
            )
            store.put_model_fit(
                args.season, args.week, "", cfg_hash, "calibration", pack_bundle(fitted)
            )
            return fitted
        except Exception as exc:  # noqa: BLE001
            print(f"  Calibration/blend fit failed ({exc}) -- using uncalibrated projections.")
            return out

    _kdst_cache: dict[str, dict[str, float]] = {}

    def _build_kdst_baselines_for_config(scoring_config: dict[str, Any]) -> dict[str, float]:
        if args.week != 1 or args.skip_week1_model:
            return {}
        cache_key = _config_hash(scoring_config, seasons=KDST_HISTORY_SEASONS)
        if cache_key in _kdst_cache:
            return _kdst_cache[cache_key]
        merged: dict[str, float] = {}
        try:
            import week1_kdst

            history_seasons = [
                s for s in range(args.season - KDST_HISTORY_SEASONS, args.season)
                if s != 2020
            ]
            fits = week1_kdst.fit_from_history(args.season, scoring_config, history_seasons)
            for position, fit in fits.items():
                merged.update(fit["estimates"])
                print(
                    f"  week-1 {position}: {len(fit['estimates'])} per-entity baselines "
                    f"(k={fit['k']} fitted on {fit['n_fit']} rows, "
                    f"positional mean {fit['positional_mean']})"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  week-1 K/DST per-entity baselines failed ({exc}) -- using flat baselines.")
            merged = {}
        _kdst_cache[cache_key] = merged
        return merged

    _entity_prior_cache: dict[str, dict[str, float]] = {}

    def _build_entity_priors_for_config(
        scoring_config: dict[str, Any], positional_baselines: dict[str, Any]
    ) -> dict[str, float]:
        if args.week < entity_prior.FIRST_COVERED_WEEK:
            return {}
        if not positional_baselines:
            return {}
        cache_key = _config_hash(scoring_config, seasons=1)
        if cache_key in _entity_prior_cache:
            return _entity_prior_cache[cache_key]
        built: dict[str, float] = {}
        try:
            prior_logs = entity_prior.load_prior_season_logs_nflreadpy(args.season)
            means = entity_prior.entity_season_means(prior_logs, scoring_config)
            built = entity_prior.entity_baselines(means, positional_baselines)
            print(
                f"  prior-season shrinkage targets: {len(built)} players "
                f"(QB/RB/WR/TE, from {args.season - 1}, k={entity_prior.DEFAULT_PRIOR_K})"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  prior-season shrinkage targets failed ({exc}) -- using flat baselines.")
            built = {}
        _entity_prior_cache[cache_key] = built
        return built

    rz_stats: dict[str, dict[str, Any]] = {}
    skip_rz, skip_rz_reason = should_skip_red_zone_fetch(
        args.week, game_logs, args.skip_rotowire
    )
    if skip_rz:
        print(
            f"Skipping the Rotowire red-zone fetch ({skip_rz_reason}) -- "
            "waiver rationale will omit red zone / tprr annotations."
        )
    else:
        print(f"Fetching Rotowire red zone / route stats ({args.season} season)...")
        rz_stats = load_rotowire_stats_or_empty(pool, args.season)
        if rz_stats:
            print(f"  {len(rz_stats)} players with Rotowire data.")

    if args.leagues_config:
        leagues_manifest = json.loads(args.leagues_config.read_text())
        backend_dir = Path(__file__).resolve().parent

        for league in leagues_manifest["leagues"]:
            league_id: str = league["leagueId"]
            league_format: str = league["leagueFormat"]
            team_count: int = league["teamCount"]

            scoring_config_path = backend_dir / league["scoringConfigPath"]
            scoring_config = json.loads(scoring_config_path.read_text())
            validate_scoring_config(scoring_config, league_id)

            if scoring_config.get("_PLACEHOLDER"):
                print(
                    f"WARNING: {league_id} scoring config is still a placeholder -- "
                    "projections will be inaccurate until real values are entered"
                )
            for unconfirmed in scoring_config.get("_unconfirmed", []):
                print(f"  NOTE ({league_id}): {unconfirmed.split(' -- ')[0]} is unconfirmed.")

            rostered_rank_cutoff = {
                "QB": team_count,
                "RB": team_count * 2,
                "WR": team_count * 2,
                "TE": team_count,
                "DST": team_count,
                "K": team_count,
            }

            print(f"\n--- Generating report for {league_id} ({league['displayName']}) ---")
            consensus_projections = _build_consensus_for_config(scoring_config, pool)

            league_game_logs = _game_logs_for_config(scoring_config)

            out_dir = backend_dir / league["outDir"]
            out_dir.mkdir(parents=True, exist_ok=True)

            calibration_bundle = _build_calibration_for_config(scoring_config)

            weekly_report, player_pool = build_weekly_report_and_pool(
                args.season,
                args.week,
                scoring_config,
                pool,
                schedule_games,
                league_game_logs,
                news_flags,
                window=args.window,
                consensus_projections=consensus_projections,
                week1_projections=_build_week1_for_config(scoring_config),
                entity_baselines={
                    **_build_kdst_baselines_for_config(scoring_config),
                    **_build_entity_priors_for_config(
                        scoring_config, calibration_bundle.get("positional_baselines") or {}
                    ),
                },
                play_probabilities=play_probabilities,
                game_context=game_context,
                decay=DEFAULT_DECAY,
                **calibration_bundle,
                league_id=league_id,
                league_format=league_format,
                rostered_rank_cutoff=rostered_rank_cutoff,
                rz_stats_by_player=rz_stats,
            )

            report_path = out_dir / f"weekly-report-week-{args.week}.json"
            pool_path = out_dir / "player-pool.json"
            report_path.write_text(json.dumps(weekly_report, indent=2, allow_nan=False) + "\n")
            pool_path.write_text(json.dumps(player_pool, indent=2, allow_nan=False) + "\n")
            manifest_path = out_dir / "manifest.json"
            _update_manifest(manifest_path, args.week, current_week, team_count)
            print(
                f"Wrote {report_path} "
                f"({len(weekly_report['projections'])} projections, {len(weekly_report['waiverTargets'])} waiver targets)"
            )
            print(f"Wrote {pool_path} ({len(player_pool['players'])} players)")

            written = store.write_projections(
                run_id, league_id, args.season, args.week,
                _store_projection_rows(weekly_report),
            )
            if written:
                print(f"  store: logged {written} projections for {league_id}")

    else:
        scoring_config = json.loads(args.scoring_config.read_text())
        validate_scoring_config(scoring_config, "single-league")
        consensus_projections = _build_consensus_for_config(scoring_config, pool)

        calibration_bundle = _build_calibration_for_config(scoring_config)

        weekly_report, player_pool = build_weekly_report_and_pool(
            args.season,
            args.week,
            scoring_config,
            pool,
            schedule_games,
            _game_logs_for_config(scoring_config),
            news_flags,
            window=args.window,
            consensus_projections=consensus_projections,
            week1_projections=_build_week1_for_config(scoring_config),
            entity_baselines={
                **_build_kdst_baselines_for_config(scoring_config),
                **_build_entity_priors_for_config(
                    scoring_config, calibration_bundle.get("positional_baselines") or {}
                ),
            },
            play_probabilities=play_probabilities,
            game_context=game_context,
            decay=DEFAULT_DECAY,
            **calibration_bundle,
            rz_stats_by_player=rz_stats,
        )

        args.out_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.out_dir / f"weekly-report-week-{args.week}.json"
        pool_path = args.out_dir / "player-pool.json"
        report_path.write_text(json.dumps(weekly_report, indent=2, allow_nan=False) + "\n")
        pool_path.write_text(json.dumps(player_pool, indent=2, allow_nan=False) + "\n")
        manifest_path = args.out_dir / "manifest.json"
        _update_manifest(manifest_path, args.week, current_week)

        print(
            f"Wrote {report_path} "
            f"({len(weekly_report['projections'])} projections, {len(weekly_report['waiverTargets'])} waiver targets)"
        )
        print(f"Wrote {pool_path} ({len(player_pool['players'])} players)")

        written = store.write_projections(
            run_id, "single", args.season, args.week,
            _store_projection_rows(weekly_report),
        )
        if written:
            print(f"  store: logged {written} projections")

    # Deliberately not in a finally: a crashed run must stay 'running'.
    store.finish_run(run_id, "ok")
    store.close()


if __name__ == "__main__":
    main()
