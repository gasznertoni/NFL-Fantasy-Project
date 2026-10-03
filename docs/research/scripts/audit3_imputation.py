"""Audit III, Task 1.2: is fantasypros.impute_unpublished_categories() still
unbiased on the population the consensus tier actually covers, and does it leak?

The shipped coefficients are documented as least squares on 2022-25. The bias
figures recorded in CLAUDE.md v20 were then measured on 2025 -- which is INSIDE
the fitting window, so those numbers are in-sample and optimistic by an unknown
amount. This script separates the two questions:

  1. LEAKAGE / HONESTY. Refit the same functional forms on 2022-24 only and
     re-measure on held-out 2025. If the held-out bias matches the shipped
     figures, the in-sample fit was not buying anything and the numbers stand.
  2. POPULATION. Measure on the population the consensus tier ACTUALLY covers
     -- top 10 per position by that week's scored points -- not the whole pool.

Bias is reported in league-1 POINTS PER GAME (what the tier is ranked on),
not in natural stat units.

Run: .venv/bin/python docs/research/scripts/audit3_imputation.py
"""
import sys, os, statistics
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import league_config  # noqa: F401
import nflreadpy as nfl
import fantasypros
from scoring import compute_league_points, nflreadpy_row_to_stat_line

POSITIONS = ("QB", "RB", "WR", "TE")
FIT_SEASONS = [2022, 2023, 2024]
TEST_SEASON = 2025


def g(row, c):
    v = row.get(c)
    return 0.0 if v is None or v != v else float(v)


def lstsq(X, y):
    """Normal equations with an intercept column, plain stdlib."""
    n, k = len(X), len(X[0]) + 1
    A = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for xi, yi in zip(X, y):
        row = [1.0] + list(xi)
        for a in range(k):
            b[a] += row[a] * yi
            for c in range(k):
                A[a][c] += row[a] * row[c]
    # gaussian elimination
    for i in range(k):
        piv = max(range(i, k), key=lambda r: abs(A[r][i]))
        A[i], A[piv] = A[piv], A[i]; b[i], b[piv] = b[piv], b[i]
        if abs(A[i][i]) < 1e-12:
            return [0.0] * k
        for r in range(i + 1, k):
            f = A[r][i] / A[i][i]
            for c in range(i, k):
                A[r][c] -= f * A[i][c]
            b[r] -= f * b[i]
    out = [0.0] * k
    for i in reversed(range(k)):
        s = b[i] - sum(A[i][c] * out[c] for c in range(i + 1, k))
        out[i] = s / A[i][i]
    return out


def r2(y, pred):
    m = statistics.fmean(y)
    ss_t = sum((v - m) ** 2 for v in y)
    ss_r = sum((v - p) ** 2 for v, p in zip(y, pred))
    return 1 - ss_r / ss_t if ss_t else float("nan")


frame = nfl.load_player_stats(seasons=FIT_SEASONS + [TEST_SEASON])
frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
frame = frame[(frame["season_type"] == "REG") & (frame["position"].isin(POSITIONS))]
rows = frame.to_dict("records")
fit_rows = [r for r in rows if int(r["season"]) in FIT_SEASONS]
test_rows = [r for r in rows if int(r["season"]) == TEST_SEASON]
print(f"fit rows {len(fit_rows)} ({FIT_SEASONS})   test rows {len(test_rows)} ({TEST_SEASON})\n")

# ---- 1. refit the same forms on 2022-24 -----------------------------------
refit_rec, refit_rush = {}, {}
print("REFIT ON 2022-24 vs SHIPPED coefficients (shipped fitted on 2022-25):")
for pos in POSITIONS:
    sub = [r for r in fit_rows if r["position"] == pos and (g(r, "receptions") or g(r, "receiving_yards"))]
    if sub:
        X = [[g(r, "receptions"), g(r, "receiving_yards")] for r in sub]
        y = [g(r, "receiving_first_downs") for r in sub]
        c = lstsq(X, y)
        refit_rec[pos] = c
        pred = [max(0.0, c[0] + c[1] * x[0] + c[2] * x[1]) for x in X]
        print(f"  rec_1D {pos}: refit={[round(v,4) for v in c]}  shipped="
              f"{fantasypros.RECEIVING_FIRST_DOWN_MODEL[pos]}  R2(in-fit)={r2(y,pred):.3f}")
    sub = [r for r in fit_rows if r["position"] == pos and g(r, "rushing_yards")]
    if sub:
        X = [[g(r, "rushing_yards")] for r in sub]
        y = [g(r, "rushing_first_downs") for r in sub]
        c = lstsq(X, y)
        refit_rush[pos] = c
        pred = [max(0.0, c[0] + c[1] * x[0]) for x in X]
        print(f"  rush_1D {pos}: refit={[round(v,4) for v in c]}  shipped="
              f"{fantasypros.RUSHING_FIRST_DOWN_MODEL[pos]}  R2(in-fit)={r2(y,pred):.3f}")

