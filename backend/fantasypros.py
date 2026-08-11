"""
FantasyPros free-tier consensus-projection tier -- the top-10-per-position
half of the two-tier value engine (CLAUDE.md v1 Scope item 1). Wires the
already-confirmed FantasyPros API (docs/research/phase1-probe-results.md)
into generate_report.py, closing v8 Next Steps item 3.

Same "never trust a vendor's precomputed points field" rule the in-house
tier follows (scoring.py's module docstring): this module pulls
FantasyPros' raw projected *stat line*, maps it into this project's
stat_line category names, and recomputes points via
scoring.compute_league_points using the league's real scoring config --
FantasyPros' own `points`/`points_ppr`/`points_half` fields are read only
to confirm the response shape, never used as the projection itself.

Split, like every other module here, into pure/testable assembly (top
half -- exercised by tests/test_fantasypros.py with synthetic API-response
fixtures, no network) and real network/data adapters (bottom half,
`load_*`/`fetch_*`), which are NOT exercised by the test suite.
"""

from __future__ import annotations

import time
from typing import Any

from scoring import compute_league_points

# Confirmed hands-on 2026-08-11 against the real API (season=2025, week=1,
# one live call per position) for QB, RB, WR, and TE. A `position=ALL`
# query was tried first and rejected: it caps at 10 players *total* across
# every position (not 10 per position) -- confirmed separately, an ALL
# query returned only 10 players combined, dominated by QBs -- so
# fetch_consensus_tier below makes one call per position instead.
#
# pass_att/pass_cmp/rush_att and the API's own pre-flagged milestone
# booleans (pass_yds_300, pass_yds_400, rush_yds_100, rush_yds_200,
# rec_yds_100, rec_yds_200, scrimage_yards_100/200) are present in the real
# response but deliberately unmapped here -- scoring.py's own milestone
# logic recomputes bonuses from raw yardage (pass_yd/rush_yd/rec_yd)
# against scoring_config, so a separately pre-computed boolean would be
# redundant, not an input this engine needs. 2pt_tds and ret_tds are also
# unmapped: 2pt_tds is a single combined pass/rush/rec total with no way to
# split it into this config's separate pass_2pt/rush_2pt/rec_2pt
# categories, and ret_tds (return TDs) isn't an individual skill-position
# scoring category in this project's schema.
FANTASYPROS_COLUMN_MAP = {
    "pass_yds": "pass_yd",
    "pass_tds": "pass_td",
    "pass_ints": "pass_int",
    "rush_yds": "rush_yd",
    "rush_tds": "rush_td",
    "rec_yds": "rec_yd",
    "rec_tds": "rec_td",
    "rec_rec": "reception",
    # FantasyPros' "fumbles" field isn't documented as lost-vs-total, and no
    # separate "lost" field showed up in any of the four confirmed live
    # responses (QB/RB/WR/TE) -- mapped to fumble_lost since that's the
    # only offensive fumble category scoring_config.placeholder.json
    # currently defines (see its _note). Revisit once real league values
    # land (CLAUDE.md Next Steps item 1) and it's clear whether a
    # non-lost "Fumble" category exists at all (Next Steps item 2b).
    "fumbles": "fumble_lost",
}

POSITIONS = ("QB", "RB", "WR", "TE")  # mirrors generate_report.POOL_POSITIONS

FANTASYPROS_API_URL_TEMPLATE = "https://api.fantasypros.com/public/v2/json/nfl/{season}/projections"

# Free tier rate-limits (confirmed: hit a 429 during phase-1 testing) --
# small delay between the position-by-position calls fetch_consensus_tier
# makes, per docs/research/phase1-probe-results.md's operational note.
REQUEST_DELAY_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Pure assembly logic -- exercised by tests/test_fantasypros.py with
# synthetic API-response fixtures, no network involved.
# ---------------------------------------------------------------------------


def fantasypros_stats_to_stat_line(stats: dict[str, Any]) -> dict[str, float]:
    """Map one player's FantasyPros `stats` object to this module's
    stat_line shape via FANTASYPROS_COLUMN_MAP -- same pattern as
    scoring.nflreadpy_row_to_stat_line."""
    out: dict[str, float] = {}
    for fp_key, our_key in FANTASYPROS_COLUMN_MAP.items():
        val = stats.get(fp_key)
        if not val:
            continue
        out[our_key] = out.get(our_key, 0) + val
    return out


