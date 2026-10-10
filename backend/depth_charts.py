"""Depth-chart rank as of a week, across both nflverse schemas. See ARCHITECTURE.md §6."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

MAX_RANK = 3


def bucket_rank(rank: Optional[float]) -> Optional[int]:
    """Depth rank as 1, 2 or 3 (third string or deeper), or None."""
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
    """ISO-8601 (trailing 'Z' allowed) to an aware datetime, or None."""
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
    return None if out != out else out  # NaN check


def rank_map_from_legacy(rows: list[dict[str, Any]]) -> dict[tuple[int, int, str], float]:
    """Pre-2025 feed: one row per player per week, `depth_team` is the rank."""
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
        if key not in out or rank < out[key]:
            out[key] = rank
    return out


def slot_ranks(rows: list[dict[str, Any]]) -> list[Optional[float]]:
    """Each snapshot row's rank WITHIN ITS SLOT, aligned with `rows`."""
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
    """2025-onward feed: timestamped league-wide snapshots, no week column."""
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


def load_depth_ranks(seasons: list[int]) -> dict[tuple[int, int, str], float]:
    """{(season, week, player_id): depth rank} across `seasons`."""
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
                stamp = record.get("dt")
                if hasattr(stamp, "isoformat"):
                    record["dt"] = stamp.isoformat()
            out.update(
                rank_map_from_snapshots(records, {int(season): week_starts.get(int(season), {})})
            )
    return out


def _week_starts(nfl, pd, seasons: list[int]) -> dict[int, dict[int, Any]]:
    """{season: {week: first kickoff}}, the as-of cutoff for each week's snapshot."""
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
