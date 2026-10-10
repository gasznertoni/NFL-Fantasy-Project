"""Audit III: NaN reachability probes.

CLAUDE.md records three shipped NaN defects (v16 `if not player_id`, v18
`if not val`, v19 `home_score is None`). This checks the guards that still use
`is None` or bare truthiness against feed data, and establishes whether each is
LIVE (fires on real data today) or LATENT (correct only because the data
happens to be clean).

Run: .venv/bin/python docs/research/scripts/audit3_nan_probes.py
"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import league_config  # noqa: F401
import nflreadpy as nfl
import dst
from scoring import compute_league_points

cfg = league_config("league-1")
NAN = float("nan")

print("PROBE 1 -- dst._points_allowed_for_team_game / build_dst_game_logs")
print("  guard: `if points_allowed is None: continue`  (v19's exact shape)")
sched = nfl.load_schedules(seasons=[2026])
sched = sched.to_pandas() if hasattr(sched, "to_pandas") else sched
sched = sched[sched["game_type"] == "REG"]
missing = int(sched["home_score"].isna().sum())
print(f"  2026 REG schedule rows: {len(sched)}, home_score NaN on {missing}")
row = sched.iloc[0].to_dict()
g = {"game_id": row["game_id"], "home_team": row["home_team"], "away_team": row["away_team"],
     "home_score": row.get("home_score"), "away_score": row.get("away_score")}
pa = dst._points_allowed_for_team_game(g, row["home_team"])
print(f"  _points_allowed_for_team_game -> {pa!r};  `is None` catches it: {pa is None};  "
      f"`!= itself` catches it: {pa != pa if pa is not None else 'n/a'}")
if pa is not None and pa != pa:
    print("  => LATENT DEFECT: a NaN score passes the `is None` guard.")
    sl = dst.assemble_dst_stat_line({"def_sacks": 3}, {"passing_yards": 200, "rushing_yards": 100}, pa)
    res = compute_league_points(sl, cfg)
    print(f"     resulting stat line def_points_allowed={sl['def_points_allowed']!r}")
    print(f"     scored total={res.total}  breakdown keys={sorted(res.breakdown)}")
    print(f"     -> the points-allowed TIER is silently ABSENT rather than raising"
          f" (worth up to {max(b['points'] for b in cfg['tiers']['def_points_allowed'])} pts).")
else:
    print("  => guard is adequate on today's feed.")

print("\nPROBE 2 -- scoring._tier_points with a NaN tier input")
for val in (NAN, None, 0.0, 24.0):
    r = compute_league_points({"def_points_allowed": val}, cfg)
    print(f"  def_points_allowed={val!r:>6} -> total={r.total:+6.2f}  "
          f"scored={'def_points_allowed' in r.breakdown}")

print("\nPROBE 3 -- dst.assemble_dst_stat_line / _sum_columns with NaN cells")
sl = dst.assemble_dst_stat_line(
    {"def_sacks": NAN, "punt_return_yards": NAN, "kickoff_return_yards": 20},
    {"passing_yards": NAN, "rushing_yards": 100}, 17.0)
print(f"  stat line: { {k: v for k, v in sl.items()} }")
r = compute_league_points(sl, cfg)
print(f"  scored total={r.total}  isnan={math.isnan(r.total)}  breakdown={sorted(r.breakdown)}")
print("  (scoring._linear_points guards NaN with `raw == raw`, so a NaN linear cell is")
print("   dropped; _tier_points has NO such guard, so a NaN tier input scores nothing.)")

print("\nPROBE 4 -- position on assembled stat lines")
import kicker
print(f"  dst.assemble_dst_stat_line sets position: "
      f"{'position' in dst.assemble_dst_stat_line({}, {}, 0)}")
print(f"  kicker.nflreadpy_kicker_row_to_stat_line sets position: "
      f"{'position' in kicker.nflreadpy_kicker_row_to_stat_line({'position': 'K', 'pat_made': 1})}")
print("  scoring.py docstring and league-1's _linear_notes both assert dst does.")
