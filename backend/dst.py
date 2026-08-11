"""
DST (team defense) stat-line assembly -- closes CLAUDE.md Next Steps item 3's
DST half ("Wire in DST/K"). scoring.py's tier/linear scoring mechanism
already knows how to score a DST stat line (def_sack/def_int/def_fumble_rec/
def_safety/def_blocked_kick/def_st_td/def_return_yd/fumble_forced as
linear categories, def_points_allowed/def_yards_allowed as banded tiers,
per scoring_config.placeholder.json) -- the only missing piece was
assembly: turning nflreadpy's raw team_stats + schedule rows into that
exact stat_line shape.

Per docs/research/dst-scoring-fields.md (the research this module
implements, done 2026-08-04): three categories are direct per-team fields
on nflreadpy's load_team_stats() row (forced fumbles, recovered-opponent-
fumble count, return yards); two need a self-join of team_stats against
itself on game_id, reading the *opponent's* row (blocked-kick credit,
yards allowed -- confirmed against 64 real 2024-2025 blocked-kick rows and
a real ARI/NO 2025 game respectively); points allowed needs a join against
load_schedules()'s home_score/away_score (confirmed: 100% game_id overlap
between load_team_stats() and load_schedules() for the full 2025 season).

CAVEAT (carry forward, don't resolve here): def_st_td is mapped from
nflreadpy's def_tds + special_teams_tds fields, but CLAUDE.md Next Steps
item 2 has an open question about whether ESPN even credits defensive/
return TDs to the DST slot at all -- ESPN hasn't done so, generally, since
2019, which may be a genuine incompatibility with this league's old
"DST Touchdowns: 6 points" rule rather than just a value to carry over.
def_st_td here is built against the placeholder config as it stands today,
same "placeholder now, swap later" pattern the whole project follows --
this module doesn't block on item 2 closing, but a def_st_td value coming
out of this module should not be read as confirmed-correct for the real
league until that question does.

Split, like every other module here, into pure assembly (top half --
exercised by tests/test_dst.py with synthetic team_stats/schedule rows, no
network) and a real network adapter (bottom half, `load_*`), NOT exercised
by the test suite.
"""

from __future__ import annotations

from typing import Any, Optional

# nflreadpy uses "LA" for the Rams (confirmed hands-on); the frontend's
# mock fixtures, the rest of this project's docs, and generate_report.py's
# own TEAM_ABBR_DISPLAY_MAP all use "LAR". Duplicated here (rather than
# imported from generate_report.py) to avoid a circular import --
# generate_report.py imports from this module, not the other way around --
# same one-entry-map-is-fine reasoning generate_report.py's own copy
# documents for itself.
TEAM_ABBR_DISPLAY_MAP = {"LA": "LAR"}


def normalize_team(team: Optional[str]) -> Optional[str]:
    if team is None:
        return None
    return TEAM_ABBR_DISPLAY_MAP.get(team, team)


# Direct per-team fields (docs/research/dst-scoring-fields.md) -> this
# project's linear stat_line category names. All read off the DST's own
# team_stats row -- no join needed for these three.
DST_DIRECT_COLUMN_MAP = {
    "def_sacks": "def_sack",
    "def_interceptions": "def_int",
    "fumble_recovery_opp": "def_fumble_rec",
    "def_safeties": "def_safety",
    "def_fumbles_forced": "fumble_forced",
}

# def_st_td: nflreadpy splits defensive TDs (pick-sixes, fumble-return TDs)
# from special-teams TDs (punt/kickoff return TDs) into two separate
# columns; this league's single def_st_td category sums both. See the
# module docstring's caveat -- this is a placeholder-config mapping
# decision, not a confirmed-real one.
DST_TD_COLUMNS = ("def_tds", "special_teams_tds")

# Return yardage: punt + kickoff return yards, both direct per-team fields.
DST_RETURN_YARD_COLUMNS = ("punt_return_yards", "kickoff_return_yards")

# Blocked-kick credit: docs/research/dst-scoring-fields.md confirmed these
# are recorded on the row of the team whose kick got blocked, not the
# blocking team -- so crediting a DST means reading its *opponent's* row
# for the same game_id, not its own.
BLOCKED_KICK_COLUMNS = ("fg_blocked", "pt_blocked")

# Yards allowed: no direct field: compute from the opponent's own passing +
# rushing yards on their row for the same game_id.
YARDS_ALLOWED_COLUMNS = ("passing_yards", "rushing_yards")


def _sum_columns(row: dict[str, Any], columns: tuple[str, ...]) -> float:
    return sum(row.get(c) or 0 for c in columns)


