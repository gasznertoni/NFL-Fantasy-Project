"""Audit III, Task 1.4: walk-forward calibration and prediction-interval
coverage, trained on prior seasons only.

Design (held-out by construction):
  train  2022-2024  -> shrinkage k, affine, interval model, blend model
  test   2025       -> every played player-week, projected as of that week

Reports RMSE, MAE, pairwise start/sit accuracy and Spearman TOGETHER, per
CLAUDE.md: "a variant that improves one while degrading the order is a
failure, and this repo has been burned by exactly that."

Interval coverage is checked two ways:
  (a) CONDITIONAL, on real held-out 2025 played weeks (nominal 80%);
  (b) MIXTURE, by Monte Carlo against the same fitted conditional curve --
      the v18 fix claims Q(t)=0 for t<=1-p, and that claim is checkable
      exactly, independent of the regression tests that ship with it.

Run: .venv/bin/python docs/research/scripts/audit3_walkforward.py
"""
import sys, os, math, random, statistics, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import league_config  # noqa: F401

import calibration as calibration_module
import calibration_fit
import blend as blend_module
from scoring import compute_league_points

TRAIN = [2022, 2023, 2024]
TEST = 2025
random.seed(20260909)


def rmse(pairs):
    return math.sqrt(statistics.fmean((a - p) ** 2 for p, a in pairs))


def mae(pairs):
    return statistics.fmean(abs(a - p) for p, a in pairs)


def spearman(pairs):
    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    p = ranks([x[0] for x in pairs]); a = ranks([x[1] for x in pairs])
    mp, ma = statistics.fmean(p), statistics.fmean(a)
    num = sum((x - mp) * (y - ma) for x, y in zip(p, a))
    den = math.sqrt(sum((x - mp) ** 2 for x in p) * sum((y - ma) ** 2 for y in a))
    return num / den if den else float("nan")


