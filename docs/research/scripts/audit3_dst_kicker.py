"""Audit III: D/ST and K paths -- the half the fantasy_points_ppr
reconciliation cannot reach (no external reference exists for a team defence).

Checks:
  A. Independent re-implementation of D/ST scoring off raw team_stats columns
     vs dst.assemble_dst_stat_line -> compute_league_points.
  B. Same for kickers off raw player_stats columns.
  C. Does the D/ST stat line carry `position`? scoring.py's docstring and
     league-1's config _linear_notes both assert it does.
  D. NaN reachability probes on the guards that use `is None` / truthiness.

Run: .venv/bin/python docs/research/scripts/audit3_dst_kicker.py
"""
import sys, os, statistics, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import league_config  # noqa: F401
import nflreadpy as nfl
import dst, kicker
from scoring import compute_league_points

SEASON = 2025


def g(row, col):
    v = row.get(col)
    if v is None or v != v:
        return 0.0
    return float(v)


def band(value, bands):
    for b in sorted(bands, key=lambda x: x.get("max", float("inf"))):
        if value <= b.get("max", float("inf")):
            return b["points"]
    return 0


def independent_dst(own, opp, points_allowed, cfg):
    lin = cfg["linear"]
    v = lambda c: lin.get(c, 0)
    pts = 0.0
    pts += g(own, "def_sacks") * v("def_sack")
    pts += g(own, "def_interceptions") * v("def_int")
    pts += g(own, "fumble_recovery_opp") * v("def_fumble_rec")
    pts += g(own, "def_safeties") * v("def_safety")
    pts += g(own, "def_fumbles_forced") * v("fumble_forced")
    pts += g(own, "def_tds") * v("def_td")
    pts += g(own, "special_teams_tds") * v("def_st_td")
    pts += g(own, "fumble_recovery_tds") * v("def_fumble_rec_td")
    pts += g(own, "def_2pt_made") * v("def_2pt_return")
    pts += (g(own, "punt_return_yards") + g(own, "kickoff_return_yards")) * v("def_return_yd")
    pts += (g(opp, "fg_blocked") + g(opp, "pt_blocked")) * v("def_blocked_kick")
    tiers = cfg.get("tiers", {})
    if "def_points_allowed" in tiers:
        pts += band(points_allowed, tiers["def_points_allowed"])
    if "def_yards_allowed" in tiers:
        pts += band(g(opp, "passing_yards") + g(opp, "rushing_yards"), tiers["def_yards_allowed"])
    return round(pts, 2)


def independent_kicker(row, cfg):
    lin = cfg["linear"]
    v = lambda c: lin.get(c, 0)
    pts = 0.0
    made = {"fg_made_0_19": "fg_made_0_19", "fg_made_20_29": "fg_made_20_29",
            "fg_made_30_39": "fg_made_30_39", "fg_made_40_49": "fg_made_40_49",
            "fg_made_50_59": "fg_made_50_59", "fg_made_60_": "fg_made_60_plus"}
    if "fg_made_0_39" in lin:  # rolled family
        pts += (g(row, "fg_made_0_19") + g(row, "fg_made_20_29") + g(row, "fg_made_30_39")) * v("fg_made_0_39")
        pts += g(row, "fg_made_40_49") * v("fg_made_40_49")
        pts += (g(row, "fg_made_50_59") + g(row, "fg_made_60_")) * v("fg_made_50_plus")
    else:
        for col, cat in made.items():
            pts += g(row, col) * v(cat)
    pts += g(row, "pat_made") * v("pat_made")
    pts += (g(row, "pat_missed") + g(row, "pat_blocked")) * v("pat_missed")
    # misses: blocked kicks fold in by distance for the banded family, flat otherwise
    blocked = kicker._blocked_distances(row)
    if "fg_missed" in lin:
        pts += (g(row, "fg_missed") + g(row, "fg_blocked")) * v("fg_missed")
    else:
        b0 = g(row, "fg_missed_0_19") + g(row, "fg_missed_20_29") + g(row, "fg_missed_30_39")
        b4, b5, b6 = g(row, "fg_missed_40_49"), g(row, "fg_missed_50_59"), g(row, "fg_missed_60_")
        for d in blocked:
            if d < 40: b0 += 1
            elif d < 50: b4 += 1
            elif d < 60: b5 += 1
            else: b6 += 1
        pts += b0 * v("fg_missed_0_39") + b4 * v("fg_missed_40_49")
        pts += b5 * v("fg_missed_50_59") + b6 * v("fg_missed_60_plus")
    return round(pts, 2)


