"""
News & injury layer (CLAUDE.md v1 scope item 2): pull player news/injury
text and use an LLM to summarize it and flag a risk level, producing the
exact `newsFlag` shape the frontend already expects (see
docs/specs/team-config-and-roster-status.md section 3.1) --
`{"designation": ..., "riskLevel": ..., "summary": ...}` -- so this can be
wired into the existing components with no frontend changes.

Source: ESPN's unofficial news endpoint
(`/apis/site/v2/sports/football/nfl/news`), per
docs/research/free-data-sources.md -- the only free source found with
narrative news text. Unofficial and unstable by nature (no ToS, no SLA),
so every network call here degrades gracefully to an empty result rather
than raising, per that doc's explicit recommendation.

Confirmed unreachable from the Cowork cloud sandbox during this build
(HTTP 000 / connection blocked -- same class of restriction the probe
script's header note already flagged for nflreadpy's data host). fetch_*
functions are therefore untested against the live endpoint here; run
scripts/ manually on your own machine to verify the endpoint shape still
matches ESPN_NEWS_URL's assumed response structure before relying on it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import requests

ESPN_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news"
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

DESIGNATIONS = ("Healthy", "Questionable", "Doubtful", "Out", "IR")
RISK_LEVELS = ("none", "low", "medium", "high")

DEFAULT_NEWS_FLAG = {"designation": "Healthy", "riskLevel": "none", "summary": None}

# Sleeper injury_status strings → our designation/riskLevel enums
_SLEEPER_DESIGNATION_MAP = {
    "Questionable": ("Questionable", "low"),
    "Doubtful": ("Doubtful", "medium"),
    "Out": ("Out", "high"),
    "IR": ("IR", "high"),
    "PUP": ("IR", "high"),
    "Sus": ("Out", "high"),
}


def fetch_sleeper_injury_status(timeout: int = 20) -> dict[str, dict[str, Any]]:
    """Fetch current injury designations from Sleeper (free, no auth).

    Returns two lookup dicts merged into one result keyed by ESPN id (str)
    and by lowercase full name — callers try ESPN id first, then name.
    Value shape: {"designation": ..., "riskLevel": ..., "body_part": str|None}.
    Only players with a non-None injury_status are included; healthy players
    are absent so callers can fall back to DEFAULT_NEWS_FLAG cleanly.
    Degrades to {} on any network/parse failure.
    """
    try:
        resp = requests.get(SLEEPER_PLAYERS_URL, timeout=timeout)
        resp.raise_for_status()
        players = resp.json()
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return {}

    if not isinstance(players, dict):
        return {}

    by_espn_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}

    for player in players.values():
        if not isinstance(player, dict):
            continue
        injury_status = player.get("injury_status")
        if not injury_status:
            continue
        designation, risk_level = _SLEEPER_DESIGNATION_MAP.get(injury_status, ("Out", "high"))
        body_part = player.get("injury_body_part") or None
        entry = {"designation": designation, "riskLevel": risk_level, "body_part": body_part}

        espn_id = player.get("espn_id")
        if espn_id:
            by_espn_id[str(espn_id)] = entry
        full_name = (player.get("full_name") or "").lower().strip()
        if full_name:
            by_name[full_name] = entry

    return {**by_name, **by_espn_id}  # espn_id keys win on collision


def apply_sleeper_designation(
    news_flag: dict[str, Any],
    player_name: str,
    espn_id: Optional[str],
    sleeper_data: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Override the designation/riskLevel in news_flag with Sleeper's value,
    keeping the existing summary text. If Sleeper has no entry for this player
    the flag is returned unchanged. If Sleeper has a body_part and the flag
    has no summary, uses body_part as a minimal summary."""
    entry = sleeper_data.get(str(espn_id) if espn_id else "") or sleeper_data.get(
        (player_name or "").lower().strip()
    )
    if not entry:
        return news_flag

    summary = news_flag.get("summary") or (
        entry["body_part"] if entry.get("body_part") else None
    )
    return {"designation": entry["designation"], "riskLevel": entry["riskLevel"], "summary": summary}


