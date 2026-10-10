"""Rotowire weekly-projections consensus feed. See ARCHITECTURE.md §7."""

from __future__ import annotations

import time
from typing import Any, Optional

from fantasypros import impute_unpublished_categories
from scoring import compute_league_points

FANTASYPROS_SOURCE = "FantasyPros"
ROTOWIRE_SOURCE = "Rotowire"

ROTOWIRE_URL = "https://www.rotowire.com/football/tables/weekly-projections.php"
ROTOWIRE_REFERER = "https://www.rotowire.com/football/projections-weekly.php"

POSITIONS = ("QB", "RB", "WR", "TE")

REQUEST_DELAY_SECONDS = 1.0

ROTOWIRE_COLUMN_MAP_QB: dict[str, str] = {
    "offpassyard": "pass_yd",
    "offpasstd": "pass_td",
    "offpassint": "pass_int",
    "offpasscomp": "pass_completion",
    "offpassatt": "pass_att",
    "offrushyard": "rush_yd",
    "offrushtd": "rush_td",
    "offrushatt": "rush_att",
}

ROTOWIRE_COLUMN_MAP_SKILL: dict[str, str] = {
    "offrushatt": "rush_att",
    "offrushyard": "rush_yd",
    "offrushtd": "rush_td",
    "offrecatt": "reception",
    "offrecyard": "rec_yd",
    "offrectd": "rec_td",
}


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
    attempts = out.pop("pass_att", 0.0)
    if attempts:
        incomplete = attempts - out.get("pass_completion", 0.0)
        if incomplete > 0:
            out["pass_incompletion"] = incomplete
    return out


def project_rotowire_player(row: dict[str, Any], position: str, scoring_config: dict[str, Any]) -> dict[str, Any]:
    stat_line = rotowire_stats_to_stat_line(row, position)
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
    """Raw Rotowire rows -> {player_id: projection}, matched by name and team."""
    out: dict[str, dict[str, Any]] = {}
    for position, players in raw_by_position.items():
        for row in players:
            full_name = f"{row.get('firstname', '')} {row.get('lastname', '')}".strip()
            team = (row.get("team") or "").upper()
            player_id = name_team_to_player_id.get((full_name.lower(), team))
            if player_id is None:
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
    """Average FantasyPros and Rotowire stat lines where both exist, then score once."""
    all_ids = set(fp_projections) | set(rw_projections)
    out: dict[str, dict[str, Any]] = {}
    for pid in all_ids:
        fp = fp_projections.get(pid)
        rw = rw_projections.get(pid)
        if fp is not None and rw is not None:
            all_keys = set(fp["stat_line"]) | set(rw["stat_line"])
            averaged: dict[str, float] = {}
            for k in all_keys:
                a = fp["stat_line"].get(k, 0.0)
                b = rw["stat_line"].get(k, 0.0)
                if isinstance(a, str) or isinstance(b, str):
                    averaged[k] = a or b
                    continue
                averaged[k] = (a + b) / 2.0
            # Score with a position: league-1 pays TE receptions double.
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
    """"FantasyPros", "Rotowire" or both, for one projection; None if unknown."""
    sources = projection.get("contributing_sources")
    if not sources:
        return None
    return " + ".join(sources)


def fetch_rotowire_projections(
    week: int,
    positions: tuple[str, ...] = POSITIONS,
) -> dict[str, list[dict[str, Any]]]:
    """{position: [row]}, one HTTP call per position; [] for a failed one."""
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
            for row in rows:
                row.setdefault("position", pos)
            out[pos] = rows
        except Exception as exc:  # noqa: BLE001
            print(f"  Rotowire projections fetch failed for {pos} ({exc}) — skipping that position.")
            out[pos] = []
    return out


def build_name_team_crosswalk(pool: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """(normalised name, TEAM) -> player_id, plus name-only fallback keys."""
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
