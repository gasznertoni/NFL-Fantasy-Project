"""Audit III: scored-category vs produced-category coverage matrix.

CLAUDE.md's rule -- "a vendor feed that cannot supply a scored category is not
the same as a league that does not have the rule" -- needs a mechanical check:
for every category each league SCORES, which stat-line producer can actually
emit it? A scored category with no producer is a silent permanent zero.

Run: .venv/bin/python docs/research/scripts/audit3_category_coverage.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import BACKEND, REPO_ROOT, league_config  # noqa: F401

import scoring, dst, kicker, fantasypros, rotowire_projections

# --- what each producer can emit -------------------------------------------
PRODUCERS = {
    "scoring.nflreadpy_row_to_stat_line": set(scoring.NFLREADPY_OFFENSE_COLUMN_MAP.values())
    | {"pass_incompletion"},
    "kicker.nflreadpy_kicker_row_to_stat_line": set(kicker.NFLREADPY_KICKER_NATIVE_BUCKETS.values())
    | set(kicker.NFLREADPY_KICKER_DIRECT_COLUMN_MAP.values())
    | set(kicker.ROLLED_BANDS) | set(kicker.FLAT_MISS_BAND)
    | set(kicker.BANDED_MISS_BANDS) | {"pat_missed"},
    "dst.assemble_dst_stat_line": set(dst.DST_DIRECT_COLUMN_MAP.values())
    | {"def_return_yd", "def_blocked_kick", "def_yards_allowed", "def_points_allowed"},
    "fantasypros (+impute)": set(fantasypros.FANTASYPROS_COLUMN_MAP.values())
    | {"rec_first_down", "rush_first_down", "pass_completion", "pass_incompletion", "pass_sacked"},
    "rotowire_projections": set(getattr(rotowire_projections, "ROTOWIRE_COLUMN_MAP", {}).values()),
}

ANY_PRODUCER = set().union(*PRODUCERS.values())

for league_id in ("league-1", "league-2"):
    cfg = league_config(league_id)
    scored = set(cfg.get("linear", {})) | set(cfg.get("milestones", {})) | set(cfg.get("tiers", {}))
    print(f"\n{'='*72}\n{league_id}: {len(scored)} scored categories\n{'='*72}")
    orphans = sorted(scored - ANY_PRODUCER)
    print(f"\nSCORED BUT NO PRODUCER (silent permanent zero): {len(orphans)}")
    for c in orphans:
        val = cfg.get("linear", {}).get(c, cfg.get("milestones", {}).get(c))
        print(f"    {c:28s} value={val}")
    # Which categories are produced by the nflreadpy path but never scored?
    unscored = sorted(ANY_PRODUCER - scored)
    print(f"\nPRODUCED BUT NOT SCORED (harmless, league has no such rule): {len(unscored)}")
    print("   ", ", ".join(unscored))

    # per-producer view of the offensive skill categories
    print("\nPer-producer coverage of this league's OFFENSIVE scored categories:")
    off = sorted(c for c in scored if not c.startswith(("def_", "fg_", "pat_")))
    for name in ("scoring.nflreadpy_row_to_stat_line", "fantasypros (+impute)", "rotowire_projections"):
        missing = [c for c in off if c not in PRODUCERS[name]]
        print(f"  {name:38s} misses {len(missing):2d}: {', '.join(missing) if missing else '-'}")
