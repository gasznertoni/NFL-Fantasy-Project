"""
Rotowire weekly-projections consensus tier — free, no API key, returns full
projected stat lines for the top-10 players per position.

Endpoint confirmed 2026-09-01:
  GET https://www.rotowire.com/football/tables/weekly-projections.php?pos={pos}&week={week}
  → JSON list of up to 10 players, each with projected stat fields and
    precomputed `fantasy`/`ppr`/`custpts` points.

Field naming note: `offrecatt` in Rotowire's response is projected RECEPTIONS
(not targets) — confirmed by checking that `ppr - fantasy == offrecatt × 1`
for every PPR-scoring row (e.g. Gibbs: 22.30 - 18.30 = 4.00 == offrecatt).

Like fantasypros.py, this module:
  - Maps Rotowire's raw projected stat fields to this project's stat_line
    category names (ROTOWIRE_COLUMN_MAP_QB / _SKILL).
  - Recomputes points via scoring.compute_league_points using the league's
    real scoring config — Rotowire's precomputed points fields are NOT read
    (they don't know this league's 6-point passing TDs or granular DST rules).
  - Resolves player identity via name+team matching against an nflreadpy pool
    (no third-party player-ID crosswalk needed — Rotowire's firstname/lastname/
    team fields are sufficient for unambiguous matching at 10 players/position).

Cap: 10 players per position, no pagination (confirmed — limit/page params
have no effect). Same structural limit as FantasyPros' free tier.

Blending: generate_report.py calls both this module and fantasypros.py, then
averages the stat lines for players appearing in both sources, keeping
whichever single source covers a player that the other misses. The averaged
result is still labelled "consensus" and scored through our formula once.
"""

from __future__ import annotations

import time
from typing import Any

from scoring import compute_league_points

ROTOWIRE_URL = "https://www.rotowire.com/football/tables/weekly-projections.php"
ROTOWIRE_REFERER = "https://www.rotowire.com/football/projections-weekly.php"

# Positions covered by the weekly-projections endpoint (DST returns very few
# rows pre-season; K is also sparse but included for completeness).
POSITIONS = ("QB", "RB", "WR", "TE")

REQUEST_DELAY_SECONDS = 1.0

# Rotowire field → this project's stat_line key, for QBs.
ROTOWIRE_COLUMN_MAP_QB: dict[str, str] = {
    "offpassyard": "pass_yd",
    "offpasstd": "pass_td",
    "offpassint": "pass_int",
    "offpasscomp": "pass_cmp",
    "offpassatt": "pass_att",
    "offrushyard": "rush_yd",
    "offrushtd": "rush_td",
    "offrushatt": "rush_att",
}

# Field map for RB/WR/TE (skill positions).
ROTOWIRE_COLUMN_MAP_SKILL: dict[str, str] = {
    "offrushatt": "rush_att",
    "offrushyard": "rush_yd",
    "offrushtd": "rush_td",
    # offrecatt = projected receptions (verified: ppr - fantasy == offrecatt × 1)
    "offrecatt": "reception",
    "offrecyard": "rec_yd",
    "offrectd": "rec_td",
}


# ---------------------------------------------------------------------------
# Pure assembly -- no network, tested with synthetic data.
# ---------------------------------------------------------------------------


def rotowire_stats_to_stat_line(row: dict[str, Any], position: str) -> dict[str, float]:
    col_map = ROTOWIRE_COLUMN_MAP_QB if position == "QB" else ROTOWIRE_COLUMN_MAP_SKILL
    out: dict[str, float] = {}
    for rw_key, our_key in col_map.items():
        raw = row.get(rw_key)
        if raw is None:
            continue
        try:
            val = float(raw)
        except (ValueError, TypeError):
            continue
        if val:
            out[our_key] = out.get(our_key, 0.0) + val
    return out


def project_rotowire_player(row: dict[str, Any], position: str, scoring_config: dict[str, Any]) -> dict[str, Any]:
    stat_line = rotowire_stats_to_stat_line(row, position)
    result = compute_league_points(stat_line, scoring_config)
    return {
        "source": "consensus",
        "rw_player_id": row.get("playerid"),
        "projected_points": result.total,
        "stat_line": stat_line,
        "breakdown": result.breakdown,
    }


