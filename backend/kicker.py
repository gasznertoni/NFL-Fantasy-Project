"""Kicker stat-line assembly from load_player_stats() rows. See ARCHITECTURE.md §3."""

from __future__ import annotations

from typing import Any

FG_MADE_0_39_COLUMNS = ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39")
FG_MADE_50_PLUS_COLUMNS = ("fg_made_50_59", "fg_made_60_")

NFLREADPY_KICKER_NATIVE_BUCKETS = {
    "fg_made_0_19": "fg_made_0_19",
    "fg_made_20_29": "fg_made_20_29",
    "fg_made_30_39": "fg_made_30_39",
    "fg_made_50_59": "fg_made_50_59",
    "fg_made_60_": "fg_made_60_plus",
}

NFLREADPY_KICKER_DIRECT_COLUMN_MAP = {
    "fg_made_40_49": "fg_made_40_49",
    "pat_made": "pat_made",
}

ROLLED_BANDS = ("fg_made_0_39", "fg_made_50_plus")
NATIVE_BANDS = ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39",
                "fg_made_50_59", "fg_made_60_plus")

FLAT_MISS_BAND = ("fg_missed",)
BANDED_MISS_BANDS = ("fg_missed_0_39", "fg_missed_40_49",
                     "fg_missed_50_59", "fg_missed_60_plus")

FG_MISSED_0_39_COLUMNS = ("fg_missed_0_19", "fg_missed_20_29", "fg_missed_30_39")


def validate_fg_band_family(linear_config: dict) -> None:
    """Raise if a scoring config mixes the rolled and native FG band families."""
    rolled = [k for k in ROLLED_BANDS if k in linear_config]
    native = [k for k in NATIVE_BANDS if k in linear_config]
    if rolled and native:
        raise ValueError(
            "scoring config mixes FG band families and would double-count made "
            f"field goals: rolled {rolled} alongside native {native}. Use one "
            "family only -- either fg_made_0_39/fg_made_40_49/fg_made_50_plus, "
            "or the six native fg_made_0_19...fg_made_60_plus buckets."
        )

FG_MISSED_COLUMNS = ("fg_missed", "fg_blocked")
PAT_MISSED_COLUMNS = ("pat_missed", "pat_blocked")


def validate_fg_miss_band_family(linear_config: dict) -> None:
    """Raise if a scoring config mixes the flat and banded FG-miss families."""
    flat = [k for k in FLAT_MISS_BAND if k in linear_config]
    banded = [k for k in BANDED_MISS_BANDS if k in linear_config]
    if flat and banded:
        raise ValueError(
            "scoring config mixes FG-miss families and would double-charge "
            f"missed field goals: flat {flat} alongside banded {banded}. Use "
            "one family only -- either fg_missed, or the banded "
            "fg_missed_0_39/fg_missed_40_49/fg_missed_50_59/fg_missed_60_plus."
        )


def _sum_columns(row: dict[str, Any], columns: tuple[str, ...]) -> float:
    return sum(row.get(c) or 0 for c in columns)


def _blocked_distances(row: dict[str, Any]) -> list[float]:
    """Blocked-FG distances from `fg_blocked_list` (fg_blocked_distance is a sum)."""
    raw = row.get("fg_blocked_list")
    if not raw or not isinstance(raw, str):
        return []
    out = []
    for part in raw.replace(",", ";").split(";"):
        part = part.strip()
        if part:
            try:
                out.append(float(part))
            except ValueError:
                continue
    return out


def nflreadpy_kicker_row_to_stat_line(row: dict[str, Any]) -> dict[str, float]:
    """Map one load_player_stats() row to kicker stat-line keys ({} for non-K)."""
    out: dict[str, float] = {}
    if row.get("position") == "K":
        out["position"] = "K"
    made_0_39 = _sum_columns(row, FG_MADE_0_39_COLUMNS)
    if made_0_39:
        out["fg_made_0_39"] = made_0_39
    made_50_plus = _sum_columns(row, FG_MADE_50_PLUS_COLUMNS)
    if made_50_plus:
        out["fg_made_50_plus"] = made_50_plus
    for nfl_col, our_col in NFLREADPY_KICKER_NATIVE_BUCKETS.items():
        val = row.get(nfl_col)
        if val:
            out[our_col] = val
    for nfl_col, our_col in NFLREADPY_KICKER_DIRECT_COLUMN_MAP.items():
        val = row.get(nfl_col)
        if val:
            out[our_col] = val
    fg_missed = _sum_columns(row, FG_MISSED_COLUMNS)
    if fg_missed:
        out["fg_missed"] = fg_missed
    # Native missed buckets exclude blocks, so blocks are added by distance.
    banded = {
        "fg_missed_0_39": _sum_columns(row, FG_MISSED_0_39_COLUMNS),
        "fg_missed_40_49": row.get("fg_missed_40_49") or 0,
        "fg_missed_50_59": row.get("fg_missed_50_59") or 0,
        "fg_missed_60_plus": row.get("fg_missed_60_") or 0,
    }
    for distance in _blocked_distances(row):
        if distance < 40:
            banded["fg_missed_0_39"] += 1
        elif distance < 50:
            banded["fg_missed_40_49"] += 1
        elif distance < 60:
            banded["fg_missed_50_59"] += 1
        else:
            banded["fg_missed_60_plus"] += 1
    for key, value in banded.items():
        if value:
            out[key] = value
    pat_missed = _sum_columns(row, PAT_MISSED_COLUMNS)
    if pat_missed:
        out["pat_missed"] = pat_missed
    return out
