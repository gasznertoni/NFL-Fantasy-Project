"""
Depth-chart rank, as-of a given week, across both nflverse schemas.

Why this is its own module: nflverse changed the depth-chart feed in 2025 and
the two shapes need different handling, but three callers want the same answer.

    through 2024   one row per player per week, with a `depth_team` rank
                   WITHIN a slot (all three starting receivers are 1)
    2025 onward    a stream of timestamped league-wide snapshots (`dt`,
                   `pos_rank`, `pos_slot`, `pos_abb`) with no week column at
                   all, and a `pos_rank` that runs ACROSS the position (the
                   three starting receivers are 1, 2 and 3) -- slot_ranks
                   converts it back to the legacy meaning

The newer feed also extends past the season, so it MUST be filtered to
snapshots taken on or before the week in question -- an unfiltered read hands
a week-1 projection a depth chart from the following March. week1.py learned
that the hard way; this module is where the lesson lives now.

What depth rank is for: availability. A player's position on the chart is the
strongest predictor of whether they record a game at all, and it is available
before a season starts, which play-rate history is not. Measured over 2018-24
on players carrying NO injury designation:

    position   depth 1   depth 2   depth 3+
    QB          0.972     0.490     0.639
    RB          0.941     0.880     0.730
    WR          0.965     0.790     0.722
    TE          0.928     0.788     0.643

Note that is not monotonic across positions -- a backup QB (0.49) behaves
nothing like a backup RB (0.88), because a second-string running back still
touches the ball and a second-string quarterback usually does not appear at
all. Any model using this needs position-by-depth interactions, not one
linear "depth" term.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

# Ranks are capped here: beyond third string the distinction stops carrying
# information (the pool thins out and the play rate flattens), and capping
# keeps the feature space small enough to interact with position.
MAX_RANK = 3


def bucket_rank(rank: Optional[float]) -> Optional[int]:
    """Depth rank as 1, 2 or 3 (3 meaning "third string or deeper"), or None."""
    if rank is None:
        return None
    try:
        value = int(rank)
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return min(value, MAX_RANK)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """ISO-8601 to an aware datetime, or None. Accepts the trailing 'Z' that
    nflverse emits, which datetime.fromisoformat rejects before 3.11."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out  # NaN check without importing math


def rank_map_from_legacy(rows: list[dict[str, Any]]) -> dict[tuple[int, int, str], float]:
    """Pre-2025 feed: one row per player per week, `depth_team` is the rank.

    Takes plain dict rows, not a DataFrame, so the parsing is testable without
    pandas -- the same pure-core / adapter-shell split every other module here
    uses.
    """
    out: dict[tuple[int, int, str], float] = {}
    for row in rows:
        if row.get("game_type") not in (None, "REG"):
            continue
        player_id = row.get("gsis_id")
        rank = _number(row.get("depth_team"))
        season, week = _number(row.get("season")), _number(row.get("week"))
        if not player_id or rank is None or season is None or week is None:
            continue
        key = (int(season), int(week), player_id)
        # A player can be listed at more than one position; the best (lowest)
        # rank is the one that describes their role.
        if key not in out or rank < out[key]:
            out[key] = rank
    return out


def slot_ranks(rows: list[dict[str, Any]]) -> list[Optional[float]]:
    """Each snapshot row's rank WITHIN ITS SLOT, aligned with `rows`.

    The two feeds do not mean the same thing by rank. The legacy `depth_team`
    ranks within a slot: all three starting receivers are 1 and their backups 2.
    The 2025 feed's `pos_rank` is an ordinal across the whole position instead
    -- Chase 1, Higgins 2, Iosivas 3, then the backups 4-6 -- and `pos_slot`
    says which receiver slot each belongs to. Every team lines up as
    "3WR 1TE", so QB/RB/TE have one slot and are unaffected, but read raw,
    the WR2 starter is a depth-2 backup and the WR3 starter lands in the
    third-string bucket. The availability model learned its depth
    coefficients on the legacy meaning, so a healthy Tee Higgins came out at
    P(play) 0.842 against JSN's 0.955.

    Re-ranking by pos_rank within (snapshot, team, grouping, slot) recovers
    the legacy meaning exactly: slot 1 is Chase 1 / Tinsley 2, slot 2 Higgins
    1 / Jones 2, slot 8 Iosivas 1 / Burton 2. Ties share a rank. A row with
    no slot keeps its pos_rank -- there is nothing to re-rank it against.
    """
    keys: list[Optional[tuple]] = []
    groups: dict[tuple, set[float]] = {}
    for row in rows:
        rank = _number(row.get("pos_rank"))
        slot = _number(row.get("pos_slot"))
        if rank is None or slot is None:
            keys.append(None)
            continue
        key = (str(row.get("dt")), row.get("team"), row.get("pos_grp"), slot)
        groups.setdefault(key, set()).add(rank)
        keys.append(key)

    order = {key: {r: i + 1 for i, r in enumerate(sorted(ranks))} for key, ranks in groups.items()}
    out: list[Optional[float]] = []
    for row, key in zip(rows, keys):
        rank = _number(row.get("pos_rank"))
        out.append(rank if key is None else float(order[key][rank]))
    return out


