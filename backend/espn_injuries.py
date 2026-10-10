"""ESPN's league-wide injury report. See ARCHITECTURE.md §6."""

from __future__ import annotations

import re
from typing import Any, Optional

import requests

ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

FETCH_FAILURES: list[str] = []

STATUS_TO_REPORT_STATUS: dict[str, Optional[str]] = {
    "Active": None,
    "Questionable": "Questionable",
    "Doubtful": "Doubtful",
    "Out": "Out",
    "Injured Reserve": "Out",
    "Suspension": "Out",
}

_ESPN_ID_RE = re.compile(r"/id/(\d+)")


def _athlete_espn_id(athlete: dict[str, Any]) -> Optional[str]:
    """The athlete's ESPN id, parsed from the profile URL (athlete.id is null)."""
    for link in athlete.get("links") or []:
        match = _ESPN_ID_RE.search(link.get("href") or "")
        if match:
            return match.group(1)
    return None


def parse_injury_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ESPN's team-grouped response into one row per listed player."""
    rows: list[dict[str, Any]] = []
    for team in payload.get("injuries") or []:
        team_name = team.get("displayName")
        for entry in team.get("injuries") or []:
            athlete = entry.get("athlete") or {}
            name = athlete.get("displayName")
            if not name:
                continue
            injury_type = entry.get("type") or {}
            rows.append(
                {
                    "espn_id": _athlete_espn_id(athlete),
                    "name": name,
                    "team": team_name,
                    "position": (athlete.get("position") or {}).get("abbreviation"),
                    "status": entry.get("status"),
                    "report_status": STATUS_TO_REPORT_STATUS.get(entry.get("status")),
                    "date": entry.get("date"),
                    "detail": injury_type.get("description") or injury_type.get("name"),
                    "short_comment": entry.get("shortComment"),
                    "long_comment": entry.get("longComment"),
                }
            )
    return rows


def index_by_player(
    rows: list[dict[str, Any]], players: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """{playerId: injury row} for `players`, by ESPN id then lowercase name."""
    by_espn: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("espn_id"):
            by_espn.setdefault(str(row["espn_id"]), row)
        if row.get("name"):
            by_name.setdefault(row["name"].lower().strip(), row)

    out: dict[str, dict[str, Any]] = {}
    for player in players:
        espn_id = player.get("espnId")
        match = by_espn.get(str(espn_id)) if espn_id else None
        if match is None:
            match = by_name.get((player.get("name") or "").lower().strip())
        if match is not None:
            out[player["playerId"]] = match
    return out


def news_articles_from_injury(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The injury row in news.summarize_player_news' article shape."""
    headline = row.get("short_comment") or row.get("detail") or row.get("status")
    body = row.get("long_comment") or row.get("short_comment") or ""
    if not headline and not body:
        return []
    status = row.get("status")
    detail = row.get("detail")
    prefix = " / ".join(x for x in (status, detail) if x)
    return [
        {
            "headline": f"[ESPN injury report{': ' + prefix if prefix else ''}] {headline}",
            "description": body,
        }
    ]


def fetch_injury_rows(timeout: int = 40) -> list[dict[str, Any]]:
    """Every listed injury, or [] on any failure."""
    try:
        response = requests.get(ESPN_INJURIES_URL, headers=_HEADERS, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        FETCH_FAILURES.append(f"{type(exc).__name__}: {exc}")
        return []
    if not isinstance(payload, dict):
        FETCH_FAILURES.append(f"unexpected payload type {type(payload).__name__}")
        return []
    rows = parse_injury_payload(payload)
    if not rows:
        FETCH_FAILURES.append("payload parsed but contained no injury rows")
    return rows
