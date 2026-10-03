"""Audit III, Task 2: pull Sleeper / ESPN / FantasyPros week-1 projections,
re-score every one of them through EACH league's own scoring-config, and
compare to ours.

STATUS: NOT RUN. Every vendor host is blocked by the egress policy of the
sandbox the third audit ran in --

    api.sleeper.app                 CONNECT -> 403 (policy denial)
    lm-api-reads.fantasy.espn.com   CONNECT -> 403 (policy denial)
    api.fantasypros.com             CONNECT -> 403 (policy denial)
    site.api.espn.com               CONNECT -> 403 (policy denial)

confirmed both from the shell and through the harness's own fetch tool, with
the denials recorded in the proxy's status endpoint. Only github.com and
raw.githubusercontent.com are reachable, which is why nflreadpy works and
nothing else does. So this script is the METHOD, written and reviewable, not a
source of numbers -- and the ESPN stat-id map below is the one part of it that
has NOT been checked against a live response.

READ THIS BEFORE TRUSTING ANY NUMBER IT PRINTS. This repo's single most
expensive class of bug is a column map that matches nothing and scores a silent
zero (v16: three of them, surviving 253 tests and four backtest rounds). The
ESPN_STAT_IDS map is exactly that shape and is UNVERIFIED. Run with --probe
first: it dumps one raw player's stat block so the ids can be checked against
real values before any comparison is believed. The script REFUSES to print a
comparison table without --i-have-verified-the-stat-ids.

Method, per CLAUDE.md's standing rules:
  * never compare vendor precomputed point totals -- map each vendor's RAW
    PROJECTED STAT LINE into this repo's stat_line shape and re-score it
    through compute_league_points() with the league's own config;
  * set `position` on every stat line, or league-1 silently pays a tight end
    an RB/WR reception;
  * a category the vendor does not publish must NOT contribute zero -- run it
    through fantasypros.impute_unpublished_categories(), and report what is
    still missing after that (for league-1 that is player return yardage,
    measured at 1.23 pts/game for a top-10 RB and 2.68 for a top-10 WR --
    see audit3_imputation_decomp.py).

Usage:
    .venv/bin/python docs/research/scripts/audit3_vendor_compare.py --probe
    .venv/bin/python docs/research/scripts/audit3_vendor_compare.py \
        --i-have-verified-the-stat-ids
"""
import sys, os, json, argparse, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import REPO_ROOT, league_config, load_env

load_env()
import requests  # noqa: E402
from scoring import compute_league_points  # noqa: E402
from fantasypros import (  # noqa: E402
    impute_unpublished_categories, fetch_consensus_tier,
    fantasypros_stats_to_stat_line, load_fantasypros_id_crosswalk_nflreadpy,
)

SEASON, WEEK = 2026, 1
POSITIONS = ("QB", "RB", "WR", "TE")

# --- Sleeper ----------------------------------------------------------------
# Sleeper's projection objects carry per-category fields under these names.
# UNVERIFIED against a live 2026 response.
SLEEPER_FIELD_MAP = {
    "pass_yd": "pass_yd", "pass_td": "pass_td", "pass_int": "pass_int",
    "pass_cmp": "pass_completion", "pass_att": "_pass_att",
    "pass_sack": "pass_sacked", "pass_2pt": "pass_2pt",
    "rush_yd": "rush_yd", "rush_td": "rush_td", "rush_2pt": "rush_2pt",
    "rush_fd": "rush_first_down",
    "rec": "reception", "rec_yd": "rec_yd", "rec_td": "rec_td",
    "rec_2pt": "rec_2pt", "rec_fd": "rec_first_down",
    "fum_lost": "fumble_lost", "fum": "fumble",
    "kr_yd": "kick_return_yd", "pr_yd": "punt_return_yd",
}

# --- ESPN -------------------------------------------------------------------
# ESPN's kona_player_info returns stats[].stats keyed by NUMERIC STAT ID.
# statSourceId == 1 is the projection; statSplitTypeId == 1 is the weekly split.
# THESE IDS ARE THE UNVERIFIED PART. Check them with --probe against a player
# whose ESPN-app projection you can see, before believing any output.
ESPN_STAT_IDS = {
    "0": "_pass_att", "1": "pass_completion", "3": "pass_yd", "4": "pass_td",
    "20": "pass_int", "24": "rush_att_unused", "23": "_rush_att",
    "25": "rush_yd", "26": "rush_td", "42": "rec_yd", "43": "rec_td",
    "53": "reception", "58": "_targets", "68": "fumble_lost",
    "62": "pass_2pt", "63": "rush_2pt", "64": "rec_2pt",
}
ESPN_POSITION_BY_ID = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}


def sleeper_projections():
    url = (f"https://api.sleeper.app/projections/nfl/{SEASON}/{WEEK}"
           "?season_type=regular" + "".join(f"&position[]={p}" for p in POSITIONS)
           + "&order_by=ppr")
    return requests.get(url, timeout=60).json()


