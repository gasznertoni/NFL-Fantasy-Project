"""Audit III, Task 1.3: reconcile each league's engine against an EXTERNAL
reference (nflreadpy's own fantasy_points_ppr), per CLAUDE.md's standing rule
that an internal backtest hides a scoring bug on both sides.

Two independent checks, both on 2025 regular-season QB/RB/WR/TE actuals:

  A. INDEPENDENT RE-IMPLEMENTATION. Score every player-game straight off the
     raw nflreadpy columns with a scorer written here from the league's config
     -- deliberately NOT importing scoring.py's column map. Any disagreement
     with compute_league_points() is an engine or map defect.

  B. EXTERNAL RECONCILIATION. Rebuild nflverse's own standard-PPR total from
     raw columns (verified against the shipped fantasy_points_ppr column), then
     compare league_points - ppr against the analytic delta implied by the two
     rule sets. Residual distribution reported, not just a mean.

Run: .venv/bin/python docs/research/scripts/audit3_reconcile.py
"""
import sys, os, json, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import league_config  # noqa: F401
import nflreadpy as nfl
from scoring import compute_league_points, nflreadpy_row_to_stat_line

SEASON = 2025
POSITIONS = ("QB", "RB", "WR", "TE")


def g(row, col):
    """Raw column read, NaN/None -> 0.0 (the v16/v18/v19 trap: NaN is truthy)."""
    v = row.get(col)
    if v is None or v != v:
        return 0.0
    return float(v)


# ---------------------------------------------------------------------------
# A. Independent scorer, written from each league's JSON, off RAW columns only.
# ---------------------------------------------------------------------------
def independent_score(row, cfg):
    pos = row.get("position")
    lin = cfg["linear"]

    def v(cat):
        """Resolve a possibly position-scoped config value."""
        x = lin.get(cat, 0)
        if isinstance(x, dict):
            return x.get(pos, x.get("default", 0))
        return x

    att = g(row, "attempts")
    cmp_ = g(row, "completions")
    inc = max(0.0, att - cmp_)
    py, ry, cy = g(row, "passing_yards"), g(row, "rushing_yards"), g(row, "receiving_yards")
    rec = g(row, "receptions")

    pts = 0.0
    pts += py * v("pass_yd") + cmp_ * v("pass_completion") + inc * v("pass_incompletion")
    pts += g(row, "passing_tds") * v("pass_td") + g(row, "passing_interceptions") * v("pass_int")
    pts += g(row, "passing_2pt_conversions") * v("pass_2pt")
    pts += g(row, "sacks_suffered") * v("pass_sacked")
    pts += ry * v("rush_yd") + g(row, "rushing_tds") * v("rush_td")
    pts += g(row, "rushing_first_downs") * v("rush_first_down")
    pts += g(row, "rushing_2pt_conversions") * v("rush_2pt")
    pts += cy * v("rec_yd") + g(row, "receiving_tds") * v("rec_td")
    pts += g(row, "receiving_first_downs") * v("rec_first_down")
    pts += g(row, "receiving_2pt_conversions") * v("rec_2pt")
    pts += rec * v("reception")
    pts += g(row, "special_teams_tds") * v("return_td")
    pts += g(row, "kickoff_return_yards") * v("kick_return_yd")
    pts += g(row, "punt_return_yards") * v("punt_return_yd")
    pts += g(row, "fumbles_total") * v("fumble") + g(row, "fumbles_lost_total") * v("fumble_lost")
    pts += g(row, "fumble_recovery_tds") * v("fumble_recovery_td")

    # milestones, "highest" mode, position-scoped points
    for cat, raw in (("pass_yd", py), ("rush_yd", ry), ("rec_yd", cy)):
        spec = cfg.get("milestones", {}).get(cat)
        if not spec:
            continue
        met = [t for t in spec["tiers"] if raw >= t["threshold"]]
        if not met:
            continue
        best = max(met, key=lambda t: t["threshold"])
        p = best["points"]
        if isinstance(p, dict):
            p = p.get(pos, p.get("default", 0))
        pts += p
    return round(pts, 2)


# ---------------------------------------------------------------------------
# B. nflverse standard PPR, rebuilt from raw columns.
# ---------------------------------------------------------------------------
def rebuilt_ppr(row):
    return (
        g(row, "passing_yards") * 0.04
        + g(row, "passing_tds") * 4
        + g(row, "passing_interceptions") * -2
        + g(row, "rushing_yards") * 0.1
        + g(row, "rushing_tds") * 6
        + g(row, "receiving_yards") * 0.1
        + g(row, "receiving_tds") * 6
        + g(row, "receptions") * 1
        + (g(row, "sack_fumbles_lost") + g(row, "rushing_fumbles_lost")
           + g(row, "receiving_fumbles_lost")) * -2
        + (g(row, "passing_2pt_conversions") + g(row, "rushing_2pt_conversions")
           + g(row, "receiving_2pt_conversions")) * 2
        + g(row, "special_teams_tds") * 6
    )


