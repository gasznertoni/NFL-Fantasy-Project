"""
Phase 1 hands-on shape probe — run this on your own machine (not in the Cowork
cloud sandbox, which has a restricted network allowlist that blocks pypi.org
and GitHub's release-asset downloads).

Usage:
    pip install nflreadpy python-dotenv requests
    # optional, only needed once you have the keys:
    #   create a .env file in the repo root with:
    #     FANTASYPROS_API_KEY=...
    #     API_SPORTS_KEY=...
    python3 scripts/probe_sources.py

Writes docs/research/phase1-local-results.json with everything this script
found, and prints a human-readable summary to the console. Share the JSON
back so it can be folded into the coverage scorecard and scope note.

Each probe is wrapped so one source failing doesn't stop the others.
"""

import json
import os
import sys
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

results = {"run_at": datetime.now(timezone.utc).isoformat(), "sources": {}}


def record(source, ok, data):
    results["sources"][source] = {"ok": ok, "data": data}
    status = "OK" if ok else "FAILED"
    print(f"\n=== {source}: {status} ===")
    print(json.dumps(data, indent=2, default=str)[:2000])


# ---------------------------------------------------------------------------
# 1. nflreadpy — stats, injuries, rosters
# ---------------------------------------------------------------------------
def probe_nflreadpy():
    import nflreadpy as nfl

    out = {}

    stats = nfl.load_player_stats(seasons=[2025])
    df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
    out["player_stats_columns"] = list(df.columns)
    out["player_stats_row_count"] = len(df)
    out["player_stats_week_range"] = [int(df["week"].min()), int(df["week"].max())] if "week" in df.columns else None
    # edge case: a rookie should NOT be in 2025 season stats pre-draft-year data,
    # so instead check a known recently-traded player is attributed to the right team
    if "player_display_name" in df.columns:
        sample = df[df["player_display_name"].str.contains("Bijan Robinson", na=False)]
        out["sample_player_rows"] = sample.tail(2).to_dict("records") if len(sample) else "not found"

    injuries = nfl.load_injuries(seasons=[2025])
    idf = injuries.to_pandas() if hasattr(injuries, "to_pandas") else injuries
    out["injuries_columns"] = list(idf.columns)
    out["injuries_row_count"] = len(idf)
    out["has_practice_participation_field"] = any(
        "practice" in c.lower() for c in idf.columns
    )

    rosters = nfl.load_rosters(seasons=[2025])
    rdf = rosters.to_pandas() if hasattr(rosters, "to_pandas") else rosters
    out["rosters_columns"] = list(rdf.columns)
    out["rosters_row_count"] = len(rdf)
    # edge cases suggested by the test plan
    if "status" in rdf.columns:
        out["status_value_counts"] = rdf["status"].value_counts().to_dict()

    return out


# ---------------------------------------------------------------------------
# 2. DynastyProcess — ECR + projections file, and full player-id crosswalk
#    (id crosswalk was already spot-checked in the cloud session; this
#    re-confirms it and adds the file that couldn't be fetched there)
# ---------------------------------------------------------------------------
def probe_dynastyprocess():
    out = {}

    ids = requests.get(
        "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv",
        timeout=30,
    )
    ids.raise_for_status()
    id_lines = ids.text.splitlines()
    out["playerids_columns"] = id_lines[0].split(",")
    out["playerids_row_count"] = len(id_lines) - 1

    # the file that couldn't be pulled from the cloud sandbox (>20MB / gzip)
    import gzip
    import io

    import pandas as pd

    ecr = requests.get(
        "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr.csv.gz",
        timeout=60,
    )
    ecr.raise_for_status()
    ecr_df = pd.read_csv(io.BytesIO(gzip.decompress(ecr.content)))
    out["fpecr_columns"] = list(ecr_df.columns)
    out["fpecr_row_count"] = len(ecr_df)
    # THE key question from the test plan: are actual projections in here,
    # or only rank/ECR fields?
    projection_like_cols = [
        c for c in ecr_df.columns
        if any(k in c.lower() for k in ["proj", "pts", "point"])
    ]
    out["projection_like_columns"] = projection_like_cols
    out["HAS_PROJECTIONS_NOT_JUST_RANK"] = len(projection_like_cols) > 0

    return out


