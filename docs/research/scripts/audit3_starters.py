"""Audit III, Task 2 (reduced scope -- see the doc's limitations section):
the starter-by-starter projection table for both leagues.

Sleeper, ESPN's fantasy API and FantasyPros' API are ALL blocked by this
session's egress policy, so the live three-vendor pull could not be run. What
this script does instead, from data already on disk:

  * our own conditional and expected (P(play)-weighted) projections and the
    published 10-90 band, for every starter on both rosters, week 1 2026;
  * which tier produced each number, and -- where the player fell inside the
    consensus tier -- the VENDOR-derived projection (FantasyPros + Rotowire
    raw stat lines, re-scored through this league's own config by the shipped
    pipeline at report-generation time);
  * ESPN's own in-app per-game projections for the eleven players CLAUDE.md
    records the builder capturing by hand on 2026-09-06.

Run: .venv/bin/python docs/research/scripts/audit3_starters.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import REPO_ROOT

MOCK = os.path.join(REPO_ROOT, "frontend", "public", "mock")

# ESPN's own in-app projections, as recorded in CLAUDE.md v20 (builder capture,
# 2026-09-06). These are ESPN's per-game numbers under league-1's settings.
ESPN_INAPP = {
    "Jaxson Dart": 20.7, "Javonte Williams": 18.0, "Jaxon Smith-Njigba": 19.3,
    "Tyler Warren": 14.6, "Baker Mayfield": 18.2,
}
ESPN_LEAGUE1_STARTER_SUM = 118.5


def load(league, week=1):
    d = json.load(open(os.path.join(MOCK, league, f"weekly-report-week-{week}.json")))
    by_id = {p["playerId"]: p for p in d["projections"]}
    # league-2's roster.json carries no playerId (league-1's does), so fall back
    # to (name, position) -- itself a finding, recorded in the write-up.
    by_name = {(p["name"], p["position"]): p for p in d["projections"]}
    return by_id, by_name, d


def find(e, by_id, by_name):
    if e.get("playerId") and e["playerId"] in by_id:
        return by_id[e["playerId"]]
    return by_name.get((e["name"], e["position"]))


for league in ("league-1", "league-2"):
    roster = json.load(open(os.path.join(REPO_ROOT, "backend", "leagues", league, "roster.json")))
    by_id, by_name, report = load(league)
    print(f"\n{'='*112}\n{league} -- {report['leagueFormatAssumption']}, week {report['week']}, "
          f"generated {report['generatedAt']}\n{'='*112}")
    hdr = (f"{'slot':6s} {'player':24s} {'pos':4s} {'tm':4s} {'tier':12s} "
           f"{'cond':>6s} {'exp':>6s} {'P(play)':>8s} {'10th':>6s} {'90th':>6s} "
           f"{'ESPN':>6s} {'gap':>7s}")
    print(hdr); print("-" * len(hdr))
    tot_cond = tot_exp = 0.0
    for group, label in ((roster["starters"], "START"), (roster.get("bench", []), "BENCH")):
        if label == "BENCH":
            print("-" * len(hdr))
        for e in group:
            p = find(e, by_id, by_name)
            slot = e.get("slot", "BN")
            if p is None:
                print(f"{slot:6s} {e['name'][:24]:24s} {e['position']:4s} {e.get('team',''):4s} "
                      f"{'NOT IN REPORT':>12s}")
                continue
            pr = p["projection"]
            cond = pr.get("conditionalPoints")
            espn = ESPN_INAPP.get(e["name"]) if league == "league-1" else None
            gap = (cond - espn) if (espn is not None and cond is not None) else None
            if label == "START":
                tot_cond += cond or 0; tot_exp += pr.get("points") or 0
            print(f"{slot:6s} {e['name'][:24]:24s} {e['position']:4s} {e.get('team',''):4s} "
                  f"{pr.get('tier',''):12s} {cond if cond is not None else float('nan'):6.2f} "
                  f"{pr.get('points',float('nan')):6.2f} {pr.get('playProbability',float('nan')):8.2f} "
                  f"{pr.get('floor',float('nan')):6.2f} {pr.get('ceiling',float('nan')):6.2f} "
                  f"{espn if espn is not None else float('nan'):6.2f} "
                  f"{gap if gap is not None else float('nan'):+7.2f}")
    print("-" * len(hdr))
    print(f"{'':6s} {'STARTER TOTAL':24s} {'':4s} {'':4s} {'':12s} {tot_cond:6.2f} {tot_exp:6.2f}")
    if league == "league-1":
        print(f"{'':6s} {'ESPN in-app starter sum':24s} {'':4s} {'':4s} {'':12s} "
              f"{ESPN_LEAGUE1_STARTER_SUM:6.2f}   -> our conditional sum is "
              f"{tot_cond - ESPN_LEAGUE1_STARTER_SUM:+.2f} vs ESPN")

# How many of each roster's players fell inside the consensus (vendor) tier?
print(f"\n{'='*80}\nVendor (consensus-tier) coverage of the two rosters, week 1\n{'='*80}")
for league in ("league-1", "league-2"):
    roster = json.load(open(os.path.join(REPO_ROOT, "backend", "leagues", league, "roster.json")))
    by_id, by_name, _ = load(league)
    ids = [e for e in roster["starters"] + roster.get("bench", [])]
    cons = [e["name"] for e in ids
            if ((find(e, by_id, by_name) or {}).get("projection", {}) or {}).get("tier") == "consensus"]
    print(f"  {league}: {len(cons)}/{len(ids)} on the roster reached the vendor tier"
          f"{': ' + ', '.join(cons) if cons else ''}")