def build_rotowire_consensus_tier(
    raw_by_position: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    name_team_to_player_id: dict[tuple[str, str], str],
) -> dict[str, dict[str, Any]]:
    """raw_by_position (from fetch_rotowire_projections) × scoring_config ×
    crosswalk → {player_id: projection_dict}, same shape as
    fantasypros.build_consensus_tier.

    Name matching uses (normalised_full_name, team) as the key. A player
    whose name+team doesn't resolve silently falls back to the in-house tier
    — same contract as the FantasyPros crosswalk's unresolved-fpid behaviour.
    """
    out: dict[str, dict[str, Any]] = {}
    for position, players in raw_by_position.items():
        for row in players:
            full_name = f"{row.get('firstname', '')} {row.get('lastname', '')}".strip()
            team = (row.get("team") or "").upper()
            player_id = name_team_to_player_id.get((full_name.lower(), team))
            if player_id is None:
                # Try name-only fallback (trades mid-week may change team)
                player_id = name_team_to_player_id.get((full_name.lower(), ""))
            if player_id is None:
                continue
            out[player_id] = project_rotowire_player(row, position, scoring_config)
    return out


def blend_consensus_projections(
    fp_projections: dict[str, dict[str, Any]],
    rw_projections: dict[str, dict[str, Any]],
    scoring_config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Average the stat lines for players covered by both FantasyPros and
    Rotowire, recompute points once from the averaged line. Players covered
    by only one source keep that source's projection unchanged.

    The blend averages stat_lines (not pre-computed points) so the result is
    still scored through our own formula — the same "never trust a vendor's
    precomputed points" rule both modules already follow.
    """
    all_ids = set(fp_projections) | set(rw_projections)
    out: dict[str, dict[str, Any]] = {}
    for pid in all_ids:
        fp = fp_projections.get(pid)
        rw = rw_projections.get(pid)
        if fp is not None and rw is not None:
            # Average the two stat lines key-by-key; include keys from either.
            all_keys = set(fp["stat_line"]) | set(rw["stat_line"])
            averaged: dict[str, float] = {}
            for k in all_keys:
                a = fp["stat_line"].get(k, 0.0)
                b = rw["stat_line"].get(k, 0.0)
                averaged[k] = (a + b) / 2.0
            result = compute_league_points(averaged, scoring_config)
            out[pid] = {
                "source": "consensus",
                "projected_points": result.total,
                "stat_line": averaged,
                "breakdown": result.breakdown,
            }
        elif fp is not None:
            out[pid] = fp
        else:
            out[pid] = rw  # type: ignore[assignment]
    return out


# ---------------------------------------------------------------------------
# Network adapters -- NOT exercised by the test suite.
# ---------------------------------------------------------------------------


def fetch_rotowire_projections(
    week: int,
    positions: tuple[str, ...] = POSITIONS,
) -> dict[str, list[dict[str, Any]]]:
    """One HTTP call per position; returns {position: [row, ...]}. A failed
    call for one position logs a warning and returns an empty list for that
    position — the caller degrades gracefully rather than blocking the report."""
    import requests

    out: dict[str, list[dict[str, Any]]] = {}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": ROTOWIRE_REFERER}
    for i, pos in enumerate(positions):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        try:
            resp = requests.get(
                ROTOWIRE_URL,
                params={"pos": pos, "week": week},
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            rows = resp.json()
            # Annotate each row with its position (not always present for QB)
            for row in rows:
                row.setdefault("position", pos)
            out[pos] = rows
        except Exception as exc:  # noqa: BLE001
            print(f"  Rotowire projections fetch failed for {pos} ({exc}) — skipping that position.")
            out[pos] = []
    return out


def build_name_team_crosswalk(pool: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """(normalised_full_name, TEAM) → player_id, built from the nflreadpy
    player pool. The name-only fallback key (name, '') is also inserted for
    each player so a trade-induced team mismatch still resolves."""
    xwalk: dict[tuple[str, str], str] = {}
    for p in pool:
        name = (p.get("name") or "").lower().strip()
        team = (p.get("team") or "").upper()
        pid = p.get("playerId")
        if not name or not pid:
            continue
        xwalk[(name, team)] = pid
        xwalk.setdefault((name, ""), pid)
    return xwalk
