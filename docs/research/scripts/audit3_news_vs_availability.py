"""Audit III, Task 1.5: does the news layer agree with the availability model?

CLAUDE.md's framing: "A news layer that disagrees with the availability model
on a starter is itself a finding." Both are already published side by side in
every shipped report -- newsFlag.riskLevel/designation from news.py, and
projection.playProbability from availability.py -- so the agreement can be
measured directly off the fixtures, with no network and no LLM spend.

Run: .venv/bin/python docs/research/scripts/audit3_news_vs_availability.py
"""
import sys, os, json, statistics
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import REPO_ROOT

MOCK = os.path.join(REPO_ROOT, "frontend", "public", "mock")

for league in ("league-1", "league-2"):
    print(f"\n{'='*74}\n{league}\n{'='*74}")
    by_risk = defaultdict(list)
    by_desig = defaultdict(list)
    weeks = 0
    for wk in range(1, 19):
        path = os.path.join(MOCK, league, f"weekly-report-week-{wk}.json")
        if not os.path.exists(path):
            continue
        weeks += 1
        for p in json.load(open(path))["projections"]:
            nf = p.get("newsFlag") or {}
            pp = p["projection"].get("playProbability")
            if pp is None:
                continue
            by_risk[nf.get("riskLevel")].append(pp)
            by_desig[nf.get("designation")].append(pp)
    print(f"({weeks} weekly reports)")
    print("\nP(play) by news riskLevel:")
    for k, v in sorted(by_risk.items(), key=lambda kv: -len(kv[1])):
        print(f"  {str(k):10s} n={len(v):6d} mean P(play)={statistics.fmean(v):.3f} "
              f"min={min(v):.3f} max={max(v):.3f}")
    print("\nP(play) by news designation:")
    for k, v in sorted(by_desig.items(), key=lambda kv: -len(kv[1])):
        print(f"  {str(k):14s} n={len(v):6d} mean P(play)={statistics.fmean(v):.3f} "
              f"min={min(v):.3f} max={max(v):.3f}")

    # Disagreements: healthy-flagged players the availability model doubts,
    # and risk-flagged players the availability model is confident about.
    path = os.path.join(MOCK, league, "weekly-report-week-1.json")
    proj = json.load(open(path))["projections"]
    healthy_but_doubted = [p for p in proj
                           if (p.get("newsFlag") or {}).get("riskLevel") in (None, "none")
                           and (p["projection"].get("playProbability") or 1) < 0.75]
    flagged_but_confident = [p for p in proj
                             if (p.get("newsFlag") or {}).get("riskLevel") in ("high", "medium")
                             and (p["projection"].get("playProbability") or 0) > 0.90]
    print(f"\nWeek 1 disagreements:")
    print(f"  news says healthy, availability P(play) < 0.75 : {len(healthy_but_doubted)}")
    for p in sorted(healthy_but_doubted, key=lambda x: x["projection"]["playProbability"])[:12]:
        print(f"     {p['name'][:26]:26s} {p['position']:3s} P(play)={p['projection']['playProbability']:.2f} "
              f"cond={p['projection'].get('conditionalPoints')}")
    print(f"  news flags risk, availability P(play) > 0.90   : {len(flagged_but_confident)}")
    for p in flagged_but_confident[:12]:
        print(f"     {p['name'][:26]:26s} {p['position']:3s} P(play)={p['projection']['playProbability']:.2f} "
              f"risk={(p.get('newsFlag') or {}).get('riskLevel')}")
