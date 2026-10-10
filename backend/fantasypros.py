"""FantasyPros free-tier consensus tier. See ARCHITECTURE.md §7."""

from __future__ import annotations

import time
from typing import Any, Optional

from scoring import compute_league_points

FANTASYPROS_COLUMN_MAP = {
    "pass_yds": "pass_yd",
    "pass_tds": "pass_td",
    "pass_ints": "pass_int",
    "rush_yds": "rush_yd",
    "rush_tds": "rush_td",
    "rec_yds": "rec_yd",
    "rec_tds": "rec_td",
    "rec_rec": "reception",
    "fumbles": "fumble_lost",
}

POSITIONS = ("QB", "RB", "WR", "TE")

FANTASYPROS_API_URL_TEMPLATE = "https://api.fantasypros.com/public/v2/json/nfl/{season}/projections"

REQUEST_DELAY_SECONDS = 2.0

RECEIVING_FIRST_DOWN_MODEL = {
    "QB": [-0.0002, 0.2505, 0.0381],
    "RB": [-0.0168, 0.0591, 0.0377],
    "WR": [-0.0172, 0.2872, 0.0253],
    "TE": [-0.0402, 0.1747, 0.0352],
}
RUSHING_FIRST_DOWN_MODEL = {
    "QB": [0.2881, 0.0610],
    "RB": [0.1275, 0.0480],
    "WR": [0.0110, 0.0426],
    "TE": [0.0147, 0.0565],
}
COMPLETIONS_MODEL = [3.3079, 0.0751]
ATTEMPTS_MODEL = [7.4872, 0.1047]
MEAN_SACKS_PER_GAME = 2.13


def _linear(model: list[float], *features: float) -> float:
    return max(0.0, model[0] + sum(m * f for m, f in zip(model[1:], features)))


def impute_unpublished_categories(
    stat_line: dict[str, float], position: Optional[str]
) -> dict[str, float]:
    """Estimate scored categories FantasyPros does not publish; never overwrites."""
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
    """Map a FantasyPros `stats` object to this project's stat-line keys."""
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
    """One raw FantasyPros player entry -> a projection dict."""
    stat_line = fantasypros_stats_to_stat_line(player.get("stats", {}))
    stat_line = impute_unpublished_categories(stat_line, position)
    result = compute_league_points(stat_line, scoring_config, position=position)
    return {
        "source": "consensus",
        "fpid": player.get("fpid"),
        # Not in stat_line: blending averages stat lines numerically.
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
    """Raw players by position -> {player_id: projection}, via the fpid crosswalk."""
    out: dict[str, dict[str, Any]] = {}
    for position, players in raw_players_by_position.items():
        for player in players:
            fpid = player.get("fpid")
            player_id = fpid_to_player_id.get(fpid)
            if player_id is None:
                continue
            out[player_id] = project_consensus_player(player, scoring_config, position)
    return out


def fetch_position_projections(season: int, week: int, position: str, api_key: str) -> list[dict[str, Any]]:
    """One FantasyPros API call for one position's top 10."""
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
    """One call per position, with a delay between calls for the rate limit."""
    out: dict[str, list[dict[str, Any]]] = {}
    for i, position in enumerate(positions):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        out[position] = fetch_position_projections(season, week, position, api_key)
    return out


def load_fantasypros_id_crosswalk_nflreadpy() -> dict[int, str]:
    """{fpid: gsis_id} from nflreadpy's load_ff_playerids()."""
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
