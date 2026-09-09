"""Audit III / Finding: verify every *_COLUMN_MAP key against a LIVE nflreadpy
response for 2024-25. A key matching nothing scores a silent zero (CLAUDE.md).

Run: .venv/bin/python docs/research/scripts/audit3_column_check.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import BACKEND  # noqa: F401  (puts backend/ on sys.path)
import nflreadpy as nfl

import scoring, dst, kicker

def cols(df):
    return set(df.columns)

ps = nfl.load_player_stats(seasons=[2024, 2025])
ps = ps.to_pandas() if hasattr(ps, "to_pandas") else ps
ts = nfl.load_team_stats(seasons=[2024, 2025])
ts = ts.to_pandas() if hasattr(ts, "to_pandas") else ts

pcols, tcols = cols(ps), cols(ts)
print(f"load_player_stats: {len(ps)} rows, {len(pcols)} cols")
print(f"load_team_stats:   {len(ts)} rows, {len(tcols)} cols")

def check(name, mapping, available, frame, extra_needed=()):
    print(f"\n=== {name} ({len(mapping)} keys) ===")
    bad = []
    for src, dstname in sorted(mapping.items()):
        ok = src in available
        if ok:
            nz = int((frame[src].fillna(0) != 0).sum())
            nan = int(frame[src].isna().sum())
            print(f"  OK   {src:32s} -> {dstname:22s} nonzero={nz:6d} nan={nan:6d}")
        else:
            bad.append(src)
            print(f"  MISS {src:32s} -> {dstname:22s}  *** NOT IN RESPONSE ***")
    for src in extra_needed:
        ok = src in available
        print(f"  {'OK  ' if ok else 'MISS'} {src:32s} (derived-input)")
        if not ok:
            bad.append(src)
    return bad

bad = []
bad += check("scoring.NFLREADPY_OFFENSE_COLUMN_MAP", scoring.NFLREADPY_OFFENSE_COLUMN_MAP,
             pcols, ps, extra_needed=("attempts",))
bad += check("dst.DST_DIRECT_COLUMN_MAP", dst.DST_DIRECT_COLUMN_MAP, tcols, ts)
for grp, name in ((dst.DST_RETURN_YARD_COLUMNS, "DST_RETURN_YARD_COLUMNS"),
                  (dst.BLOCKED_KICK_COLUMNS, "BLOCKED_KICK_COLUMNS"),
                  (dst.YARDS_ALLOWED_COLUMNS, "YARDS_ALLOWED_COLUMNS")):
    bad += check(f"dst.{name}", {c: "(sum)" for c in grp}, tcols, ts)
kmaps = {}
kmaps.update(kicker.NFLREADPY_KICKER_NATIVE_BUCKETS)
kmaps.update(kicker.NFLREADPY_KICKER_DIRECT_COLUMN_MAP)
for grp, label in ((kicker.FG_MADE_0_39_COLUMNS, "FG_MADE_0_39"),
                   (kicker.FG_MADE_50_PLUS_COLUMNS, "FG_MADE_50_PLUS"),
                   (kicker.FG_MISSED_COLUMNS, "FG_MISSED(flat)"),
                   (kicker.PAT_MISSED_COLUMNS, "PAT_MISSED"),
                   (kicker.FG_MISSED_0_39_COLUMNS, "FG_MISSED_0_39")):
    for c in grp:
        kmaps.setdefault(c, f"({label})")
for c in ("fg_missed_40_49", "fg_missed_50_59", "fg_missed_60_", "fg_blocked_list"):
    kmaps.setdefault(c, "(banded-miss / blocked-distance input)")
kk = ps[ps["position"] == "K"]
print(f"\n(kicker rows in 2024-25: {len(kk)})")
bad += check("kicker.py (all families)", kmaps, pcols, kk)

print("\n================ SUMMARY ================")
if bad:
    print(f"MISSING KEYS ({len(bad)}): {bad}")
else:
    print("All column-map keys present in the live 2024-25 responses.")
