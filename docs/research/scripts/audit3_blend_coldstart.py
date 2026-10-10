"""Audit III follow-up (2026-09-14): does blend.py really degenerate at one
game of in-season history? MEASURED ANSWER: no, it does not.

The league-2 week-2 briefing committed to main on 2026-09-14 reports that
generate_report.py had to be run with --skip-blend because "the rolling-volume
+Vegas blend collapsed the whole player pool to implausible near-zero
conditional points with only one game of 2026 history (top WR in the entire
pool projected 6.0 pts, top TE 4.1)". This script checks that claim against the
data instead of relaying it.

RESULT: it does not reproduce. Projecting real 2026 week 2 for the 332 players
with a week-1 game logged, calibration fitted on 2022-2025:

    pos    n   rolling max   blend max   rolling mean   blend mean   blend<=0
    QB    35         23.28       25.71          15.81        14.88          0
    RB    83         21.10       23.37           8.30         7.93          0
    WR   140         18.54       18.88           6.80         6.92          0
    TE    74         13.85       16.21           5.35         5.25          0

The blend tracks the rolling average within ~0.15 pts on the mean at every
position, its maxima are higher not lower, and nothing is non-positive.

WHAT THE REPORTED SYMPTOM ACTUALLY LOOKS LIKE. An earlier version of this
script projected week 1 by mistake (no prior games in scope). That run produced
exactly the reported signature: the ENTIRE pool flat at the positional
baseline -- every WR 6.65, every TE 5.60 -- with the blend shrinking those to
5.73 and 4.29. Those are within rounding of the briefing's "top WR 6.0, top TE
4.1". A whole pool collapsing to near-identical near-baseline values is the
signature of EMPTY in-season history, not of the blend: with no prior games the
rolling average has nothing to average and every player falls to the same
positional mean. That failure mode hits the --skip-blend path too, so disabling
the blend would not fix it.

SO THE LIKELY REAL CAUSE is that the briefing run's game logs did not contain
2026 week 1 at projection time -- an as-of/data-loading question in
generate_report's own loop, not a blend defect. Worth confirming before the
next blend-eligible week, because audit III measured the blend as worth
RMSE -0.21 on held-out 2025 for league-2 (6.379 -> 6.169), so leaving it off
has a real cost.

CAVEAT, stated because it bounds the claim: this run fits calibration on
2022-2025 and loads logs via calibration_fit.load_game_logs([2025, 2026]),
which demonstrably DID include 2026 week 1 (332 players). The briefing run
fitted "fresh this run on 2021-2025" and its exact inputs are not visible from
here, so this shows the blend is sound under these inputs rather than proving
what happened in that run.

Run: .venv/bin/python docs/research/scripts/audit3_blend_coldstart.py
"""
import sys, os, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import league_config  # noqa: F401

import calibration_fit, blend as blend_module

SEASON, WEEK = 2026, 2
TRAIN = [2022, 2023, 2024, 2025]
cfg = league_config("league-2")

bundle = calibration_fit.fit_from_history(cfg, SEASON, TRAIN)
bm = bundle["blend_model"]

logs = calibration_fit.load_game_logs([SEASON - 1, SEASON], scoring_config=cfg)
ctx = calibration_fit._load_context([SEASON]).get(SEASON)
# _walk_forward_rows cannot be used here: it only emits a row for a week the
# player actually PLAYED, and week 2 has not been played. Build the week-2
# feature rows directly, the same way generate_report's own loop does.
import baseline as baseline_module
from projections import project_player, DEFAULT_WINDOW, DEFAULT_DECAY
import calibration as calibration_module

season_logs = {p: [g for g in log if g["season"] in (SEASON, SEASON - 1)]
               for p, log in logs.items()}
season_logs = {p: log for p, log in season_logs.items() if log}
baselines = baseline_module.positional_baselines_by_week(
    season_logs, cfg, SEASON, [WEEK], window=DEFAULT_WINDOW, population="all")

rows = []
for pid, log in season_logs.items():
    pos = next((g.get("position") for g in log if g.get("position")), None)
    if not pos:
        continue
    prior = [g for g in log if g["season"] == SEASON and g.get("week", 0) < WEEK]
    if not prior:                      # no 2026 game yet -> genuine cold start
        continue
    pb = (baselines.get(WEEK, {}).get(pos) or {}).get("baseline")
    projection = project_player(
        log, cfg, SEASON, WEEK, window=DEFAULT_WINDOW, decay=DEFAULT_DECAY,
        positional_baseline=pb,
        shrinkage_k=calibration_module.DEFAULT_SHRINKAGE_K.get(pos))
    volume = blend_module.rolling_volume(log, SEASON, WEEK, DEFAULT_WINDOW, DEFAULT_DECAY)
    team = next((g.get("team") for g in prior), None)
    context = (ctx or {}).get(WEEK, {}).get(team)
    row = blend_module.build_feature_row(pid, pos, projection, volume, context)
    row["projected"] = projection["projected_points"]
    rows.append(row)
print(f"\n2026 players with a week-1 game, projected for week {WEEK}: {len(rows)}")

by_pos = {}
for r in rows:
    pos = r["position"]
    raw = r["projected"]
    blended = bm.predict_one(r) if bm is not None and pos in bm.fitted_positions else raw
    by_pos.setdefault(pos, []).append((raw, blended, r.get("games_used")))

print(f"\nProjections for week {WEEK} off ONE game of 2026 history")
print(f"{'pos':4s} {'n':>4s} {'rolling max':>12s} {'blend max':>11s} "
      f"{'rolling mean':>13s} {'blend mean':>11s} {'blend<=0':>9s}")
for pos in ("QB", "RB", "WR", "TE"):
    v = by_pos.get(pos)
    if not v:
        continue
    raws = [a for a, b, g in v]; bl = [b for a, b, g in v]
    print(f"{pos:4s} {len(v):4d} {max(raws):12.2f} {max(bl):11.2f} "
          f"{statistics.fmean(raws):13.2f} {statistics.fmean(bl):11.2f} "
          f"{sum(1 for x in bl if x <= 0):9d}")

print("\nTop 5 by each method, WR and TE (the two the briefing named):")
for pos in ("WR", "TE"):
    v = by_pos.get(pos) or []
    print(f"  {pos} rolling: " + ", ".join(f"{a:.1f}" for a, b, g in sorted(v, key=lambda t: -t[0])[:5]))
    print(f"  {pos} blend:   " + ", ".join(f"{b:.1f}" for a, b, g in sorted(v, key=lambda t: -t[1])[:5]))
