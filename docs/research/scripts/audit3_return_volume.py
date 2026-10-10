"""Audit III, tested proposal: does giving the blend a ROLLING RETURN-YARDAGE
feature recover the ranking that league-1's return-yardage line costs?

Motivation (audit3_rankability.py): league-1 scores kick/punt return yards at
0.1/yd. That category is worth ~1.0 pts/game to the WR/RB population, its ICC
is 0.488 -- as stable as receiving yards, because return duty is a role, not a
coin flip -- and the model has NO feature for it. Stripping it from the ACTUAL
raises held-out 2025 Spearman by +0.107 (RB) and +0.100 (WR). So the category is
predictable and unpredicted.

Measured against the STACK WE SHIP (CLAUDE.md's rule: "measure against the stack
you ship, not the component you are replacing") -- rolling average + shrinkage +
blend + affine -- with the ONLY change being two extra blend features.

Reports RMSE, MAE, pairwise start/sit accuracy and Spearman together, plus a
paired significance test on squared error.

Run: .venv/bin/python docs/research/scripts/audit3_return_volume.py
"""
import sys, os, math, random, statistics
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import league_config  # noqa: F401

import blend as blend_module
import calibration_fit

TRAIN = [2022, 2023, 2024]
TEST = 2025
EXTRA = ("kick_return_yd", "punt_return_yd")
random.seed(20260909)


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
    p, a = ranks([x[0] for x in pairs]), ranks([x[1] for x in pairs])
    mp, ma = statistics.fmean(p), statistics.fmean(a)
    num = sum((x-mp)*(y-ma) for x, y in zip(p, a))
    den = math.sqrt(sum((x-mp)**2 for x in p)*sum((y-ma)**2 for y in a))
    return num/den if den else float("nan")


def pairwise(rows_a, rows_b=None):
    """Deterministic: enumerate every within-(position, week) pair once. When
    two variants are passed they are scored on the SAME pairs."""
    groups = defaultdict(list)
    for i, r in enumerate(rows_a):
        groups[(r["position"], r["week"])].append(i)
    ga = gb = ta = tb = 0
    for idx in groups.values():
        for x in range(len(idx)):
            for y in range(x+1, len(idx)):
                i, j = idx[x], idx[y]
                a1, a2 = rows_a[i]["actual"], rows_a[j]["actual"]
                if a1 == a2:
                    continue
                p1, p2 = rows_a[i]["proj"], rows_a[j]["proj"]
                if p1 != p2:
                    ta += 1; ga += (p1 > p2) == (a1 > a2)
                if rows_b is not None:
                    q1, q2 = rows_b[i]["proj"], rows_b[j]["proj"]
                    if q1 != q2:
                        tb += 1; gb += (q1 > q2) == (a1 > a2)
    if rows_b is None:
        return ga/ta, ta
    return (ga/ta, ta), (gb/tb, tb)


def paired_z(sq_a, sq_b):
    """Paired normal-approximation test on per-row squared error (a - b)."""
    d = [x - y for x, y in zip(sq_a, sq_b)]
    n = len(d)
    m = statistics.fmean(d)
    sd = statistics.pstdev(d)
    if sd == 0:
        return float("nan"), float("nan")
    z = m / (sd / math.sqrt(n))
    p = math.erfc(abs(z) / math.sqrt(2))
    return z, p


def build(cfg, features):
    """Fit on TRAIN, evaluate on TEST, with `features` as the blend's columns."""
    orig = blend_module.FEATURES
    orig_vol = blend_module.VOLUME_COLUMNS
    blend_module.FEATURES = features
    blend_module.VOLUME_COLUMNS = tuple(c for c in features if c not in
                                        blend_module.BASE_COLUMNS + blend_module.CONTEXT_COLUMNS)
    try:
        bundle = calibration_fit.fit_from_history(cfg, TEST, TRAIN)
        logs = calibration_fit.load_game_logs([TEST-1, TEST], scoring_config=cfg)
        ctx = calibration_fit._load_context([TEST]).get(TEST)
        test = calibration_fit._walk_forward_rows(
            logs, cfg, TEST, calibration_fit.DEFAULT_WINDOW,
            calibration_fit.DEFAULT_DECAY, ctx)
        aff = {}
        for pid, a in bundle["affines"].items():
            pos = next((g.get("position") for g in logs.get(pid, []) if g.get("position")), None)
            if pos: aff[pos] = a
        bm = bundle["blend_model"]
        out = []
        for r in test:
            pos = r["position"]
            v = bm.predict_one(r) if bm is not None and pos in bm.fitted_positions else r["projected"]
            a = aff.get(pos)
            if a: v = a[0] + a[1]*v
            out.append({"position": pos, "week": r["week"],
                        "actual": r["actual_points"], "proj": v})
        return out
    finally:
        blend_module.FEATURES = orig
        blend_module.VOLUME_COLUMNS = orig_vol


for league_id in ("league-1", "league-2"):
    cfg = league_config(league_id)
    print(f"\n{'#'*76}\n# {league_id}\n{'#'*76}")
    base = build(cfg, blend_module.FEATURES)
    withret = build(cfg, blend_module.FEATURES + EXTRA)
    assert len(base) == len(withret)

    print(f"\n{'variant':28s} {'RMSE':>7s} {'MAE':>7s} {'pairwise':>9s} {'Spearman':>9s}")
    (acc_b, nb), (acc_r, nr) = pairwise(base, withret)
    for name, rows, acc in (("SHIPPED blend", base, acc_b),
                            ("SHIPPED + return volume", withret, acc_r)):
        pr = [(r["proj"], r["actual"]) for r in rows]
        print(f"{name:28s} {math.sqrt(statistics.fmean((a-p)**2 for p,a in pr)):7.4f} "
              f"{statistics.fmean(abs(a-p) for p,a in pr):7.4f} {acc:9.4f} {spearman(pr):9.4f}")
    sq_b = [(r['actual']-r['proj'])**2 for r in base]
    sq_r = [(r['actual']-r['proj'])**2 for r in withret]
    z, p = paired_z(sq_b, sq_r)
    print(f"  paired test on squared error (base - withreturn): z={z:+.3f}  p={p:.3g}  "
          f"(positive z favours the return feature)")
    print(f"  pairwise evaluated on {nb} / {nr} orderable pairs")

    print(f"\n  {'pos':4s} {'RMSE base':>10s} {'RMSE +ret':>10s} {'Sp base':>8s} {'Sp +ret':>8s} "
          f"{'pair base':>10s} {'pair +ret':>10s} {'d(pair)':>8s}")
    for pos in ("QB", "RB", "WR", "TE"):
        idx = [i for i, r in enumerate(base) if r["position"] == pos]
        if not idx: continue
        b = [base[i] for i in idx]; w = [withret[i] for i in idx]
        pb = [(r["proj"], r["actual"]) for r in b]; pw = [(r["proj"], r["actual"]) for r in w]
        (ab, _), (aw, _) = pairwise(b, w)
        print(f"  {pos:4s} {math.sqrt(statistics.fmean((a-p)**2 for p,a in pb)):10.4f} "
              f"{math.sqrt(statistics.fmean((a-p)**2 for p,a in pw)):10.4f} "
              f"{spearman(pb):8.4f} {spearman(pw):8.4f} {ab:10.4f} {aw:10.4f} {aw-ab:+8.4f}")
