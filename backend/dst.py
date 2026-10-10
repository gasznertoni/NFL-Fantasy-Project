"""D/ST stat-line assembly from team stats and schedules. See ARCHITECTURE.md §3."""

from __future__ import annotations

from typing import Any, Optional

# Duplicated from generate_report.py to avoid a circular import.
TEAM_ABBR_DISPLAY_MAP = {"LA": "LAR"}


def normalize_team(team: Optional[str]) -> Optional[str]:
    if team is None:
        return None
    return TEAM_ABBR_DISPLAY_MAP.get(team, team)


DST_DIRECT_COLUMN_MAP = {
    "def_sacks": "def_sack",
    "def_interceptions": "def_int",
    "fumble_recovery_opp": "def_fumble_rec",
    "def_safeties": "def_safety",
    "def_fumbles_forced": "fumble_forced",
    # fumble_recovery_tds is NOT defensive-only (it includes offensive recoveries).
    "fumble_recovery_tds": "def_fumble_rec_td",
    "def_tds": "def_td",
    "special_teams_tds": "def_st_td",
    "def_2pt_made": "def_2pt_return",
}

_OVERLAPPING_TD_CATEGORIES = ("def_td", "def_fumble_rec_td")


def validate_dst_td_categories(linear_config: dict) -> None:
    """Raise if a scoring config defines both def_td and def_fumble_rec_td."""
    present = [k for k in _OVERLAPPING_TD_CATEGORIES if k in linear_config]
    if len(present) > 1:
        raise ValueError(
            "scoring config defines overlapping D/ST touchdown categories "
            f"{present}; def_td already includes fumble-return scores. Define "
            "def_td (any defensive TD) or def_fumble_rec_td (fumble returns "
            "only), not both."
        )

DST_RETURN_YARD_COLUMNS = ("punt_return_yards", "kickoff_return_yards")

BLOCKED_KICK_COLUMNS = ("fg_blocked", "pt_blocked")

YARDS_ALLOWED_COLUMNS = ("passing_yards", "rushing_yards")


def _sum_columns(row: dict[str, Any], columns: tuple[str, ...]) -> float:
    return sum(row.get(c) or 0 for c in columns)


def assemble_dst_stat_line(
    own_row: dict[str, Any],
    opponent_row: dict[str, Any],
    points_allowed: float,
) -> dict[str, float]:
    """One team-game's D/ST stat line, ready for scoring.compute_league_points."""
    stat_line: dict[str, float] = {}
    for nfl_col, our_col in DST_DIRECT_COLUMN_MAP.items():
        val = own_row.get(nfl_col)
        if val:
            stat_line[our_col] = val
    return_yd = _sum_columns(own_row, DST_RETURN_YARD_COLUMNS)
    if return_yd:
        stat_line["def_return_yd"] = return_yd
    blocked = _sum_columns(opponent_row, BLOCKED_KICK_COLUMNS)
    if blocked:
        stat_line["def_blocked_kick"] = blocked
    # Always set, even at 0: a shutout is information.
    stat_line["def_yards_allowed"] = _sum_columns(opponent_row, YARDS_ALLOWED_COLUMNS)
    stat_line["def_points_allowed"] = points_allowed
    return stat_line


def _has_final_score(value: Any) -> bool:
    """Whether a score cell is a real played-game score (unplayed is NaN, not None)."""
    return value is not None and value == value


def _points_allowed_for_team_game(schedule_game: dict[str, Any], team: str) -> Optional[float]:
    """The opponent's score from `team`'s side, or None if not in or not played."""
    if team == schedule_game.get("home_team"):
        score = schedule_game.get("away_score")
    elif team == schedule_game.get("away_team"):
        score = schedule_game.get("home_score")
    else:
        return None
    return float(score) if _has_final_score(score) else None


