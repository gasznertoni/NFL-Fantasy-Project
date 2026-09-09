"""Audit III: where does the CONSENSUS-TIER residual bias actually come from?

The v20 note records the post-imputation bias on the top-10-per-position
population as QB +0.19 / RB +0.18 / WR -2.94 / TE -0.02, attributing the WR
remainder to return yardage. This decomposes the residual category by category
so the attribution is measured rather than asserted, for every position.

Run: .venv/bin/python docs/research/scripts/audit3_imputation_decomp.py
"""
import sys, os, statistics
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import league_config  # noqa: F401
import nflreadpy as nfl
import fantasypros
from scoring import compute_league_points, nflreadpy_row_to_stat_line, resolve_value

POSITIONS = ("QB", "RB", "WR", "TE")
SEASON = 2025
cfg = league_config("league-1")
LIN = cfg["linear"]

# What FantasyPros' free tier actually publishes (fantasypros.FANTASYPROS_COLUMN_MAP).
VENDOR_KEYS = set(fantasypros.FANTASYPROS_COLUMN_MAP.values())
# What impute_unpublished_categories() fills in.
IMPUTED_KEYS = {"rec_first_down", "rush_first_down", "pass_completion",
                "pass_incompletion", "pass_sacked"}

frame = nfl.load_player_stats(seasons=[SEASON])
frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
frame = frame[(frame["season_type"] == "REG") & (frame["position"].isin(POSITIONS))]
rows = frame.to_dict("records")

scored = []
by_wp = defaultdict(list)
for r in rows:
    sl = nflreadpy_row_to_stat_line(r)
    pts = float(compute_league_points(sl, cfg))
    scored.append((r, sl, pts))
    by_wp[(int(r["week"]), r["position"])].append(pts)
cut = {k: (sorted(v, reverse=True)[9] if len(v) >= 10 else 1e9) for k, v in by_wp.items()}

print(f"league-1, {SEASON}, top-10 per position per week by scored points")
print(f"{'pos':4s} {'n':>4s} {'imputation err':>14s} {'unpublished & unimputed categories -> pts/game':>50s}")
for pos in POSITIONS:
    pop = [(r, sl, p) for r, sl, p in scored
           if r["position"] == pos and p >= cut[(int(r["week"]), r["position"])]]
    if not pop:
        continue
    # (1) error introduced by the ESTIMATOR itself, on categories it does fill
    est_err = []
    # (2) points sitting in categories no vendor publishes and nothing imputes
    missing = defaultdict(list)
    for r, sl, pts in pop:
        vendor = {k: v for k, v in sl.items() if k in VENDOR_KEYS}
        vendor["position"] = pos
        imp = fantasypros.impute_unpublished_categories(vendor, pos)
        e = 0.0
        for k in IMPUTED_KEYS:
            v = resolve_value(LIN.get(k, 0), pos)
            e += (imp.get(k, 0) - sl.get(k, 0)) * v
        est_err.append(e)
        for k, v in sl.items():
            if k in VENDOR_KEYS or k in IMPUTED_KEYS or k == "position":
                continue
            val = resolve_value(LIN.get(k, 0), pos)
            if val:
                missing[k].append(v * val)
    # milestones are also unpublished
    mile = []
    for r, sl, pts in pop:
        vendor = {k: v for k, v in sl.items() if k in VENDOR_KEYS}
        vendor["position"] = pos
        full_m = compute_league_points(sl, cfg, position=pos).breakdown
        m = sum(v for k, v in full_m.items() if k.endswith("_milestone"))
        mile.append(m)
    n = len(pop)
    parts = []
    for k, vals in sorted(missing.items(), key=lambda kv: -abs(sum(kv[1]))):
        parts.append(f"{k}={sum(vals)/n:+.3f}")
    # NOT a loss term: milestones are recomputed from rec_yd/rush_yd, which
    # FantasyPros DOES publish. Printed for scale only.
    parts.append(f"[milestones(not lost)={statistics.fmean(mile):+.3f}]")
    print(f"{pos:4s} {n:4d} {statistics.fmean(est_err):+14.3f}   " + "  ".join(parts))
print("\nReading: 'imputation err' is the estimator's own bias on the five categories")
print("it fills. Everything to its right is a scored category NO vendor publishes and")
print("nothing imputes -- it is lost from the consensus tier at full value. The
bracketed milestone figure is NOT lost (it is recomputed from published
yardage); it is shown only for scale. The unbracketed terms sum with the
imputation error to the measured per-position bias, exactly.")
