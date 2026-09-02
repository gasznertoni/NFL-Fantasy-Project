"""
Opportunity-based expected touchdowns, and the TD/non-TD split of a game log.

Why this exists
---------------
Fantasy points decompose exactly:

    points = (non-TD points) + (TD points)

and the two halves behave nothing alike. Touchdowns are the least stable
component in the whole system -- receiving-TD ICC is 0.07-0.11 -- while volume
and yardage are comparatively steady. A rolling average of TOTAL points
inherits the touchdown noise directly, which is why the estimator has always
sat near the ICC ceiling.

`nflreadpy.load_ff_opportunity()` carries expected touchdowns derived from
actual opportunity (down, distance, field position), separately for passing,
rushing and receiving, 2021 onward. Replacing the touchdown term -- and ONLY
the touchdown term -- with that expectation improves every position:

    position   RMSE (rolling total)   RMSE (split, expected TDs)   paired p
    QB               10.114                    10.080                0.55
    RB                6.529                     6.468              1.2e-02
    WR                6.420                     6.378              6.4e-03
    TE                5.336                     5.276              3.6e-03

worth +0.48 lineup points a week (t=2.86, p=4.2e-03) over 1 920 simulated
14-team lineups on held-out 2024-25. See
docs/research/second-audit-2026-09-02.md section 4.

The narrowness matters
----------------------
Swapping in expected points WHOLESALE is worth nothing -- it is statistically
indistinguishable from the rolling average as a standalone predictor (all four
positions, p > 0.27) and adds literally nothing on top of the volume block
already in blend.py (RB 6.467 -> 6.468). Expected points is derived from
volume, so the blend already contains it. The value is in the touchdown term
alone, and a wholesale swap discards the well-measured non-TD signal along with
the noisy one. That is the trap this module is shaped to avoid.

League-aware by construction
----------------------------
Expected touchdowns are converted to POINTS through each league's own scoring
config, not a hardcoded 6. league-1 scores a passing TD at 6 and league-2 at 4,
so a shared constant would be wrong for one of them. The same config drives the
non-TD residual, which keeps `non_td + td == total` exact in both leagues.

Architecture matches every other module here: pure functions over plain dict
rows (tested with synthetic data, no network), plus a thin network adapter at
the bottom that is not exercised by the suite.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# nflreadpy's expected-TD columns -> the scoring category whose per-TD value
# converts them to points. Keyed this way so a league that prices passing and
# rushing touchdowns differently is handled without a special case.
EXPECTED_TD_COLUMNS: dict[str, str] = {
    "pass_touchdown_exp": "pass_td",
    "rush_touchdown_exp": "rush_td",
    "rec_touchdown_exp": "rec_td",
}

# The realised touchdown categories, used to strip touchdown points back out of
# an actual game so the remainder is the stable half. These are THIS PROJECT's
# stat-line category names, not nflreadpy's columns: game logs are already
# mapped stat lines by the time they reach this module (see
# generate_report.load_all_game_logs_nflreadpy), and scoring them is a
# per-league step because the two leagues price a passing TD differently.
ACTUAL_TD_CATEGORIES: tuple[str, ...] = ("pass_td", "rush_td", "rec_td")

# Game-log keys this module writes. blend.VOLUME_COLUMNS consumes them, so a
# rename here has to happen there too.
EXPECTED_TD_POINTS_KEY = "exp_td_points"
NON_TD_POINTS_KEY = "non_td_points"
SNAP_SHARE_KEY = "offense_pct"


def _number(value: Any) -> float:
    """A stat as a float, treating absent, non-numeric and NaN alike as zero.

    NaN is truthy and `float("nan")` survives a `or 0` guard, which is exactly
    how three scoring defects hid through 253 passing tests in v16. Checked
    explicitly here for the same reason scoring.py now does.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if out != out else out


def td_point_values(scoring_config: dict[str, Any]) -> dict[str, float]:
    """{category: points per touchdown} for this league, from its own config.

    A category the league does not define is worth 0, which is the correct
    reading of an absent rule and matches compute_league_points' contract.
    """
    linear = scoring_config.get("linear", {}) or {}
    return {c: float(linear.get(c, 0) or 0) for c in ACTUAL_TD_CATEGORIES}


def expected_td_points(row: dict[str, Any], values: dict[str, float]) -> float:
    """Expected touchdown POINTS for one player-week from an ff_opportunity row."""
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
    """Add the TD / non-TD split to one game-log stat line.

    Args:
        stat_line: a game-log entry -- mapped stat categories plus season/week.
        opportunity_row: the matching load_ff_opportunity() row, for expected
            touchdowns. May be None; coverage is ~90% of player-weeks.
        values: td_point_values() for this league.
        scoring_config: this league's config, used to score the row.

    Returns:
        The same dict with NON_TD_POINTS_KEY always set, and
        EXPECTED_TD_POINTS_KEY set only when an opportunity row was available.
        Leaving the key ABSENT rather than zero is deliberate: blend.py
        distinguishes "missing" from "zero" with a per-feature flag, and a
        player with no expected-TD data is not a player with no expected
        touchdowns.

    The keys written here are not scoring categories, so compute_league_points
    ignores them -- it reads only keys present in the config. That is the same
    mechanism that already lets season/week/opponent_team ride along on a
    stat line without being scored.
    """
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
    """Add exp_td_points / non_td_points / offense_pct to every game-log row.

    Lookup keys are (season, week, player_id). A player or week missing from a
    lookup simply does not get that feature, which blend.py handles as its own
    state rather than as a zero.

    Per-league, because two of the three features are denominated in points and
    the leagues price touchdowns differently.
    """
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


# ---------------------------------------------------------------------------
# Network adapters -- NOT exercised by the test suite.
# ---------------------------------------------------------------------------
def load_expected_td_rows(seasons: Iterable[int]) -> dict[tuple[int, int, str], dict[str, Any]]:
    """{(season, week, player_id): ff_opportunity row}. Empty on any failure --
    the split degrades to the plain rolling average, which is the behaviour
    before this module existed."""
    try:
        import nflreadpy as nfl
    except Exception:
        return {}

    out: dict[tuple[int, int, str], dict[str, Any]] = {}
    # ONE SEASON AT A TIME. nflverse has no file for the current season until
    # games have been played, and a combined request for [2025, 2026] raises --
    # taking the 2025 rows down with it. The first 2026 week-1 run loaded
    # "0 expected-TD rows, 0 snap-share rows" for exactly this reason. Same
    # cold-start reality load_all_game_logs_nflreadpy already handles.
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
    """{(season, week, gsis_id): offensive snap share 0-1}.

    load_snap_counts() keys on a PFR id, so this joins through
    load_ff_playerids()' pfr_id -> gsis_id crosswalk. Empty on any failure.
    """
    try:
        import nflreadpy as nfl

        ids = nfl.load_ff_playerids()
        ids = ids.to_pandas() if hasattr(ids, "to_pandas") else ids
        crosswalk = ids[["gsis_id", "pfr_id"]].dropna()
    except Exception:
        return {}

    out: dict[tuple[int, int, str], float] = {}
    # Per season, for the same cold-start reason as load_expected_td_rows.
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