def rank_map_from_snapshots(
    rows: list[dict[str, Any]],
    week_starts: dict[int, dict[int, Any]],
) -> dict[tuple[int, int, str], float]:
    """2025-onward feed: timestamped league-wide snapshots, no week column.

    Each week takes the LATEST snapshot at or before that week's first kickoff.
    The as-of filter is the whole point: the feed extends past the season, so an
    unfiltered read hands a week-1 projection a chart from the following March.

    Ranks are re-expressed within each slot first (see slot_ranks), so a
    starting WR2 is depth 1 here exactly as he was in the legacy feed.
    """
    parsed: list[tuple[int, datetime, str, float]] = []
    for row, rank in zip(rows, slot_ranks(rows)):
        stamp = _parse_timestamp(row.get("dt"))
        season = _number(row.get("season"))
        player_id = row.get("gsis_id")
        if stamp is None or rank is None or season is None or not player_id:
            continue
        parsed.append((int(season), stamp, player_id, rank))
    if not parsed:
        return {}

    out: dict[tuple[int, int, str], float] = {}
    for season, weeks in week_starts.items():
        season_rows = [r for r in parsed if r[0] == int(season)]
        if not season_rows:
            continue
        for week, kickoff in weeks.items():
            cutoff = _parse_timestamp(kickoff)
            eligible = [r for r in season_rows if cutoff is None or r[1] <= cutoff]
            if not eligible:
                continue
            latest = max(r[1] for r in eligible)
            for _s, stamp, player_id, rank in eligible:
                if stamp != latest:
                    continue
                key = (int(season), int(week), player_id)
                if key not in out or rank < out[key]:
                    out[key] = rank
    return out


# ---------------------------------------------------------------------------
# Network adapter -- NOT exercised by the test suite.
# ---------------------------------------------------------------------------
def load_depth_ranks(seasons: list[int]) -> dict[tuple[int, int, str], float]:
    """{(season, week, player_id): depth rank} across `seasons`.

    Returns {} for any season whose feed is unavailable or whose shape is
    unrecognised, rather than raising -- depth rank is a useful feature, not a
    required one, and every consumer treats a missing rank as its own state.
    """
    import nflreadpy as nfl
    import pandas as pd

    out: dict[tuple[int, int, str], float] = {}
    week_starts = _week_starts(nfl, pd, seasons)

    for season in seasons:
        try:
            frame = nfl.load_depth_charts(seasons=[int(season)])
            frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
        except Exception:
            continue
        if frame is None or frame.empty:
            continue
        columns = set(frame.columns)
        if {"week", "depth_team"} <= columns:
            out.update(rank_map_from_legacy(frame.to_dict("records")))
        elif {"dt", "pos_rank"} <= columns:
            if "season" not in columns:
                frame = frame.assign(season=int(season))
            records = frame.to_dict("records")
            for record in records:
                # pandas hands back Timestamps; the pure parser wants ISO text
                # or a datetime, and isoformat() satisfies both.
                stamp = record.get("dt")
                if hasattr(stamp, "isoformat"):
                    record["dt"] = stamp.isoformat()
            out.update(
                rank_map_from_snapshots(records, {int(season): week_starts.get(int(season), {})})
            )
    return out


def _week_starts(nfl, pd, seasons: list[int]) -> dict[int, dict[int, Any]]:
    """{season: {week: first kickoff}} -- the as-of cutoff each week's
    snapshot must not cross."""
    try:
        frame = nfl.load_schedules()
        frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
        frame = frame[(frame["game_type"] == "REG") & (frame["season"].isin([int(s) for s in seasons]))]
        frame = frame.assign(_kick=pd.to_datetime(frame["gameday"], errors="coerce", utc=True))
    except Exception:
        return {}

    out: dict[int, dict[int, Any]] = {}
    for (season, week), group in frame.groupby(["season", "week"]):
        kickoff = group["_kick"].min()
        out.setdefault(int(season), {})[int(week)] = (
            kickoff.isoformat() if hasattr(kickoff, "isoformat") else kickoff
        )
    return out