def assemble_dst_stat_line(
    own_row: dict[str, Any],
    opponent_row: dict[str, Any],
    points_allowed: float,
) -> dict[str, float]:
    """One team-game's DST stat_line, ready to feed straight into
    scoring.compute_league_points.

    Args:
        own_row: this team's nflreadpy load_team_stats() row (as a dict)
            for one (season, week) -- the DST's own defensive production.
        opponent_row: the opponent's row for the *same game_id* -- used
            only for the two categories that need the opponent's numbers
            (blocked-kick credit, yards allowed). Caller's job to find
            this via the game_id self-join; see build_dst_game_logs below.
        points_allowed: the opponent's score in this game, from
            load_schedules()'s home_score/away_score (team_stats has no
            score field at all).
    """
    stat_line: dict[str, float] = {}
    for nfl_col, our_col in DST_DIRECT_COLUMN_MAP.items():
        val = own_row.get(nfl_col)
        if val:
            stat_line[our_col] = val
    st_td = _sum_columns(own_row, DST_TD_COLUMNS)
    if st_td:
        stat_line["def_st_td"] = st_td
    return_yd = _sum_columns(own_row, DST_RETURN_YARD_COLUMNS)
    if return_yd:
        stat_line["def_return_yd"] = return_yd
    blocked = _sum_columns(opponent_row, BLOCKED_KICK_COLUMNS)
    if blocked:
        stat_line["def_blocked_kick"] = blocked
    # Unlike the categories above, def_points_allowed/def_yards_allowed are
    # banded tiers (scoring_config.placeholder.json's "tiers" section) --
    # compute_league_points' _tier_points reads these even when 0 (a
    # shutout is real, valuable information, not "nothing happened"), so
    # these two are always set, never skipped on a falsy value.
    stat_line["def_yards_allowed"] = _sum_columns(opponent_row, YARDS_ALLOWED_COLUMNS)
    stat_line["def_points_allowed"] = points_allowed
    return stat_line


def _points_allowed_for_team_game(schedule_game: dict[str, Any], team: str) -> Optional[float]:
    """The opponent's score in `schedule_game`, from this `team`'s
    perspective -- None if `team` isn't actually in this game (a caller
    bug, not an expected runtime case, but returning None rather than
    guessing keeps this function honest about it)."""
    if team == schedule_game.get("home_team"):
        return schedule_game.get("away_score")
    if team == schedule_game.get("away_team"):
        return schedule_game.get("home_score")
    return None


def build_dst_game_logs(
    team_stats_rows: list[dict[str, Any]],
    schedule_games: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """The full pure assembly step: nflreadpy's raw load_team_stats() rows
    (one per team-game) + load_schedules() rows -> {team: [{"season",
    "week", "opponent_team", **dst_stat_line}, ...]}, in the exact
    game_log shape projections.project_player already expects (this
    project's playerId for a DST *is* the team abbreviation -- see
    generate_report.py -- so this dict is keyed the same way
    game_logs_by_player already is, just with team abbreviations instead
    of gsis_ids as the key).

    Self-join key is game_id (docs/research/dst-scoring-fields.md;
    confirmed 100% game_id overlap between load_team_stats() and
    load_schedules() for the full real 2025 season). A team_stats row
    whose game_id has no matching opponent row, or no matching schedule
    row, is skipped rather than guessed at -- an incomplete/mid-scrape
    data pull should produce fewer game-log entries, not fabricated ones.

    schedule_games is expected in generate_report.load_schedule_nflreadpy's
    shape (season/week/home_team/away_team) PLUS game_id/home_score/
    away_score, which that adapter doesn't currently load -- see this
    module's own load_team_stats_nflreadpy/load_schedule_with_scores_nflreadpy
    adapters below for where those extra fields come from in a real run.
    """
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


# ---------------------------------------------------------------------------
# Real data adapters -- NOT exercised by the test suite (network + nflreadpy
# required, same caveat as every other load_* function in this backend).
# Confirmed working end-to-end against real 2025 data (see this module's
# probe notes / backend/README.md) before merge.
# ---------------------------------------------------------------------------


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
    """Separate from generate_report.load_schedule_nflreadpy: that adapter
    intentionally only carries season/week/home_team/away_team (everything
    the existing QB/RB/WR/TE opponent lookup needs). DST's points-allowed
    join additionally needs game_id and the final score, so this pulls a
    superset of columns from the same load_schedules() call rather than
    changing that adapter's existing (smaller, already-tested-against)
    output shape for every other caller."""
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
    """The DST equivalent of generate_report.load_player_pool_nflreadpy: one
    entry per team that actually appears in this season's schedule (not
    nflreadpy's full load_teams() table, which also carries relocated/
    defunct franchise rows not relevant to a live season), keyed by team
    abbreviation as playerId -- a DST isn't a per-player entity, so there's
    no gsis_id to use, and this project's established preference is a
    real, stable ID over an invented p_00501-style scheme (see
    generate_report.py's own docstring on this) -- team abbreviations are
    exactly that."""
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
