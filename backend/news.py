"""News and injury flags summarised by an LLM. See ARCHITECTURE.md §9."""

from __future__ import annotations

import json
from typing import Any, Optional

import requests

ESPN_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news"
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

DESIGNATIONS = ("Healthy", "Questionable", "Doubtful", "Out", "IR")
RISK_LEVELS = ("none", "low", "medium", "high")

DEFAULT_NEWS_FLAG = {"designation": "Healthy", "riskLevel": "none", "summary": None}

SUMMARY_MODEL = "claude-haiku-4-5-20251001"

_SLEEPER_DESIGNATION_MAP = {
    "Questionable": ("Questionable", "low"),
    "Doubtful": ("Doubtful", "medium"),
    "Out": ("Out", "high"),
    "IR": ("IR", "high"),
    "PUP": ("IR", "high"),
    "Sus": ("Out", "high"),
}


def fetch_sleeper_injury_status(timeout: int = 20) -> dict[str, dict[str, Any]]:
    """Current Sleeper designations keyed by ESPN id and lowercase name; {} on failure."""
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

    return {**by_name, **by_espn_id}


def apply_sleeper_designation(
    news_flag: dict[str, Any],
    player_name: str,
    espn_id: Optional[str],
    sleeper_data: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Override a flag's designation/riskLevel with Sleeper's, keeping the summary."""
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
    """Raw articles from ESPN's unofficial news endpoint; [] on any failure."""
    try:
        resp = requests.get(ESPN_NEWS_URL, params={"limit": limit}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return []

    articles = data.get("articles") if isinstance(data, dict) else None
    return articles if isinstance(articles, list) else []


def articles_for_player(articles: list[dict[str, Any]], player_name: str, player_espn_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Articles relevant to a player: ESPN athlete id first, then full name."""
    if not player_name.strip() and player_espn_id is None:
        # An empty name would match every article.
        return []

    matched = []
    name_lower = player_name.lower()
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
        if name_lower in text:
            matched.append(article)
    return matched


def _validate_news_flag(candidate: dict[str, Any]) -> dict[str, Any]:
    """Coerce an LLM reply into a valid newsFlag, defaulting on anything malformed."""
    designation = candidate.get("designation")
    risk_level = candidate.get("riskLevel")
    summary = candidate.get("summary")

    if designation not in DESIGNATIONS or risk_level not in RISK_LEVELS:
        return dict(DEFAULT_NEWS_FLAG)

    if summary is not None and not isinstance(summary, str):
        summary = None

    return {"designation": designation, "riskLevel": risk_level, "summary": summary}


def build_summary_prompt(player_name: str, articles: list[dict[str, Any]]) -> str:
    """Build the summarization prompt (pure, so it is testable without a key)."""
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
    """Parse the model's reply into a dict, tolerating a markdown fence."""
    text = raw_text.strip()
    if text.startswith("```"):
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


SUMMARY_FAILURES: list[str] = []


def summarize_player_news(
    player_name: str,
    articles: list[dict[str, Any]],
    client: Any,
    store: Any = None,
    player_id: Optional[str] = None,
) -> dict[str, Any]:
    """Summarize a player's relevant articles into a validated newsFlag."""
    if not articles:
        return dict(DEFAULT_NEWS_FLAG)

    prompt = build_summary_prompt(player_name, articles)

    cache_key = None
    if store is not None and player_id:
        try:
            from store import content_hash

            cache_key = content_hash(prompt, SUMMARY_MODEL)
            hit = store.get_news_summary(player_id, cache_key)
            if hit and hit.get("model") == SUMMARY_MODEL:
                return _validate_news_flag(
                    {
                        "designation": hit.get("designation"),
                        "riskLevel": hit.get("risk_level"),
                        "summary": hit.get("summary"),
                    }
                )
        except Exception:  # noqa: BLE001
            cache_key = None

    try:
        response = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text
        candidate = _parse_json_object(raw_text)
    except Exception as exc:  # noqa: BLE001
        SUMMARY_FAILURES.append(f"{player_name}: {type(exc).__name__}: {exc}")
        return dict(DEFAULT_NEWS_FLAG)

    flag = _validate_news_flag(candidate)

    # Only cache a validated summary; a cached failure would be permanent.
    if cache_key and flag.get("summary"):
        try:
            store.put_news_summary(
                player_id, cache_key, flag["summary"],
                flag.get("designation"), flag.get("riskLevel"), SUMMARY_MODEL,
            )
        except Exception:  # noqa: BLE001
            pass

    return flag


def build_anthropic_client():
    """Real Anthropic client factory; needs ANTHROPIC_API_KEY."""
    import anthropic

    return anthropic.Anthropic()