def build_dst_game_logs(
    team_stats_rows: list[dict[str, Any]],
    schedule_games: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Team-stats + schedule rows -> {team: [game-log entry with D/ST stat line]}."""
    rows_by_game_team = {(r.get("game_id"), r.get("team")): r for r in team_stats_rows}
    schedule_by_game_id = {g.get("game_id"): g for g in schedule_games if g.get("game_id")}

    game_logs: dict[str, list[dict[str, Any]]] = {}
    for row in team_stats_rows:
        team = row.get("team")
        opponent = row.get("opponent_team")
        game_id = row.get("game_id")
        if not team or not opponent or not game_id:
            continue
        opponent_row = rows_by_game_team.get((game_id, opponent))
        schedule_game = schedule_by_game_id.get(game_id)
        if opponent_row is None or schedule_game is None:
            continue
        points_allowed = _points_allowed_for_team_game(schedule_game, team)
        if points_allowed is None:
            continue
        stat_line = assemble_dst_stat_line(row, opponent_row, points_allowed)
        stat_line["season"] = row.get("season")
        stat_line["week"] = row.get("week")
        stat_line["opponent_team"] = opponent
        game_logs.setdefault(team, []).append(stat_line)
    return game_logs


def load_team_stats_nflreadpy(season: int) -> list[dict[str, Any]]:
    import nflreadpy as nfl

    try:
        team_stats = nfl.load_team_stats(seasons=[season])
        df = team_stats.to_pandas() if hasattr(team_stats, "to_pandas") else team_stats
        if "season_type" in df.columns:
            df = df[df["season_type"] == "REG"]
        records = df.to_dict("records")
        for row in records:
            row["team"] = normalize_team(row.get("team"))
            row["opponent_team"] = normalize_team(row.get("opponent_team"))
        return records
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_team_stats_nflreadpy: nflreadpy's load_team_stats() response shape "
            "didn't match this adapter's assumptions (expected columns include 'team', "
            "'opponent_team', 'game_id', 'season', 'week'). Check nflreadpy's actual "
            "column names for your installed version and update this function."
        ) from exc


def load_schedule_with_scores_nflreadpy(season: int) -> list[dict[str, Any]]:
    """Schedule rows with game_id and final scores, for the points-allowed join."""
    import nflreadpy as nfl

    try:
        schedules = nfl.load_schedules(seasons=[season])
        df = schedules.to_pandas() if hasattr(schedules, "to_pandas") else schedules
        return [
            {
                "game_id": row["game_id"],
                "season": int(row["season"]),
                "week": int(row["week"]),
                "home_team": normalize_team(row.get("home_team")),
                "away_team": normalize_team(row.get("away_team")),
                "home_score": row.get("home_score"),
                "away_score": row.get("away_score"),
            }
            for _, row in df.iterrows()
        ]
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_schedule_with_scores_nflreadpy: nflreadpy's load_schedules() response "
            "shape didn't match this adapter's assumptions (expected columns include "
            "'game_id', 'season', 'week', 'home_team', 'away_team', 'home_score', "
            "'away_score'). Check nflreadpy's actual column names for your installed "
            "version and update this function."
        ) from exc


def load_dst_pool_nflreadpy(season: int) -> list[dict[str, Any]]:
    """One pool entry per team in this season's schedule, keyed by team abbreviation."""
    import nflreadpy as nfl

    try:
        schedules = nfl.load_schedules(seasons=[season])
        sdf = schedules.to_pandas() if hasattr(schedules, "to_pandas") else schedules
        teams = sorted({normalize_team(t) for t in set(sdf["home_team"]) | set(sdf["away_team"])})

        team_info = nfl.load_teams()
        tdf = team_info.to_pandas() if hasattr(team_info, "to_pandas") else team_info
        nick_by_abbr = {normalize_team(abbr): nick for abbr, nick in zip(tdf["team_abbr"], tdf["team_nick"])}

        return [
            {
                "playerId": team,
                "name": f"{nick_by_abbr.get(team, team)} D/ST",
                "position": "DST",
                "team": team,
            }
            for team in teams
        ]
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_dst_pool_nflreadpy: nflreadpy's load_schedules()/load_teams() response "
            "shape didn't match this adapter's assumptions (expected 'home_team'/"
            "'away_team' on schedules, 'team_abbr'/'team_nick' on teams). Check "
            "nflreadpy's actual column names for your installed version and update this "
            "function."
        ) from exc