def fetch_espn_news(limit: int = 50, timeout: int = 15) -> list[dict[str, Any]]:
    """Fetch raw articles from ESPN's unofficial news endpoint. Returns []
    on any failure (network error, non-200, non-JSON, unexpected shape) --
    per the source doc's "degrade gracefully" instruction, a broken/changed
    endpoint should never take down the report pipeline; it should just
    mean no news layer for this run.
    """
    try:
        resp = requests.get(ESPN_NEWS_URL, params={"limit": limit}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return []

    articles = data.get("articles") if isinstance(data, dict) else None
    return articles if isinstance(articles, list) else []


def articles_for_player(articles: list[dict[str, Any]], player_name: str, player_espn_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Filter raw articles down to ones relevant to a given player.

    Primary match: ESPN's response tags each article with a `categories`
    list, and entries of type "athlete" carry that athlete's ESPN id
    directly -- an exact match, no NLP needed, when player_espn_id is
    available and present in the response.

    Fallback: plain case-insensitive substring match on the player's name
    against the headline/description, for when the id isn't available (not
    every player has a resolved ESPN id in the crosswalk yet -- see
    CLAUDE.md's player-ID crosswalk gap for rookies) or the categories
    field is absent/differently shaped than expected. Substring matching on
    a name is intentionally crude and will over-match common surnames --
    acceptable here because the output still goes through per-player LLM
    summarization, which will correctly report "no relevant news" for a
    false-positive match rather than fabricate injury content.
    """
    if not player_name.strip() and player_espn_id is None:
        # An empty/blank name with no id to fall back on would make every
        # article match (empty string is a substring of everything) --
        # treat this as "can't identify the player" rather than "matches
        # all news."
        return []

    matched = []
    name_lower = player_name.lower()
    # News text commonly refers to a player by last name alone after a
    # first mention (e.g. "McCaffrey limited in practice") -- matching
    # only the full name would miss most real headlines. Last-name-only
    # matching is deliberately crude (over-matches common surnames); see
    # the docstring above for why that's an acceptable tradeoff here.
    for article in articles:
        categories = article.get("categories") or []
        id_hit = player_espn_id is not None and any(
            str(c.get("athleteId") or c.get("id")) == str(player_espn_id)
            for c in categories
            if isinstance(c, dict) and c.get("type") == "athlete"
        )
        if id_hit:
            matched.append(article)
            continue
        if not player_name.strip():
            continue
        text = f"{article.get('headline', '')} {article.get('description', '')}".lower()
        # Full-name match only (not last-name-alone): last-name-only matching
        # triggered LLM calls for every player sharing a common surname (Brown,
        # Williams, Hill, etc.) against unrelated articles -- expensive with no
        # quality benefit since the ESPN-ID primary match already covers exact
        # hits. Full-name match misses "McCaffrey runs for 80" headlines but
        # those are not injury/availability news we need to summarize anyway.
        if name_lower in text:
            matched.append(article)
    return matched


def _validate_news_flag(candidate: dict[str, Any]) -> dict[str, Any]:
    """Defensively coerce an LLM's JSON reply into a valid newsFlag, per the
    closed enums in docs/specs/team-config-and-roster-status.md section
    3.1. An LLM occasionally drifts from a requested enum (e.g. returns
    "healthy" lowercase, or an explanatory sentence instead of one of the 5
    values) -- falling back to the safe default here means a malformed
    reply degrades to "no flag shown," never a broken/mismatched UI state
    or a silently-wrong medical-sounding claim.
    """
    designation = candidate.get("designation")
    risk_level = candidate.get("riskLevel")
    summary = candidate.get("summary")

    if designation not in DESIGNATIONS or risk_level not in RISK_LEVELS:
        return dict(DEFAULT_NEWS_FLAG)

    if summary is not None and not isinstance(summary, str):
        summary = None

    return {"designation": designation, "riskLevel": risk_level, "summary": summary}


def build_summary_prompt(player_name: str, articles: list[dict[str, Any]]) -> str:
    """Pure prompt-building function, separated from the network/LLM call
    so it's directly unit-testable without a live API key."""
    snippets = "\n\n".join(
        f"Headline: {a.get('headline', '')}\nText: {a.get('description', '')}"
        for a in articles
    )
    return (
        f"You are a fantasy football injury/news analyst. Below are recent news "
        f"snippets that may mention the player {player_name}. Some snippets may "
        f"be irrelevant or about a different context -- ignore anything not "
        f"actually about this player's health/availability status.\n\n"
        f"{snippets}\n\n"
        f"Respond with ONLY a JSON object, no other text, in exactly this shape:\n"
        f'{{"designation": one of {list(DESIGNATIONS)}, '
        f'"riskLevel": one of {list(RISK_LEVELS)}, '
        f'"summary": a one-sentence plain-English summary of the relevant news, '
        f'or null if nothing relevant/no elevated risk was found}}\n\n'
        f'If nothing here indicates an elevated injury/availability risk, respond '
        f'with {{"designation": "Healthy", "riskLevel": "none", "summary": null}}.\n\n'
        f'Output the raw JSON object only. Do not wrap it in a markdown code '
        f'fence and do not add any commentary before or after it.'
    )


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    """Parse the model's reply into a dict, tolerating a markdown fence.

    Models routinely wrap JSON in ```json ... ``` even when told not to, and
    a bare json.loads() on that raises. That failure used to be swallowed by
    summarize_player_news' except-block and degrade to DEFAULT_NEWS_FLAG, so a
    100% failure rate looked exactly like "no newsworthy players" -- every LLM
    summary in every report was silently empty until 2026-09-01.

    Strips an optional fence, then falls back to the outermost {...} span, so a
    stray sentence around the object does not lose the whole response.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        # ```json\n{...}\n```  ->  {...}
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


# Reasons summarization degraded, appended to as it happens. A caller that
# runs a whole pool should report len(SUMMARY_FAILURES) -- see the note in
# summarize_player_news about why this exists.
SUMMARY_FAILURES: list[str] = []


def summarize_player_news(player_name: str, articles: list[dict[str, Any]], client: Any) -> dict[str, Any]:
    """Summarize a player's relevant articles into a validated newsFlag.

    Args:
        client: any object exposing `.messages.create(...)` matching the
            Anthropic SDK's shape (duck-typed deliberately, so tests can
            pass a fake without the `anthropic` package installed).
    Returns:
        DEFAULT_NEWS_FLAG immediately if there are no relevant articles at
        all -- an explicit "no news" case, not something to spend an LLM
        call determining.
    """
    if not articles:
        return dict(DEFAULT_NEWS_FLAG)

    prompt = build_summary_prompt(player_name, articles)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text
        candidate = _parse_json_object(raw_text)
    except Exception as exc:  # noqa: BLE001
        # Covers API/network errors from the call itself (rate limit,
        # timeout, auth) as well as a malformed/empty response shape --
        # per this module's "never take down the report pipeline" contract,
        # any failure here degrades to "no flag shown" for this player.
        #
        # But it is NOT silent any more. Swallowing this without a trace is
        # what let every summarization fail for months while the pipeline
        # reported success: a per-player degrade is only safe if someone can
        # see how often it fires.
        SUMMARY_FAILURES.append(f"{player_name}: {type(exc).__name__}: {exc}")
        return dict(DEFAULT_NEWS_FLAG)

    return _validate_news_flag(candidate)


def build_anthropic_client():
    """Real client factory -- separate function so tests never need to hit
    this path. Requires ANTHROPIC_API_KEY in the environment and the
    `anthropic` package (add to backend/requirements.txt, run locally)."""
    import anthropic  # local import: optional dependency, only needed for real runs

    return anthropic.Anthropic()
