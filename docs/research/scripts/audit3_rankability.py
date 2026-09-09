"""Audit III: which of league-1's scored categories are actually RANKABLE?

The walk-forward run shows the same model ranks league-2 (plain PPR) far better
than league-1 (te_premium): held-out 2025 Spearman 0.655 vs 0.565 pooled, and
0.617 vs 0.460 at WR. Both leagues are scored from the same stat lines by the
same estimator, so the gap has to come from the scoring rules themselves --
some category league-1 adds is close to unpredictable and is injecting noise
into the TARGET.

Method: for each scored category, measure how much of a player's week-to-week
variation is stable (between-player) rather than noise (within-player) --
an intraclass correlation on 2025 per-game POINTS from that category. Then
re-run the held-out rank correlation with the suspect category removed from
the actual, to size its cost directly.

Run: .venv/bin/python docs/research/scripts/audit3_rankability.py
"""
import sys, os, math, statistics
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import league_config  # noqa: F401
import nflreadpy as nfl
from scoring import compute_league_points, nflreadpy_row_to_stat_line

SEASON = 2025
POSITIONS = ("QB", "RB", "WR", "TE")
cfg = league_config("league-1")

frame = nfl.load_player_stats(seasons=[SEASON])
frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
frame = frame[(frame["season_type"] == "REG") & (frame["position"].isin(POSITIONS))]
rows = frame.to_dict("records")

# per-player series of per-category points
series = defaultdict(lambda: defaultdict(list))   # category -> player -> [pts]
totals = defaultdict(list)
for r in rows:
    sl = nflreadpy_row_to_stat_line(r)
    res = compute_league_points(sl, cfg)
    pid = r["player_id"]
    for cat, pts in res.breakdown.items():
        series[cat][pid].append(pts)
    totals[pid].append(res.total)
    # make sure every player has a 0 entry for categories they didn't hit
for cat in list(series):
    for pid in totals:
        if pid not in series[cat]:
            series[cat][pid] = [0.0] * len(totals[pid])
        else:
            # pad to full game count
            series[cat][pid] += [0.0] * (len(totals[pid]) - len(series[cat][pid]))


def icc(by_player, min_games=6):
    """One-way random-effects ICC: between-player variance share."""
    groups = [v for v in by_player.values() if len(v) >= min_games]
    if len(groups) < 20:
        return float("nan"), 0
    k = statistics.fmean(len(g) for g in groups)
    grand = statistics.fmean(x for g in groups for x in g)
    n = len(groups)
    msb = sum(len(g) * (statistics.fmean(g) - grand) ** 2 for g in groups) / (n - 1)
    within = [x - statistics.fmean(g) for g in groups for x in g]
    dfw = sum(len(g) for g in groups) - n
    msw = sum(x * x for x in within) / dfw if dfw else float("nan")
    if msb + (k - 1) * msw == 0:
        return float("nan"), n
    return (msb - msw) / (msb + (k - 1) * msw), n


print(f"league-1 scored categories, {SEASON}, stability of per-game points")
print(f"{'category':22s} {'mean pts/g':>10s} {'sd':>7s} {'ICC':>7s} {'players':>8s}")
out = []
for cat, by_p in series.items():
    allv = [x for g in by_p.values() for x in g]
    if not allv:
        continue
    i, n = icc(by_p)
    out.append((abs(statistics.fmean(allv)), cat, statistics.fmean(allv),
                statistics.pstdev(allv), i, n))
for _, cat, m, sd, i, n in sorted(out, reverse=True):
    print(f"{cat:22s} {m:10.3f} {sd:7.3f} {i:7.3f} {n:8d}")

# ---- direct cost: strip return yardage from the ACTUAL and re-rank ---------
print("\nCOST OF THE RETURN-YARDAGE LINE (league-1 only), held-out 2025")
print("Recomputing the actual with kick/punt return yards + return TDs removed,")
print("then measuring how well the SAME shipped projection ranks it.\n")

import calibration_fit, blend as blend_module

def spearman(pairs):
    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i]); r = [0.0]*len(xs); i = 0
        while i < len(order):
            j = i
            while j+1 < len(order) and xs[order[j+1]] == xs[order[i]]: j += 1
            avg = (i+j)/2.0+1
            for k in range(i, j+1): r[order[k]] = avg
            i = j+1
        return r
    p = ranks([x[0] for x in pairs]); a = ranks([x[1] for x in pairs])
    mp, ma = statistics.fmean(p), statistics.fmean(a)
    num = sum((x-mp)*(y-ma) for x, y in zip(p, a))
    den = math.sqrt(sum((x-mp)**2 for x in p)*sum((y-ma)**2 for y in a))
    return num/den if den else float("nan")

bundle = calibration_fit.fit_from_history(cfg, SEASON, [2022, 2023, 2024])
logs = calibration_fit.load_game_logs([SEASON-1, SEASON], scoring_config=cfg)
ctx = calibration_fit._load_context([SEASON]).get(SEASON)
test = calibration_fit._walk_forward_rows(logs, cfg, SEASON,
        calibration_fit.DEFAULT_WINDOW, calibration_fit.DEFAULT_DECAY, ctx)
bm = bundle["blend_model"]

# index the raw actual breakdown by (player, week) so we can subtract
ret_pts = {}
for r in rows:
    sl = nflreadpy_row_to_stat_line(r)
    b = compute_league_points(sl, cfg).breakdown
    ret_pts[(r["player_id"], int(r["week"]))] = (
        b.get("kick_return_yd", 0) + b.get("punt_return_yd", 0) + b.get("return_td", 0))

by_pos = defaultdict(lambda: ([], []))
for r in test:
    pos = r["position"]
    proj = bm.predict_one(r) if bm is not None and pos in bm.fitted_positions else r["projected"]
    a_full = r["actual_points"]
    a_less = a_full - ret_pts.get((r["player_id"], r["week"]), 0)
    by_pos[pos][0].append((proj, a_full))
    by_pos[pos][1].append((proj, a_less))
print(f"{'pos':4s} {'n':>5s} {'Spearman vs actual':>19s} {'vs actual w/o return':>21s} {'delta':>8s}")
for pos in POSITIONS:
    full, less = by_pos[pos]
    if not full: continue
    sf, sl_ = spearman(full), spearman(less)
    print(f"{pos:4s} {len(full):5d} {sf:19.4f} {sl_:21.4f} {sl_-sf:+8.4f}")
