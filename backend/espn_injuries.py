"""
ESPN's league-wide injury report -- the richest live availability source
this project has.

What it is: the JSON behind https://www.espn.com/nfl/injuries, one entry per
listed player per team, each with a status, a date, and beat-reporter
commentary. Same unofficial-ESPN-API family as news.py's news endpoint, which
this project already depends on and already treats as "free, unstable by
nature, must degrade gracefully".

Why it earns a place next to the two sources already wired in:

    source      coverage of the 904-player 2026 pool   what it carries
    nflreadpy   0 (caps at the last COMPLETED season)  practice participation
    Sleeper     167                                    designation + body part
    ESPN        449                                    status + dated commentary

449 of 904 is nearly three times Sleeper's coverage, and the commentary is the
part that matters for flagging: "Love has been tending to a high-ankle sprain
since making his pro debut preseason Week 1, missing all practices and the
Cardinals' final two exhibitions" is a different quality of signal from
Sleeper's "Ankle".

It also feeds the news layer far better than the general news endpoint does.
That returns 50 league-wide articles which name-match only 83 of the pool;
this returns per-player injury text for 449, already about the right subject.

Two things it does NOT have, which is why the other sources stay:
  * practice participation -- the feature that splits a Questionable from 0.51
    (did not practise) to 0.79 (full), so nflreadpy still wins where it has
    the season;
  * a populated `athlete.id` -- the field exists but is null on every entry.
    The ESPN id has to be parsed out of the athlete's profile URL instead.
    That is fragile by nature, so the join falls back to name matching.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import requests

ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"

# A descriptive User-Agent is the polite default, but ESPN answers this
# endpoint with 403 for one and 200 for a browser string -- verified directly:
# "Mozilla/5.0 (compatible; nfl-fantasy-value-assistant/1.0)" -> 403,
# "Mozilla/5.0 ..." -> 200 with the full 8.9 MB payload. Matches rotowire.py,
# which reaches the same conclusion for the same reason.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

# Populated when a fetch fails, so a caller can say WHY there were no rows.
# Returning a bare [] and moving on is how a 403 looked identical to "nobody
# is injured" -- the same silent-degrade shape this project has now been
# bitten by three times (scoring column maps, news JSON parsing, and this).
FETCH_FAILURES: list[str] = []

# ESPN's status vocabulary -> availability.REPORT_STATUSES, with observed
# counts from the 2026 pre-season pull (800 entries):
#
#   Active           411   listed for injury NEWS but expected to play
#   Injured Reserve  194   unavailable
#   Questionable     154   genuinely uncertain
#   Out               37   unavailable
#   Suspension         4   unavailable (not injury, but still not playing)
#
# "Active" maps to None deliberately: the player appears here because there is
# news about them, not because their availability is in doubt. Treating that as
# a designation would penalise every player ESPN happens to have written about.
#
# Injured Reserve and Suspension both collapse to "Out" for the same reason
# availability.py collapses Sleeper's IR: "Out" is the level the model has
# actually calibrated on (P(play) = 0.0006), and the answer for all three is
# already ~0. A separate level would extrapolate a coefficient from no data.
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
    """The athlete's ESPN id, parsed out of their profile URL.

    `athlete.id` is present in the schema but null on every entry observed, so
    the id is recovered from links like
    https://www.espn.com/nfl/player/_/id/4870808/jeremiyah-love. Returns None
    rather than raising when the shape changes -- the caller falls back to a
    name match.
    """
    for link in athlete.get("links") or []:
        match = _ESPN_ID_RE.search(link.get("href") or "")
        if match:
            return match.group(1)
    return None


def parse_injury_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ESPN's team-grouped response into one row per listed player.

    Pure, so the parsing is unit-testable without the network. Rows carry both
    join keys (espn_id, name) and let the caller decide precedence.
    """
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
    """{playerId: injury row} for the players in `players`.

    ESPN id first, then lowercased name -- the id is the reliable key when the
    URL parse works, and the name catches the rest. On the 2026 pool that is
    435 by id and 14 more by name, out of 800 ESPN entries (the remainder are
    defensive players who are not in a fantasy pool at all).

    Later rows do not overwrite earlier ones for the same player: ESPN lists a
    player once, and if that ever changes the first entry is the one at the top
    of the team's list, which is the most recent.
    """
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
    """The injury row as news.summarize_player_news' article shape.

    This is the reason the module is worth wiring into the news layer and not
    only into availability: the commentary here is already about the right
    player and the right subject, where the general news endpoint returns 50
    league-wide articles that have to be name-matched and are usually about
    someone else.
    """
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


# ---------------------------------------------------------------------------
# Network adapter -- NOT exercised by the test suite.
# ---------------------------------------------------------------------------
def fetch_injury_rows(timeout: int = 40) -> list[dict[str, Any]]:
    """Every listed injury, or [] on any failure.

    Degrades to [] rather than raising, same contract as news.py's fetchers:
    an unofficial endpoint going away must not take down the weekly report.
    The response is ~9 MB, so this is fetched once per run and shared.
    """
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
