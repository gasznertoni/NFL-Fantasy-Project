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
from typing import Any, Optional

from fantasypros import impute_unpublished_categories
from scoring import compute_league_points

# Display names for the two consensus feeds, used to label each projection
# with the sources that actually produced it.
FANTASYPROS_SOURCE = "FantasyPros"
ROTOWIRE_SOURCE = "Rotowire"

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
    # Real projected completions and attempts. league-1 scores a completion at
    # +0.1 and an incompletion at -0.1 as of 2026-09-06, so these stopped being
    # unscored bookkeeping fields: pass_completion is the scoring category, and
    # pass_incompletion is derived from the pair in rotowire_stats_to_stat_line.
    "offpasscomp": "pass_completion",
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
    # Rotowire publishes attempts and completions, so incompletions are exact
    # here rather than estimated the way they are for FantasyPros.
    attempts = out.pop("pass_att", 0.0)
    if attempts:
        incomplete = attempts - out.get("pass_completion", 0.0)
        if incomplete > 0:
            out["pass_incompletion"] = incomplete
    return out


def project_rotowire_player(row: dict[str, Any], position: str, scoring_config: dict[str, Any]) -> dict[str, Any]:
    stat_line = rotowire_stats_to_stat_line(row, position)
    # Rotowire, like FantasyPros, publishes no first downs and no sacks taken,
    # both of which league-1 scores. Same estimator, and it only fills keys that
    # are absent -- so Rotowire's real completions survive untouched.
    stat_line = impute_unpublished_categories(stat_line, position)
    result = compute_league_points(stat_line, scoring_config, position=position)
    return {
        "source": "consensus",
        "rw_player_id": row.get("playerid"),
        "position": position,
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

    Every entry carries `contributing_sources`: the names that actually fed it,
    not the names the blend is capable of using. Only some of the tier is
    genuinely blended -- the two feeds cover overlapping but different players
    -- so a single static "FantasyPros + Rotowire" label on the whole tier
    would tell a reader that two sources agreed on a player where only one had
    any data at all.
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
                # A stat line can carry non-numeric bookkeeping keys (position
                # is the one that bites); averaging those is meaningless, so
                # take whichever side has a value and move on.
                if isinstance(a, str) or isinstance(b, str):
                    averaged[k] = a or b
                    continue
                averaged[k] = (a + b) / 2.0
            # league-1 pays a TE reception double an RB/WR one, so a blended
            # line MUST be scored with a position or every blended tight end
            # silently loses half a point per catch.
            position = fp.get("position") or rw.get("position")
            result = compute_league_points(averaged, scoring_config, position=position)
            out[pid] = {
                "source": "consensus",
                "position": position,
                "projected_points": result.total,
                "stat_line": averaged,
                "breakdown": result.breakdown,
                "contributing_sources": [FANTASYPROS_SOURCE, ROTOWIRE_SOURCE],
            }
        elif fp is not None:
            out[pid] = {**fp, "contributing_sources": [FANTASYPROS_SOURCE]}
        else:
            out[pid] = {**rw, "contributing_sources": [ROTOWIRE_SOURCE]}  # type: ignore[dict-item]
    return out


def consensus_source_label(projection: dict[str, Any]) -> Optional[str]:
    """Human-readable source for ONE consensus projection, e.g. "FantasyPros",
    "Rotowire", or "FantasyPros + Rotowire".

    None when the entry predates contributing_sources, so a caller falls back
    to the tier-level label rather than inventing one.
    """
    sources = projection.get("contributing_sources")
    if not sources:
        return None
    return " + ".join(sources)


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
