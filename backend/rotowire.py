"""
Rotowire data integration: per-game red zone usage and route efficiency.

Provides two signals not available in nflreadpy's weekly player stats:
  - Goal-line touches (rzTargets5 + rzRush5 per game): direct TD-opportunity
    signal, especially useful for RBs and TEs competing for short-yardage work.
  - tprr (targets per route run): how often a WR/TE gets targeted when on
    the field -- a purer target-share signal than wopr, which mixes volume
    (absolute targets) with route participation.

Both are surfaced as annotations in waiver target rationale text. They do not
currently alter projected points -- that would require a backtest round to tune
the multiplier, the same discipline matchup.py/usage.py already established.

Endpoint (undocumented, no auth required):
  https://www.rotowire.com/football/ajax/player-page-data.php
  ?id={rotowire_id}&pos={QB|RB|WR|TE|K}&opp={any}&team={team_abbr}
Returns gl{YEAR}['body']: list of per-game dicts, one entry per regular-season
week played. The opp parameter is required but does not affect the season-level
game log data (confirmed empirically -- any valid abbreviation works).

Player ID source: DynastyProcess db_playerids via nflreadpy.load_ff_playerids().
`rotowire_id` column confirmed present for 4,740 skill-position players, 0 gaps
(2026-09-01 probe). ID for a player is also the trailing number in their
Rotowire URL: /football/player/{name}-{id}.

Fetch strategy: cache all responses to rotowire_cache_{season}.json in the
backend directory (TTL 24 hours). Repeated runs within the same day make 0
network calls. Stale or missing cache triggers incremental fetches for players
not yet in it, with a 0.5-second delay between requests.

Degrades gracefully throughout -- any failure returns None/empty rather than
raising, matching news.py's "never take down the report pipeline" contract.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

ROTOWIRE_PLAYER_URL = "https://www.rotowire.com/football/ajax/player-page-data.php"

# Positions the endpoint supports and that have meaningful stats. DST is
# excluded: DST is a team entity with its own data pipeline (dst.py), and
# Rotowire's endpoint is player-centric.
ROTOWIRE_POSITIONS = {"QB", "RB", "WR", "TE", "K"}

FETCH_DELAY_SECS = 0.5
REQUEST_TIMEOUT = 10
CACHE_TTL_HOURS = 24

# Minimum goal-line touches per game (recent average) to include the RZ note
# in a waiver rationale. Below this, the signal is too noisy to be actionable.
MIN_RZ_TOUCHES_FOR_NOTE = 1.0
# Minimum tprr to flag as a "rising target share" signal (20% = getting
# targeted on 1 in 5 routes -- a genuinely high rate in real data).
MIN_TPRR_FOR_NOTE = 0.20

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.rotowire.com/football/",
}


# ---------------------------------------------------------------------------
# ID crosswalk
# ---------------------------------------------------------------------------

def load_id_crosswalk() -> dict[str, str]:
    """Return {gsis_id: rotowire_id} from DynastyProcess's player ID table.

    Uses nflreadpy.load_ff_playerids() which wraps the DynastyProcess
    db_playerids.csv (CLAUDE.md Data Sources table). The `rotowire_id` column
    is confirmed present and populated for all skill-position players in the
    2025 pool.

    Returns {} on any failure -- callers treat a missing crosswalk as "no
    Rotowire data this run," same graceful-miss contract as every other
    data-source failure in this project.
    """
    try:
        import nflreadpy as nfl

        df = nfl.load_ff_playerids()
        if hasattr(df, "to_pandas"):
            df = df.to_pandas()
        result: dict[str, str] = {}
        for _, row in df.iterrows():
            gsis_id = row.get("gsis_id")
            rotowire_id = row.get("rotowire_id")
            if not gsis_id or not rotowire_id:
                continue
            rw_str = str(rotowire_id).strip()
            if rw_str in ("", "nan", "None"):
                continue
            try:
                # rotowire_id comes through as a float (e.g. 16808.0) -- int-cast
                # strips the decimal so the URL parameter is a plain integer.
                result[str(gsis_id)] = str(int(float(rw_str)))
            except (ValueError, TypeError):
                pass
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Per-player fetch
# ---------------------------------------------------------------------------

def fetch_player_gamelog(
    rotowire_id: str,
    pos: str,
    team: str,
    season_year: int = 2025,
    opp: str = "KC",
    timeout: int = REQUEST_TIMEOUT,
) -> Optional[list[dict[str, Any]]]:
    """Fetch the current-season game log for one player from Rotowire.

    Returns gl{season_year}['body'] on success, None on any failure.

    The endpoint requires `opp` and `team` but does not use them to filter
    the season-level game log -- any valid team abbreviation works. `team`
    is passed as the player's real team (better practice); `opp` defaults to
    a dummy value since we don't need to know the specific opponent here.
    """
    season_key = f"gl{season_year}"
    try:
        r = requests.get(
            ROTOWIRE_PLAYER_URL,
            params={"id": rotowire_id, "pos": pos, "team": team, "opp": opp},
            headers=_HEADERS,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    gl = data.get(season_key)
    if isinstance(gl, dict):
        body = gl.get("body")
        return body if isinstance(body, list) else None
    if isinstance(gl, list):
        return gl
    return None


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------

def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        f = float(value)
        return None if f != f else f  # filter NaN
    except (TypeError, ValueError):
        return None


def rz_touches_per_game(
    gamelog_rows: list[dict[str, Any]],
    n_recent: int = 3,
) -> Optional[float]:
    """Average goal-line touches (rzTargets5 + rzRush5) over the last n
    non-DNP games.

    Returns None when there are no rows -- distinguishes "no Rotowire data"
    from "zero red zone usage" (both suppress the rationale note, but for
    different reasons).
    """
    active = [r for r in gamelog_rows if not r.get("dnp")]
    recent = active[-n_recent:] if n_recent > 0 else []
    if not recent:
        return None
    total = 0.0
    for r in recent:
        t = _safe_float(r.get("rzTargets5")) or 0.0
        ru = _safe_float(r.get("rzRush5")) or 0.0
        total += t + ru
    return total / len(recent)


def tprr_recent(
    gamelog_rows: list[dict[str, Any]],
    n_recent: int = 3,
) -> Optional[float]:
    """Average targets-per-route-run over the last n non-DNP games, as a
    decimal fraction (e.g. 0.25 = 25% target rate).

    Rotowire stores tprr as a percentage string (e.g. "19.2" meaning 19.2%);
    this function divides by 100 so the result is in [0, 1] and consistent with
    Python's standard fraction representation -- callers can format with :.0%
    and compare against the MIN_TPRR_FOR_NOTE threshold without unit confusion.

    Returns None when tprr is absent (QBs/RBs who don't run pass routes) or
    when there is no data.
    """
    active = [r for r in gamelog_rows if not r.get("dnp")]
    recent = active[-n_recent:]
    values = [v for v in (_safe_float(r.get("tprr")) for r in recent) if v is not None]
    if not values:
        return None
    # Rotowire tprr is already a percentage (e.g. 19.2 = 19.2%) -- normalize
    # to a decimal fraction so 0.20 thresholds and :.0% formatting both work.
    return (sum(values) / len(values)) / 100.0


def compute_player_rz_stats(
    gamelog_rows: list[dict[str, Any]],
    n_recent: int = 3,
) -> dict[str, Any]:
    """Aggregate Rotowire signals for one player into a dict that
    waiver_targets.generate_rationale can read directly."""
    return {
        "rz_touches_per_game": rz_touches_per_game(gamelog_rows, n_recent),
        "tprr_recent": tprr_recent(gamelog_rows, n_recent),
    }


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

def _cache_is_fresh(cache: dict[str, Any], max_age_hours: int) -> bool:
    fetched_at = cache.get("fetched_at")
    if not fetched_at:
        return False
    try:
        age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
        ).total_seconds()
        return age < max_age_hours * 3600
    except Exception:
        return False


def fetch_and_cache_pool_stats(
    pool: list[dict[str, Any]],
    crosswalk: dict[str, str],
    cache_path: Path,
    season_year: int = 2025,
    max_age_hours: int = CACHE_TTL_HOURS,
    delay_secs: float = FETCH_DELAY_SECS,
) -> dict[str, dict[str, Any]]:
    """Fetch Rotowire game logs for the whole player pool, with a disk cache.

    Cache format (cache_path JSON):
        {"fetched_at": ISO timestamp, "gamelogs": {gsis_id: [gl_rows]}}

    A fresh cache (within max_age_hours) is reused as-is. A stale or missing
    cache triggers incremental fetches for players not yet stored, with
    delay_secs between requests to avoid hammering the endpoint.

    Returns {gsis_id: rz_stats_dict} for every player that could be fetched.
    Players not in the crosswalk, in unsupported positions, or whose fetch
    failed are absent from the result -- callers should .get(player_id).

    Args:
        pool: the player pool list, each entry {"playerId", "position", "team"}.
        crosswalk: {gsis_id: rotowire_id} from load_id_crosswalk().
        cache_path: file path for the JSON cache. Written after each batch.
        season_year: the season whose game log key to read (gl2025 etc.).
        max_age_hours: how old the cache can be before triggering a re-fetch.
        delay_secs: sleep between individual HTTP requests.
    """
    # Load existing cache
    existing_gamelogs: dict[str, list[dict[str, Any]]] = {}
    cache_fresh = False
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            existing_gamelogs = cached.get("gamelogs", {})
            cache_fresh = _cache_is_fresh(cached, max_age_hours)
        except (json.JSONDecodeError, OSError):
            pass

    # Players this run should cover (have a crosswalk entry + supported pos)
    eligible: dict[str, dict[str, Any]] = {
        p["playerId"]: p
        for p in pool
        if p.get("position") in ROTOWIRE_POSITIONS and crosswalk.get(p["playerId"])
    }

    to_fetch: dict[str, dict[str, Any]]
    if cache_fresh:
        # Incremental: only players newly added to the pool since last cache
        to_fetch = {gid: p for gid, p in eligible.items() if gid not in existing_gamelogs}
    else:
        # Stale/missing: full refresh
        to_fetch = eligible

    if to_fetch:
        print(f"  Fetching Rotowire game logs for {len(to_fetch)} players...")
        fetched = 0
        for gsis_id, player in to_fetch.items():
            rw_id = crosswalk[gsis_id]
            pos = player.get("position", "RB")
            team = player.get("team") or "KC"
            gl = fetch_player_gamelog(rw_id, pos, team, season_year=season_year)
            if gl is not None:
                existing_gamelogs[gsis_id] = gl
                fetched += 1
            time.sleep(delay_secs)
        print(f"    {fetched}/{len(to_fetch)} fetched successfully.")

        try:
            cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "gamelogs": existing_gamelogs,
                    },
                    indent=2,
                )
                + "\n"
            )
        except OSError:
            pass  # non-fatal: we still return the in-memory result

    return {
        gsis_id: compute_player_rz_stats(gl_rows)
        for gsis_id, gl_rows in existing_gamelogs.items()
        if gsis_id in eligible
    }
