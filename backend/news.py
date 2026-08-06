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

DESIGNATIONS = ("Healthy", "Questionable", "Doubtful", "Out", "IR")
RISK_LEVELS = ("none", "low", "medium", "high")

DEFAULT_NEWS_FLAG = {"designation": "Healthy", "riskLevel": "none", "summary": None}


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
    last_name_lower = player_name.split()[-1].lower() if player_name.strip() else name_lower
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
            # No usable name to substring-match on (e.g. an id was given
            # but the name is blank) -- an id-only miss is just a miss.
            continue
        text = f"{article.get('headline', '')} {article.get('description', '')}".lower()
        if name_lower in text or last_name_lower in text:
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
        f'with {{"designation": "Healthy", "riskLevel": "none", "summary": null}}.'
    )


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
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text
        candidate = json.loads(raw_text)
    except Exception:
        # Covers API/network errors from the call itself (rate limit,
        # timeout, auth) as well as a malformed/empty response shape --
        # per this module's "never take down the report pipeline" contract,
        # any failure here degrades to "no flag shown" for this player.
        return dict(DEFAULT_NEWS_FLAG)

    return _validate_news_flag(candidate)


def build_anthropic_client():
    """Real client factory -- separate function so tests never need to hit
    this path. Requires ANTHROPIC_API_KEY in the environment and the
    `anthropic` package (add to backend/requirements.txt, run locally)."""
    import anthropic  # local import: optional dependency, only needed for real runs

    return anthropic.Anthropic()