# ---------------------------------------------------------------------------
# 3. Sleeper — full player dump (the 5MB blob the cloud session couldn't pull)
# ---------------------------------------------------------------------------
def probe_sleeper():
    out = {}
    resp = requests.get("https://api.sleeper.app/v1/players/nfl", timeout=60)
    resp.raise_for_status()
    players = resp.json()
    out["player_count"] = len(players)
    sample_id = next(iter(players))
    out["sample_fields"] = list(players[sample_id].keys())

    # edge cases: rookie, practice-squad player
    rookies = [
        p for p in players.values()
        if p.get("years_exp") == 0 and p.get("position") in ("QB", "RB", "WR", "TE")
    ]
    out["rookie_count_found"] = len(rookies)
    out["rookie_sample"] = rookies[0] if rookies else None

    ps = [p for p in players.values() if p.get("status") == "Practice Squad"]
    out["practice_squad_count_found"] = len(ps)

    return out


# ---------------------------------------------------------------------------
# 4. FantasyPros API (needs FANTASYPROS_API_KEY)
# ---------------------------------------------------------------------------
def probe_fantasypros():
    key = os.environ.get("FANTASYPROS_API_KEY")
    if not key:
        return None  # skip cleanly, not a failure
    out = {}
    resp = requests.get(
        "https://api.fantasypros.com/public/v2/json/nfl/2025/projections",
        params={"position": "ALL", "week": 1, "scoring": "PPR"},
        headers={"x-api-key": key},
        timeout=30,
    )
    out["status_code"] = resp.status_code
    if resp.ok:
        data = resp.json()
        out["top_level_keys"] = list(data.keys())
        # free-tier metadata worth surfacing directly: how many players actually
        # exist for this query vs. how many the free tier hands back
        for meta_key in ("count", "limit", "public_api_limited", "tier", "scoring"):
            if meta_key in data:
                out[f"meta_{meta_key}"] = data[meta_key]
        players = data.get("players", [])
        out["player_count"] = len(players)
        out["sample_player_fields"] = list(players[0].keys()) if players else None
        # NOTE: the real projection numbers live *inside* a nested "stats" object
        # per player, not as top-level fields on the player dict — check there,
        # not just the top-level keys (an earlier version of this script only
        # checked top-level keys and produced a false negative)
        stats_obj = players[0].get("stats", {}) if players else {}
        out["sample_stats_fields"] = list(stats_obj.keys())
        out["HAS_PROJECTIONS_IN_FREE_TIER"] = any(
            "point" in str(k).lower() or "fpts" in str(k).lower() or "proj" in str(k).lower()
            for k in stats_obj.keys()
        )
    else:
        out["body"] = resp.text[:500]
    return out


# ---------------------------------------------------------------------------
# 5. API-Sports (American Football) (needs API_SPORTS_KEY)
# ---------------------------------------------------------------------------
def probe_api_sports():
    key = os.environ.get("API_SPORTS_KEY")
    if not key:
        return None  # skip cleanly
    out = {}
    resp = requests.get(
        "https://v1.american-football.api-sports.io/players",
        params={"search": "Robinson"},
        headers={"x-apisports-key": key},
        timeout=30,
    )
    out["status_code"] = resp.status_code
    out["quota_headers"] = {
        k: v for k, v in resp.headers.items() if "ratelimit" in k.lower() or "requests" in k.lower()
    }
    if resp.ok:
        data = resp.json()
        out["response_keys"] = list(data.keys())
        out["results_count"] = data.get("results")
        out["sample_response"] = data.get("response", [])[:1]
    else:
        out["body"] = resp.text[:500]
    return out


def main():
    probes = [
        ("nflreadpy", probe_nflreadpy),
        ("dynastyprocess", probe_dynastyprocess),
        ("sleeper", probe_sleeper),
        ("fantasypros", probe_fantasypros),
        ("api_sports", probe_api_sports),
    ]

    for name, fn in probes:
        try:
            data = fn()
            if data is None:
                print(f"\n=== {name}: SKIPPED (no API key set) ===")
                results["sources"][name] = {"ok": None, "data": "skipped, no key"}
            else:
                record(name, True, data)
        except Exception as e:  # noqa: BLE001
            record(name, False, {"error": str(e)})

    out_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs", "research", "phase1-local-results.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n\nWrote {out_path}")


if __name__ == "__main__":
    sys.exit(main())