def analytic_delta(row, cfg):
    """league_points - standard_ppr, derived rule-by-rule from the config."""
    pos = row.get("position")
    lin = cfg["linear"]

    def v(cat, default=0):
        x = lin.get(cat, default)
        if isinstance(x, dict):
            return x.get(pos, x.get("default", 0))
        return x

    att, cmp_ = g(row, "attempts"), g(row, "completions")
    inc = max(0.0, att - cmp_)
    py, ry, cy = g(row, "passing_yards"), g(row, "rushing_yards"), g(row, "receiving_yards")
    subset_lost = (g(row, "sack_fumbles_lost") + g(row, "rushing_fumbles_lost")
                   + g(row, "receiving_fumbles_lost"))

    d = 0.0
    d += py * (v("pass_yd") - 0.04)
    d += cmp_ * v("pass_completion") + inc * v("pass_incompletion")
    d += g(row, "passing_tds") * (v("pass_td") - 4)
    d += g(row, "passing_interceptions") * (v("pass_int") - (-2))
    d += g(row, "sacks_suffered") * v("pass_sacked")
    d += ry * (v("rush_yd") - 0.1) + cy * (v("rec_yd") - 0.1)
    d += g(row, "rushing_tds") * (v("rush_td") - 6) + g(row, "receiving_tds") * (v("rec_td") - 6)
    d += g(row, "rushing_first_downs") * v("rush_first_down")
    d += g(row, "receiving_first_downs") * v("rec_first_down")
    d += g(row, "receptions") * (v("reception") - 1)
    d += (g(row, "passing_2pt_conversions") * (v("pass_2pt") - 2)
          + g(row, "rushing_2pt_conversions") * (v("rush_2pt") - 2)
          + g(row, "receiving_2pt_conversions") * (v("rec_2pt") - 2))
    d += g(row, "special_teams_tds") * (v("return_td") - 6)
    d += g(row, "kickoff_return_yards") * v("kick_return_yd")
    d += g(row, "punt_return_yards") * v("punt_return_yd")
    # our engine uses whole-player fumble TOTALS; nflverse ppr uses the subset.
    d += g(row, "fumbles_total") * v("fumble")
    d += g(row, "fumbles_lost_total") * v("fumble_lost") - subset_lost * (-2)
    d += g(row, "fumble_recovery_tds") * v("fumble_recovery_td")
    for cat, raw in (("pass_yd", py), ("rush_yd", ry), ("rec_yd", cy)):
        spec = cfg.get("milestones", {}).get(cat)
        if not spec:
            continue
        met = [t for t in spec["tiers"] if raw >= t["threshold"]]
        if not met:
            continue
        p = max(met, key=lambda t: t["threshold"])["points"]
        if isinstance(p, dict):
            p = p.get(pos, p.get("default", 0))
        d += p
    return d


def describe(name, xs):
    xs = sorted(xs)
    n = len(xs)
    q = lambda f: xs[min(n - 1, int(f * n))]
    print(f"  {name:28s} n={n:6d} mean={statistics.fmean(xs):+8.4f} sd={statistics.pstdev(xs):7.4f} "
          f"min={xs[0]:+8.3f} p1={q(.01):+7.3f} p50={q(.5):+7.3f} p99={q(.99):+7.3f} max={xs[-1]:+8.3f} "
          f"|>0.01|={sum(1 for x in xs if abs(x) > 0.01)}")


def main():
    frame = nfl.load_player_stats(seasons=[SEASON])
    frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
    frame = frame[(frame["season_type"] == "REG") & (frame["position"].isin(POSITIONS))]
    rows = frame.to_dict("records")
    print(f"{SEASON} REG QB/RB/WR/TE player-games: {len(rows)}\n")

    # --- sanity: is our rebuilt PPR actually nflverse's? --------------------
    ppr_res = [rebuilt_ppr(r) - g(r, "fantasy_points_ppr") for r in rows]
    print("SANITY -- rebuilt standard PPR vs nflreadpy's shipped fantasy_points_ppr:")
    describe("rebuilt - shipped", ppr_res)
    worst = max(rows, key=lambda r: abs(rebuilt_ppr(r) - g(r, "fantasy_points_ppr")))
    print(f"    worst row: {worst.get('player_display_name')} wk{worst.get('week')} "
          f"rebuilt={rebuilt_ppr(worst):.2f} shipped={g(worst,'fantasy_points_ppr'):.2f}")

    for league_id in ("league-1", "league-2"):
        cfg = league_config(league_id)
        print(f"\n{'='*78}\n{league_id}\n{'='*78}")

        engine, indep, delta_res, pos_of = [], [], [], []
        for r in rows:
            sl = nflreadpy_row_to_stat_line(r)
            e = float(compute_league_points(sl, cfg))
            i = independent_score(r, cfg)
            engine.append(e)
            indep.append(i)
            delta_res.append(e - (g(r, "fantasy_points_ppr") + analytic_delta(r, cfg)))
            pos_of.append(r["position"])

        print("A. ENGINE vs INDEPENDENT RE-IMPLEMENTATION (must be ~0):")
        describe("engine - independent", [a - b for a, b in zip(engine, indep)])

        print("B. ENGINE vs EXTERNAL REFERENCE (shipped ppr + analytic delta):")
        describe("residual", delta_res)
        for p in POSITIONS:
            sub = [d for d, q in zip(delta_res, pos_of) if q == p]
            describe(f"  {p}", sub)

        print("C. Mean points/game by position (engine), starters only (>=1 touch):")
        for p in POSITIONS:
            sub = [e for e, q, r in zip(engine, pos_of, rows) if q == p]
            print(f"    {p}: mean={statistics.fmean(sub):6.2f} n={len(sub)}")

        # biggest single-row disagreements on check A
        diffs = sorted(zip([abs(a - b) for a, b in zip(engine, indep)], range(len(rows))), reverse=True)[:3]
        print("D. Largest engine-vs-independent disagreements:")
        for dv, idx in diffs:
            r = rows[idx]
            print(f"    {dv:6.3f}  {r.get('player_display_name')} ({r['position']}) wk{r.get('week')} "
                  f"engine={engine[idx]:.2f} indep={indep[idx]:.2f}")


if __name__ == "__main__":
    main()