sub = [r for r in fit_rows if g(r, "passing_yards")]
Xc = [[g(r, "passing_yards")] for r in sub]
refit_cmp = lstsq(Xc, [g(r, "completions") for r in sub])
refit_att = lstsq(Xc, [g(r, "attempts") for r in sub])
refit_sacks = statistics.fmean(g(r, "sacks_suffered") for r in fit_rows if g(r, "attempts") >= 1)
print(f"  completions: refit={[round(v,4) for v in refit_cmp]} shipped={fantasypros.COMPLETIONS_MODEL}")
print(f"  attempts:    refit={[round(v,4) for v in refit_att]} shipped={fantasypros.ATTEMPTS_MODEL}")
print(f"  mean sacks:  refit={refit_sacks:.3f} shipped={fantasypros.MEAN_SACKS_PER_GAME}")


def impute_with(coefs_rec, coefs_rush, cmpm, attm, sackm, sl, pos):
    out = dict(sl)
    rec, ry, rushy, py = out.get("reception", 0), out.get("rec_yd", 0), out.get("rush_yd", 0), out.get("pass_yd", 0)
    if pos in coefs_rec and (rec or ry):
        c = coefs_rec[pos]
        out.setdefault("rec_first_down", max(0.0, c[0] + c[1] * rec + c[2] * ry))
    if pos in coefs_rush and rushy:
        c = coefs_rush[pos]
        out.setdefault("rush_first_down", max(0.0, c[0] + c[1] * rushy))
    if py:
        cm = max(0.0, cmpm[0] + cmpm[1] * py)
        at = max(0.0, attm[0] + attm[1] * py)
        out.setdefault("pass_completion", cm)
        out.setdefault("pass_incompletion", max(0.0, at - cm))
        out.setdefault("pass_sacked", sackm)
    return out


SHIPPED = (fantasypros.RECEIVING_FIRST_DOWN_MODEL,
           {k: v for k, v in fantasypros.RUSHING_FIRST_DOWN_MODEL.items()},
           fantasypros.COMPLETIONS_MODEL, fantasypros.ATTEMPTS_MODEL,
           fantasypros.MEAN_SACKS_PER_GAME)
REFIT = (refit_rec, refit_rush, refit_cmp, refit_att, refit_sacks)

cfg = league_config("league-1")

# ---- 2. bias on held-out 2025, whole pool and consensus-covered pool -------
# The "vendor view" of a stat line: only the categories FantasyPros publishes.
VENDOR_KEYS = {"pass_yd", "pass_td", "pass_int", "rush_yd", "rush_td",
               "rec_yd", "rec_td", "reception", "fumble_lost"}

by_week_pos = defaultdict(list)
scored = []
for r in test_rows:
    full = nflreadpy_row_to_stat_line(r)
    pts = float(compute_league_points(full, cfg))
    scored.append((r, full, pts))
    by_week_pos[(int(r["week"]), r["position"])].append(pts)
top10 = {k: sorted(v, reverse=True)[:10][-1] if len(v) >= 10 else -1e9 for k, v in by_week_pos.items()}

print(f"\n{'='*78}\nBIAS in league-1 POINTS PER GAME  (imputed stat line - true stat line)\n{'='*78}")
print(f"{'population':22s} {'pos':4s} {'n':>5s} {'no-imputation':>14s} {'SHIPPED':>10s} {'REFIT 22-24':>12s}")
for label, pred in (("whole 2025 pool", lambda r, p: True),
                    ("top-10/pos/week", lambda r, p: p >= top10[(int(r["week"]), r["position"])])):
    for pos in POSITIONS:
        deltas_none, deltas_ship, deltas_refit = [], [], []
        for r, full, pts in scored:
            if r["position"] != pos or not pred(r, pts):
                continue
            vendor = {k: v for k, v in full.items() if k in VENDOR_KEYS}
            vendor["position"] = pos
            truth = pts
            none_pts = float(compute_league_points(vendor, cfg, position=pos))
            ship = float(compute_league_points(
                fantasypros.impute_unpublished_categories(vendor, pos), cfg, position=pos))
            ref = float(compute_league_points(
                impute_with(*REFIT, vendor, pos), cfg, position=pos))
            deltas_none.append(none_pts - truth)
            deltas_ship.append(ship - truth)
            deltas_refit.append(ref - truth)
        if deltas_none:
            print(f"{label:22s} {pos:4s} {len(deltas_none):5d} "
                  f"{statistics.fmean(deltas_none):+14.3f} {statistics.fmean(deltas_ship):+10.3f} "
                  f"{statistics.fmean(deltas_refit):+12.3f}")

print("\nNOTE: 'no-imputation' is the pre-v20 behaviour (absent contributes 0).")
print("A SHIPPED vs REFIT gap of ~0 means the 2025-inclusive fit leaked nothing.")
