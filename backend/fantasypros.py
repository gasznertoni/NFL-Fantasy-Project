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
from typing import Any, Optional

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


# ---------------------------------------------------------------------------
# Imputation for categories FantasyPros does not publish.
#
# Added 2026-09-06, when league-1's final ESPN settings began scoring rushing
# and receiving first downs (0.75 each), completions (+0.1), incompletions
# (-0.1) and sacks taken (-0.5). FantasyPros' free-tier projection carries
# yards/TDs/receptions/interceptions/fumbles and nothing else, so under the new
# config a consensus-tier stat line silently loses real points -- measured on
# 2025 actuals: RB 3.29 and WR 2.98 pts/game, roughly a third of an RB's score.
#
# That matters more than the absolute error: the consensus tier and the
# in-house tier are ranked against each other for start/sit advice, so a bias
# that hits only one of them makes the two numbers non-comparable. Leaving the
# categories absent is NOT the safe default here -- "absent contributes 0" is
# correct for a rule a league does not have, and wrong for a rule it does.
#
# Least squares on 2022-25 load_player_stats(), one fit per position, keyed on
# only the fields FantasyPros actually publishes. Fit quality (R2 / MAE in
# natural units): receiving first downs QB .665/.003, RB .702/.287,
# WR .842/.473, TE .803/.431; rushing first downs QB .662/.643, RB .763/.669,
# WR .594/.053, TE .646/.038; completions .780/3.00 and attempts .703/5.24 off
# passing yards. Net effect at WR: a ~3 pt/game systematic bias traded for
# ~0.35 pts of noise.
#
# Sacks are the exception and are deliberately a CONSTANT: passing yards carry
# essentially no signal about sacks taken (R2 = 0.043), so a regression here
# would be false precision. 2.13 is the 2022-25 per-game mean for a QB with at
# least one attempt.
#
# Coefficients are [intercept, ...] in the order named by each comment. These
# are estimates of a projection's expected value, not of any one game.
# ---------------------------------------------------------------------------
# [intercept, per reception, per receiving yard]
RECEIVING_FIRST_DOWN_MODEL = {
    "QB": [-0.0002, 0.2505, 0.0381],
    "RB": [-0.0168, 0.0591, 0.0377],
    "WR": [-0.0172, 0.2872, 0.0253],
    "TE": [-0.0402, 0.1747, 0.0352],
}
# [intercept, per rushing yard] -- carries are not published, so yards only.
RUSHING_FIRST_DOWN_MODEL = {
    "QB": [0.2881, 0.0610],
    "RB": [0.1275, 0.0480],
    "WR": [0.0110, 0.0426],
    "TE": [0.0147, 0.0565],
}
# [intercept, per passing yard]
COMPLETIONS_MODEL = [3.3079, 0.0751]
ATTEMPTS_MODEL = [7.4872, 0.1047]
MEAN_SACKS_PER_GAME = 2.13


def _linear(model: list[float], *features: float) -> float:
    return max(0.0, model[0] + sum(m * f for m, f in zip(model[1:], features)))


def impute_unpublished_categories(
    stat_line: dict[str, float], position: Optional[str]
) -> dict[str, float]:
    """Add the scoring categories FantasyPros does not publish, estimated from
    the ones it does. Returns a new dict; the input is not mutated.

    Safe to run for every league: it only adds stat-line keys, and a league
    whose config does not define a category ignores it entirely (league-2 has
    no first-down, completion or sack line, so this is a no-op there).

    Only fills a key that is absent, so a real published value always wins."""
    out = dict(stat_line)
    receptions = out.get("reception", 0) or 0
    rec_yd = out.get("rec_yd", 0) or 0
    rush_yd = out.get("rush_yd", 0) or 0
    pass_yd = out.get("pass_yd", 0) or 0

    if position in RECEIVING_FIRST_DOWN_MODEL and (receptions or rec_yd):
        out.setdefault("rec_first_down",
                       _linear(RECEIVING_FIRST_DOWN_MODEL[position], receptions, rec_yd))
    if position in RUSHING_FIRST_DOWN_MODEL and rush_yd:
        out.setdefault("rush_first_down",
                       _linear(RUSHING_FIRST_DOWN_MODEL[position], rush_yd))
    if pass_yd:
        completions = _linear(COMPLETIONS_MODEL, pass_yd)
        attempts = _linear(ATTEMPTS_MODEL, pass_yd)
        out.setdefault("pass_completion", completions)
        out.setdefault("pass_incompletion", max(0.0, attempts - completions))
        out.setdefault("pass_sacked", MEAN_SACKS_PER_GAME)
    return out


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


def project_consensus_player(
    player: dict[str, Any],
    scoring_config: dict[str, Any],
    position: Optional[str] = None,
) -> dict[str, Any]:
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
    stat_line = impute_unpublished_categories(stat_line, position)
    result = compute_league_points(stat_line, scoring_config, position=position)
    return {
        "source": "consensus",
        "fpid": player.get("fpid"),
        # Carried at the top level, NOT inside stat_line: stat lines are
        # averaged key-by-key when the two consensus feeds are blended, and a
        # string in there breaks that arithmetic.
        "position": position,
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
    for position, players in raw_players_by_position.items():
        for player in players:
            fpid = player.get("fpid")
            player_id = fpid_to_player_id.get(fpid)
            if player_id is None:
                continue
            # The position key is the grouping this dict is built on, and is
            # needed twice over now: to pick the right imputation model, and
            # because league-1 prices a TE reception at double an RB/WR one.
            out[player_id] = project_consensus_player(player, scoring_config, position)
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