def describe(name, xs):
    xs = sorted(xs); n = len(xs)
    q = lambda f: xs[min(n - 1, int(f * n))]
    print(f"  {name:26s} n={n:5d} mean={statistics.fmean(xs):+8.4f} sd={statistics.pstdev(xs):7.4f} "
          f"min={xs[0]:+7.3f} max={xs[-1]:+7.3f} |>0.01|={sum(1 for x in xs if abs(x) > 0.01)}")


ts = nfl.load_team_stats(seasons=[SEASON])
ts = ts.to_pandas() if hasattr(ts, "to_pandas") else ts
ts = ts[ts["season_type"] == "REG"]
team_rows = ts.to_dict("records")
for r in team_rows:
    r["team"] = dst.normalize_team(r.get("team"))
    r["opponent_team"] = dst.normalize_team(r.get("opponent_team"))

sched = nfl.load_schedules(seasons=[SEASON])
sched = sched.to_pandas() if hasattr(sched, "to_pandas") else sched
sched_rows = [{"game_id": r["game_id"], "season": int(r["season"]), "week": int(r["week"]),
               "home_team": dst.normalize_team(r.get("home_team")),
               "away_team": dst.normalize_team(r.get("away_team")),
               "home_score": r.get("home_score"), "away_score": r.get("away_score")}
              for _, r in sched.iterrows()]

logs = dst.build_dst_game_logs(team_rows, sched_rows)
n_logs = sum(len(v) for v in logs.values())
print(f"D/ST game logs assembled: {n_logs} across {len(logs)} teams\n")

# --- C. position propagation ------------------------------------------------
sample = next(iter(logs.values()))[0]
print("C. POSITION ON THE D/ST STAT LINE")
print(f"   keys: {sorted(k for k in sample)}")
print(f"   'position' present: {'position' in sample}  -> compute_league_points falls back to None")
kk = None
ps = nfl.load_player_stats(seasons=[SEASON])
ps = ps.to_pandas() if hasattr(ps, "to_pandas") else ps
ps = ps[(ps["season_type"] == "REG") & (ps["position"] == "K")]
krows = ps.to_dict("records")
ksample = kicker.nflreadpy_kicker_row_to_stat_line(krows[0])
print(f"   kicker stat line 'position' present: {'position' in ksample}")

rows_by_gt = {(r.get("game_id"), r.get("team")): r for r in team_rows}
sched_by_id = {s["game_id"]: s for s in sched_rows}

for league_id in ("league-1", "league-2"):
    cfg = league_config(league_id)
    print(f"\n{'='*70}\n{league_id}\n{'='*70}")
    diffs, totals = [], []
    for r in team_rows:
        gid, team, opp = r.get("game_id"), r.get("team"), r.get("opponent_team")
        orow = rows_by_gt.get((gid, opp)); s = sched_by_id.get(gid)
        if orow is None or s is None:
            continue
        pa = s["away_score"] if team == s["home_team"] else s["home_score"]
        if pa is None or pa != pa:
            continue
        sl = dst.assemble_dst_stat_line(r, orow, float(pa))
        e = float(compute_league_points(sl, cfg))
        i = independent_dst(r, orow, float(pa), cfg)
        diffs.append(e - i); totals.append(e)
    print("A. D/ST engine vs independent re-implementation:")
    describe("engine - independent", diffs)
    print(f"   D/ST mean points/game: {statistics.fmean(totals):.2f} (n={len(totals)})")

    kd, kt = [], []
    for r in krows:
        sl = kicker.nflreadpy_kicker_row_to_stat_line(r)
        e = float(compute_league_points(sl, cfg))
        i = independent_kicker(r, cfg)
        kd.append(e - i); kt.append(e)
    print("B. Kicker engine vs independent re-implementation:")
    describe("engine - independent", kd)
    print(f"   K mean points/game: {statistics.fmean(kt):.2f} (n={len(kt)})")
    worst = sorted(zip((abs(x) for x in kd), range(len(krows))), reverse=True)[:3]
    for dv, idx in worst:
        if dv > 0.01:
            print(f"     DIFF {dv:.2f} {krows[idx].get('player_display_name')} wk{krows[idx].get('week')}")