def project_consensus_player(player: dict[str, Any], scoring_config: dict[str, Any]) -> dict[str, Any]:
    """One FantasyPros player entry (the raw `{"fpid", "name", "position_id",
    "team_id", "stats": {...}}` shape the API returns) -> a projection dict.
    Player identity (name/position/team) is deliberately NOT re-derived
    here -- generate_report.py already has authoritative identity for
    every pool player from nflreadpy's roster snapshot, keyed by the same
    player_id this tier resolves to via the crosswalk, so re-emitting
    FantasyPros' own (differently-formatted) team/position strings would
    just be a second, potentially-disagreeing source of truth for the same
    fact."""
    stat_line = fantasypros_stats_to_stat_line(player.get("stats", {}))
    result = compute_league_points(stat_line, scoring_config)
    return {
        "source": "consensus",
        "fpid": player.get("fpid"),
        "projected_points": result.total,
        "stat_line": stat_line,
        "breakdown": result.breakdown,
    }


def build_consensus_tier(
    raw_players_by_position: dict[str, list[dict[str, Any]]],
    scoring_config: dict[str, Any],
    fpid_to_player_id: dict[int, str],
) -> dict[str, dict[str, Any]]:
    """The full pure assembly step: raw FantasyPros players (grouped by
    position, the shape fetch_consensus_tier returns) -> {player_id:
    projection dict}, keyed by this project's own player_id (gsis_id), via
    fpid_to_player_id (see load_fantasypros_id_crosswalk_nflreadpy).

    A player whose fpid isn't in the crosswalk is silently dropped, not an
    error -- the crosswalk has a known gap for very recently drafted
    rookies (docs/research/phase1-probe-results.md's rookie edge-case
    finding on `fantasypros_id`), and FantasyPros' free tier only returns
    each position's most established top 10 anyway, so an unresolved id
    here just means that player falls back to the in-house tier instead of
    the whole report generation failing."""
    out: dict[str, dict[str, Any]] = {}
    for players in raw_players_by_position.values():
        for player in players:
            fpid = player.get("fpid")
            player_id = fpid_to_player_id.get(fpid)
            if player_id is None:
                continue
            out[player_id] = project_consensus_player(player, scoring_config)
    return out


# ---------------------------------------------------------------------------
# Real data adapters -- NOT exercised by the test suite (network required).
# Confirmed working end-to-end 2026-08-11: the API calls below against real
# 2025-season data for QB/RB/WR/TE, and the crosswalk against a real
# nflreadpy load_ff_playerids() pull (Saquon Barkley's real fpid 17240
# resolved to his real gsis_id 00-0034844).
# ---------------------------------------------------------------------------


def fetch_position_projections(season: int, week: int, position: str, api_key: str) -> list[dict[str, Any]]:
    """One FantasyPros API call for one position's top 10 (or fewer, if the
    real slate has fewer than 10 startable players) projected players.
    `scoring` is intentionally not sent -- confirmed to have no effect on
    the free tier (every response already includes points/points_ppr/
    points_half together, per CLAUDE.md's Data Sources table), and this
    module doesn't read any of those fields anyway."""
    import requests

    resp = requests.get(
        FANTASYPROS_API_URL_TEMPLATE.format(season=season),
        params={"position": position, "week": week},
        headers={"x-api-key": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("players", [])


def fetch_consensus_tier(
    season: int, week: int, api_key: str, positions: tuple[str, ...] = POSITIONS
) -> dict[str, list[dict[str, Any]]]:
    """One call per position (see FANTASYPROS_COLUMN_MAP's module-level note
    on why `position=ALL` doesn't work for this), with a delay between
    calls to stay under the free tier's rate limit."""
    out: dict[str, list[dict[str, Any]]] = {}
    for i, position in enumerate(positions):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        out[position] = fetch_position_projections(season, week, position, api_key)
    return out


def load_fantasypros_id_crosswalk_nflreadpy() -> dict[int, str]:
    """fpid (int, FantasyPros' player identifier) -> this project's
    player_id (gsis_id), via nflreadpy's load_ff_playerids() -- a thin
    wrapper around DynastyProcess's db_playerids.csv (CLAUDE.md's Data
    Sources "Player-ID crosswalk" row), confirmed to carry both ids
    together. `fantasypros_id` comes back as a float (e.g. 17240.0) even
    though the API's `fpid` is an int -- cast both sides to int before
    keying the dict. Rows missing either id are dropped, not coerced -- a
    player with no gsis_id (not yet debuted, or out of the league
    entirely) has nothing in this project's pool to key by anyway."""
    import nflreadpy as nfl

    try:
        df = nfl.load_ff_playerids()
        df = df.to_pandas() if hasattr(df, "to_pandas") else df
        df = df.dropna(subset=["fantasypros_id", "gsis_id"])
        return {int(row["fantasypros_id"]): row["gsis_id"] for _, row in df.iterrows()}
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_fantasypros_id_crosswalk_nflreadpy: nflreadpy's load_ff_playerids() "
            "response shape didn't match this adapter's assumptions (expected columns "
            "include 'fantasypros_id', 'gsis_id'). Check nflreadpy's actual column names "
            "for your installed version and update this function."
        ) from exc