def espn_projections(limit=800):
    hdr = {"X-Fantasy-Filter": json.dumps({"players": {
        "filterStatsForTopScoringPeriodIds": {"value": WEEK},
        "limit": limit,
        "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}}),
        "User-Agent": "Mozilla/5.0"}
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}"
           "/segments/0/leaguedefaults/3?view=kona_player_info")
    return requests.get(url, headers=hdr, timeout=90).json().get("players", [])


def espn_stat_line(player):
    for st in player.get("player", {}).get("stats", []):
        if st.get("statSourceId") == 1 and st.get("scoringPeriodId") == WEEK:
            out = {}
            for sid, val in (st.get("stats") or {}).items():
                key = ESPN_STAT_IDS.get(str(sid))
                if key and not key.startswith("_") and val:
                    out[key] = out.get(key, 0) + float(val)
            atts = (st.get("stats") or {}).get("0")
            if atts and out.get("pass_completion"):
                inc = float(atts) - out["pass_completion"]
                if inc > 0:
                    out["pass_incompletion"] = inc
            return out
    return None


def sleeper_stat_line(entry):
    stats = entry.get("stats") or {}
    out = {}
    for k, our in SLEEPER_FIELD_MAP.items():
        v = stats.get(k)
        if v and not our.startswith("_"):
            out[our] = out.get(our, 0) + float(v)
    att, cmp_ = stats.get("pass_att"), stats.get("pass_cmp")
    if att and cmp_ and float(att) > float(cmp_):
        out["pass_incompletion"] = float(att) - float(cmp_)
    return out


def score(stat_line, position, cfg):
    """The one path every vendor number must go through."""
    sl = dict(stat_line)
    sl["position"] = position
    sl = impute_unpublished_categories(sl, position)
    return float(compute_league_points(sl, cfg, position=position))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="dump one raw response per vendor and exit")
    ap.add_argument("--i-have-verified-the-stat-ids", action="store_true",
                    dest="verified")
    args = ap.parse_args()

    if args.probe:
        for name, fn in (("sleeper", sleeper_projections), ("espn", espn_projections)):
            try:
                d = fn()
                first = d[0] if isinstance(d, list) else list(d.values())[0]
                print(f"\n===== {name} raw first entry =====")
                print(json.dumps(first, indent=1)[:4000])
            except Exception as exc:
                print(f"{name}: FETCH FAILED -- {type(exc).__name__}: {exc}")
        key = os.environ.get("FANTASYPROS_API_KEY")
        if key:
            try:
                raw = fetch_consensus_tier(SEASON, WEEK, key)
                print("\n===== fantasypros raw first QB =====")
                print(json.dumps(raw["QB"][0], indent=1)[:2500])
            except Exception as exc:
                print(f"fantasypros: FETCH FAILED -- {type(exc).__name__}: {exc}")
        return

    if not args.verified:
        print(__doc__)
        print("\nREFUSING to print a comparison table. Run --probe, check "
              "ESPN_STAT_IDS and SLEEPER_FIELD_MAP against the raw response, "
              "then re-run with --i-have-verified-the-stat-ids.")
        return

    mock = os.path.join(REPO_ROOT, "frontend", "public", "mock")
    for league_id in ("league-1", "league-2"):
        cfg = league_config(league_id)
        ours = {p["name"]: p for p in json.load(open(
            os.path.join(mock, league_id, f"weekly-report-week-{WEEK}.json")))["projections"]}

        rows = {}
        try:
            for e in sleeper_projections():
                pos = (e.get("player") or {}).get("position")
                nm = ((e.get("player") or {}).get("first_name", "") + " "
                      + (e.get("player") or {}).get("last_name", "")).strip()
                if pos in POSITIONS and nm:
                    rows.setdefault(nm, {})["sleeper"] = score(sleeper_stat_line(e), pos, cfg)
        except Exception as exc:
            print(f"  sleeper unavailable: {exc}")
        try:
            for p in espn_projections():
                pl = p.get("player", {})
                pos = ESPN_POSITION_BY_ID.get(pl.get("defaultPositionId"))
                sl = espn_stat_line(p) if pos in POSITIONS else None
                if sl:
                    rows.setdefault(pl.get("fullName"), {})["espn"] = score(sl, pos, cfg)
        except Exception as exc:
            print(f"  espn unavailable: {exc}")
        key = os.environ.get("FANTASYPROS_API_KEY")
        if key:
            try:
                for pos, players in fetch_consensus_tier(SEASON, WEEK, key).items():
                    for pl in players:
                        rows.setdefault(pl.get("name"), {})["fantasypros"] = score(
                            fantasypros_stats_to_stat_line(pl.get("stats", {})), pos, cfg)
            except Exception as exc:
                print(f"  fantasypros unavailable: {exc}")

        print(f"\n=== {league_id}: vendor projections re-scored through this league's config ===")
        print(f"{'player':26s} {'ours(cond)':>10s} {'sleeper':>8s} {'espn':>8s} {'fpros':>8s}")
        for nm, v in sorted(rows.items(), key=lambda kv: -max(kv[1].values(), default=0))[:60]:
            o = (ours.get(nm) or {}).get("projection", {}).get("conditionalPoints")
            print(f"{nm[:26]:26s} {o if o is not None else float('nan'):10.2f} "
                  f"{v.get('sleeper', float('nan')):8.2f} {v.get('espn', float('nan')):8.2f} "
                  f"{v.get('fantasypros', float('nan')):8.2f}")


if __name__ == "__main__":
    main()
