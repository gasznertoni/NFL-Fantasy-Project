"""Rotowire red-zone and route-efficiency signals. See ARCHITECTURE.md §8."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

ROTOWIRE_PLAYER_URL = "https://www.rotowire.com/football/ajax/player-page-data.php"

ROTOWIRE_POSITIONS = {"QB", "RB", "WR", "TE", "K"}

FETCH_DELAY_SECS = 0.5
REQUEST_TIMEOUT = 10
CACHE_TTL_HOURS = 24

MIN_RZ_TOUCHES_FOR_NOTE = 1.0
MIN_TPRR_FOR_NOTE = 0.20

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.rotowire.com/football/",
}


def load_id_crosswalk() -> dict[str, str]:
    """{gsis_id: rotowire_id} from DynastyProcess's player ID table; {} on failure."""
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
                result[str(gsis_id)] = str(int(float(rw_str)))
            except (ValueError, TypeError):
                pass
        return result
    except Exception:
        return {}


def fetch_player_gamelog(
    rotowire_id: str,
    pos: str,
    team: str,
    season_year: int = 2025,
    opp: str = "KC",
    timeout: int = REQUEST_TIMEOUT,
) -> Optional[list[dict[str, Any]]]:
    """One player's current-season Rotowire game log, or None on failure."""
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
    """Average goal-line touches over the last n non-DNP games, or None."""
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
    """Average targets per route run over the last n non-DNP games, as a fraction."""
    active = [r for r in gamelog_rows if not r.get("dnp")]
    recent = active[-n_recent:]
    values = [v for v in (_safe_float(r.get("tprr")) for r in recent) if v is not None]
    if not values:
        return None
    return (sum(values) / len(values)) / 100.0


def compute_player_rz_stats(
    gamelog_rows: list[dict[str, Any]],
    n_recent: int = 3,
) -> dict[str, Any]:
    """Rotowire signals for one player, in waiver_targets' shape."""
    return {
        "rz_touches_per_game": rz_touches_per_game(gamelog_rows, n_recent),
        "tprr_recent": tprr_recent(gamelog_rows, n_recent),
    }


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
    """Rotowire stats for the whole pool, via a 24-hour disk cache."""
    existing_gamelogs: dict[str, list[dict[str, Any]]] = {}
    cache_fresh = False
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            existing_gamelogs = cached.get("gamelogs", {})
            cache_fresh = _cache_is_fresh(cached, max_age_hours)
        except (json.JSONDecodeError, OSError):
            pass

    eligible: dict[str, dict[str, Any]] = {
        p["playerId"]: p
        for p in pool
        if p.get("position") in ROTOWIRE_POSITIONS and crosswalk.get(p["playerId"])
    }

    to_fetch: dict[str, dict[str, Any]]
    if cache_fresh:
        to_fetch = {gid: p for gid, p in eligible.items() if gid not in existing_gamelogs}
    else:
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
            pass

    return {
        gsis_id: compute_player_rz_stats(gl_rows)
        for gsis_id, gl_rows in existing_gamelogs.items()
        if gsis_id in eligible
    }
