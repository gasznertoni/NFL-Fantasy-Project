"""Expected touchdowns and the TD/non-TD split of a game log. See ARCHITECTURE.md §4."""

from __future__ import annotations

from typing import Any, Iterable, Optional

EXPECTED_TD_COLUMNS: dict[str, str] = {
    "pass_touchdown_exp": "pass_td",
    "rush_touchdown_exp": "rush_td",
    "rec_touchdown_exp": "rec_td",
}

ACTUAL_TD_CATEGORIES: tuple[str, ...] = ("pass_td", "rush_td", "rec_td")

EXPECTED_TD_POINTS_KEY = "exp_td_points"
NON_TD_POINTS_KEY = "non_td_points"
SNAP_SHARE_KEY = "offense_pct"


def _number(value: Any) -> float:
    """A stat as a float, treating absent, non-numeric and NaN alike as zero."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if out != out else out


def td_point_values(scoring_config: dict[str, Any]) -> dict[str, float]:
    """{category: points per touchdown} for this league, from its own config."""
    linear = scoring_config.get("linear", {}) or {}
    return {c: float(linear.get(c, 0) or 0) for c in ACTUAL_TD_CATEGORIES}


def expected_td_points(row: dict[str, Any], values: dict[str, float]) -> float:
    """Expected touchdown points for one player-week from an ff_opportunity row."""
    return sum(
        _number(row.get(column)) * values.get(category, 0.0)
        for column, category in EXPECTED_TD_COLUMNS.items()
    )


def actual_td_points(stat_line: dict[str, Any], values: dict[str, float]) -> float:
    """Realised touchdown POINTS for one player-week from a mapped stat line."""
    return sum(
        _number(stat_line.get(category)) * values.get(category, 0.0)
        for category in ACTUAL_TD_CATEGORIES
    )


def split_game_row(
    stat_line: dict[str, Any],
    opportunity_row: Optional[dict[str, Any]],
    values: dict[str, float],
    scoring_config: dict[str, Any],
) -> dict[str, Any]:
    """Add the TD / non-TD split to one game-log stat line."""
    from scoring import compute_league_points

    out = dict(stat_line)
    total = float(compute_league_points(stat_line, scoring_config))
    out[NON_TD_POINTS_KEY] = total - actual_td_points(stat_line, values)
    if opportunity_row is not None:
        out[EXPECTED_TD_POINTS_KEY] = expected_td_points(opportunity_row, values)
    return out


def enrich_game_logs(
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    opportunity_by_key: dict[tuple[int, int, str], dict[str, Any]],
    snap_share_by_key: dict[tuple[int, int, str], float],
    scoring_config: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Add exp_td_points / non_td_points / offense_pct to every game-log row."""
    values = td_point_values(scoring_config)
    out: dict[str, list[dict[str, Any]]] = {}
    for player_id, games in game_logs_by_player.items():
        enriched = []
        for game in games:
            key = (
                int(_number(game.get("season"))),
                int(_number(game.get("week"))),
                player_id,
            )
            row = split_game_row(
                game, opportunity_by_key.get(key), values, scoring_config
            )
            share = snap_share_by_key.get(key)
            if share is not None:
                row[SNAP_SHARE_KEY] = share
            enriched.append(row)
        out[player_id] = enriched
    return out


def load_expected_td_rows(seasons: Iterable[int]) -> dict[tuple[int, int, str], dict[str, Any]]:
    """{(season, week, player_id): ff_opportunity row}; empty on failure."""
    try:
        import nflreadpy as nfl
    except Exception:
        return {}

    out: dict[tuple[int, int, str], dict[str, Any]] = {}
    # One season per request: a season with no games yet raises and takes the rest down.
    for season in seasons:
        try:
            frame = nfl.load_ff_opportunity(seasons=[int(season)])
            frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
        except Exception:
            continue
        if frame is None or len(frame) == 0:
            continue
        keep = ["season", "week", "player_id", *EXPECTED_TD_COLUMNS]
        have = [c for c in keep if c in frame.columns]
        if not {"season", "week", "player_id"} <= set(have):
            continue
        for row in frame[have].to_dict("records"):
            pid = row.get("player_id")
            if not isinstance(pid, str) or not pid:
                continue
            out[(int(_number(row["season"])), int(_number(row["week"])), pid)] = row
    return out


def load_snap_shares(seasons: Iterable[int]) -> dict[tuple[int, int, str], float]:
    """{(season, week, gsis_id): offensive snap share 0-1}; empty on failure."""
    try:
        import nflreadpy as nfl

        ids = nfl.load_ff_playerids()
        ids = ids.to_pandas() if hasattr(ids, "to_pandas") else ids
        crosswalk = ids[["gsis_id", "pfr_id"]].dropna()
    except Exception:
        return {}

    out: dict[tuple[int, int, str], float] = {}
    # One season per request, as above.
    for season in seasons:
        try:
            snaps = nfl.load_snap_counts(seasons=[int(season)])
            snaps = snaps.to_pandas() if hasattr(snaps, "to_pandas") else snaps
        except Exception:
            continue
        if snaps is None or len(snaps) == 0 or "pfr_player_id" not in snaps.columns:
            continue
        try:
            snaps = snaps[snaps["game_type"] == "REG"]
            merged = snaps.merge(
                crosswalk, left_on="pfr_player_id", right_on="pfr_id", how="left"
            ).dropna(subset=["gsis_id"])
            records = merged[["season", "week", "gsis_id", "offense_pct"]].to_dict("records")
        except Exception:
            continue
        for row in records:
            key = (int(_number(row["season"])), int(_number(row["week"])), row["gsis_id"])
            out[key] = _number(row.get("offense_pct"))
    return out