def pairwise_accuracy(rows, n_pairs=200000):
    """Within a (position, week), how often does the higher projection also
    score higher? Exactly the start/sit question."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        groups[(r["position"], r["week"])].append((r["proj"], r["actual"]))
    pairs = []
    for v in groups.values():
        for i in range(len(v)):
            for j in range(i + 1, len(v)):
                pairs.append((v[i], v[j]))
    if len(pairs) > n_pairs:
        pairs = random.sample(pairs, n_pairs)
    good = tot = 0
    for (p1, a1), (p2, a2) in pairs:
        if p1 == p2 or a1 == a2:
            continue
        tot += 1
        good += (p1 > p2) == (a1 > a2)
    return (good / tot if tot else float("nan")), tot


def main():
    for league_id in ("league-1", "league-2"):
        cfg = league_config(league_id)
        print(f"\n{'#'*78}\n# {league_id}\n{'#'*78}")

        bundle = calibration_fit.fit_from_history(cfg, TEST, TRAIN)
        interval_model = bundle["interval_model"]
        blend_model = bundle["blend_model"]

        # held-out season rows, projected as of each week
        logs = calibration_fit.load_game_logs([TEST - 1, TEST], scoring_config=cfg)
        ctx = calibration_fit._load_context([TEST]).get(TEST)
        test_rows = calibration_fit._walk_forward_rows(
            logs, cfg, TEST, calibration_fit.DEFAULT_WINDOW,
            calibration_fit.DEFAULT_DECAY, ctx)
        print(f"held-out {TEST} played player-weeks: {len(test_rows)}")

        # Reproduce the shipped stack: raw rolling -> blend -> affine.
        affine_by_pos = {}
        for pid, aff in bundle["affines"].items():
            pos = next((g.get("position") for g in logs.get(pid, []) if g.get("position")), None)
            if pos:
                affine_by_pos[pos] = aff

        variants = {"raw (rolling+shrinkage)": [], "+blend": [], "+blend+affine (SHIPPED)": []}
        for r in test_rows:
            pos, raw = r["position"], r["projected"]
            blended = raw
            if blend_model is not None and pos in blend_model.fitted_positions:
                blended = blend_model.predict_one(r)
            aff = affine_by_pos.get(pos)
            final = blended if not aff else aff[0] + aff[1] * blended
            base = dict(position=pos, week=r["week"], actual=r["actual_points"])
            variants["raw (rolling+shrinkage)"].append({**base, "proj": raw})
            variants["+blend"].append({**base, "proj": blended})
            variants["+blend+affine (SHIPPED)"].append({**base, "proj": final})

        print(f"\n{'variant':26s} {'RMSE':>7s} {'MAE':>7s} {'pairwise':>9s} {'Spearman':>9s}")
        for name, rows in variants.items():
            pairs = [(r["proj"], r["actual"]) for r in rows]
            acc, n = pairwise_accuracy(rows)
            print(f"{name:26s} {rmse(pairs):7.4f} {mae(pairs):7.4f} {acc:9.4f} {spearman(pairs):9.4f}")

        shipped = variants["+blend+affine (SHIPPED)"]
        print(f"\nby position (SHIPPED stack):")
        print(f"  {'pos':4s} {'n':>5s} {'RMSE':>7s} {'MAE':>7s} {'pairwise':>9s} {'Spearman':>9s} {'bias':>7s}")
        for pos in ("QB", "RB", "WR", "TE"):
            sub = [r for r in shipped if r["position"] == pos]
            if not sub:
                continue
            pairs = [(r["proj"], r["actual"]) for r in sub]
            acc, _ = pairwise_accuracy(sub)
            bias = statistics.fmean(a - p for p, a in pairs)
            print(f"  {pos:4s} {len(sub):5d} {rmse(pairs):7.4f} {mae(pairs):7.4f} {acc:9.4f} "
                  f"{spearman(pairs):9.4f} {bias:+7.3f}")

        # ---- (a) CONDITIONAL interval coverage on held-out 2025 -----------
        print(f"\nINTERVAL COVERAGE (a): conditional band, nominal "
              f"{interval_model.quantiles[1]-interval_model.quantiles[0]:.0%}, held-out {TEST}")
        tot = cov = 0
        by_pos = {}
        for r in shipped:
            band = interval_model.interval(r["position"], r["proj"])
            if band is None:
                continue
            lo, hi = band
            ok = lo <= r["actual"] <= hi
            tot += 1; cov += ok
            d = by_pos.setdefault(r["position"], [0, 0]); d[0] += 1; d[1] += ok
        print(f"  pooled: {cov}/{tot} = {cov/tot:.3f}")
        for pos, (n, c) in sorted(by_pos.items()):
            print(f"    {pos}: {c}/{n} = {c/n:.3f}")

        # ---- (b) MIXTURE band, Monte-Carlo verification --------------------
        print(f"\nINTERVAL COVERAGE (b): mixture band vs Monte-Carlo truth")
        print(f"  {'pos':4s} {'p':>5s} {'floor':>7s} {'ceil':>7s} {'MC cover':>9s} {'target':>7s}")
        # Draw conditional outcomes from the fitted quantile curve by inverse
        # transform, then zero them out with probability 1-p.
        for pos in ("RB", "WR"):
            for p in (1.0, 0.95, 0.90, 0.85, 0.70, 0.50):
                proj = 10.0
                band = interval_model.interval(pos, proj, play_probability=p)
                if band is None:
                    continue
                lo, hi = band
                hits = 0; N = 40000
                for _ in range(N):
                    if random.random() > p:
                        outcome = 0.0
                    else:
                        u = random.random()
                        off = interval_model.quantile_offset(pos, proj, u)
                        outcome = proj + off
                    hits += lo <= outcome <= hi
                print(f"  {pos:4s} {p:5.2f} {lo:7.2f} {hi:7.2f} {hits/N:9.3f} {0.80:7.2f}")


if __name__ == "__main__":
    main()
