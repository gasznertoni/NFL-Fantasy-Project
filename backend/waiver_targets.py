"""Waiver-target selection and templated rationale. See ARCHITECTURE.md §10."""

from __future__ import annotations

from typing import Any, Optional

DEFAULT_ROSTERED_RANK_CUTOFF: dict[str, int] = {"QB": 14, "RB": 30, "WR": 30, "TE": 14, "DST": 14, "K": 14}

DEFAULT_TOP_N = 3

EXCLUDED_DESIGNATIONS = {"Out", "IR", "Doubtful"}


def waiver_eligible_candidates(
    candidates: list[dict[str, Any]],
    rostered_rank_cutoff: dict[str, int] = DEFAULT_ROSTERED_RANK_CUTOFF,
) -> list[dict[str, Any]]:
    """Drop unavailable and assumed-rostered players; add replacement_surplus."""
    available = [
        c
        for c in candidates
        if (c.get("news_flag") or {}).get("designation") not in EXCLUDED_DESIGNATIONS
    ]

    eligible: list[dict[str, Any]] = []
    for position in {c["position"] for c in available}:
        cutoff = rostered_rank_cutoff.get(position, 0)
        at_position = sorted(
            (c for c in available if c["position"] == position),
            key=lambda c: c["points"],
            reverse=True,
        )
        replacement_points = at_position[cutoff - 1]["points"] if 0 < cutoff <= len(at_position) else 0.0
        for c in at_position[cutoff:]:
            enriched = dict(c)
            enriched["replacement_surplus"] = c["points"] - replacement_points
            eligible.append(enriched)
    return eligible


def describe_trend(per_game_points: list[float]) -> Optional[str]:
    """Last game vs the average before it; None with fewer than two games."""
    if len(per_game_points) < 2:
        return None
    latest = per_game_points[-1]
    prior_avg = sum(per_game_points[:-1]) / len(per_game_points[:-1])
    if prior_avg == 0:
        return "up" if latest > 0 else None
    if latest > prior_avg * 1.15:
        return "up"
    if latest < prior_avg * 0.85:
        return "down"
    return "flat"


def select_waiver_targets(
    candidates: list[dict[str, Any]],
    top_n: int = DEFAULT_TOP_N,
    rostered_rank_cutoff: dict[str, int] = DEFAULT_ROSTERED_RANK_CUTOFF,
) -> list[dict[str, Any]]:
    """Top `top_n` eligible candidates by surplus over replacement, all positions."""
    eligible = waiver_eligible_candidates(candidates, rostered_rank_cutoff)
    ranked = sorted(eligible, key=lambda c: c["replacement_surplus"], reverse=True)
    return [{k: v for k, v in c.items() if k != "replacement_surplus"} for c in ranked[:top_n]]


def generate_rationale(
    candidate: dict[str, Any],
    rz_stats: Optional[dict[str, Any]] = None,
) -> str:
    """Deterministic one-sentence rationale, with an optional Rotowire note."""
    from rotowire import MIN_RZ_TOUCHES_FOR_NOTE, MIN_TPRR_FOR_NOTE

    name = candidate["name"]
    points = candidate.get("points", 0.0)
    games_used = candidate.get("games_used", 0)
    confidence = candidate.get("confidence", "no_data")
    per_game_points = candidate.get("per_game_points") or []
    trend = describe_trend(per_game_points)

    if confidence == "no_data":
        base = f"No games played yet this season for {name} -- a speculative add based on role/opportunity alone."
    elif confidence == "low":
        base = (
            f"Only {games_used} game(s) of data so far, averaging {points:.1f} pts -- "
            f"an early sample worth monitoring rather than a proven trend."
        )
    elif trend == "up":
        base = (
            f"Trending up over the last {games_used} games, averaging {points:.1f} pts "
            f"with the most recent game the best of the stretch."
        )
    elif trend == "down":
        base = (
            f"Averaging {points:.1f} pts over the last {games_used} games, "
            f"though production cooled the last time out."
        )
    else:
        base = (
            f"Steady {points:.1f}-point average over the last {games_used} games -- "
            f"a solid depth add off the wire."
        )

    if rz_stats:
        rz = rz_stats.get("rz_touches_per_game")
        tprr = rz_stats.get("tprr_recent")
        if rz is not None and rz >= MIN_RZ_TOUCHES_FOR_NOTE:
            base += f" Averaging {rz:.1f} goal-line touch(es) per game recently -- elevated TD upside."
        elif tprr is not None and tprr >= MIN_TPRR_FOR_NOTE:
            base += f" Running a {tprr:.0%} target rate on routes (tprr) recently -- strong target share."

    news_flag = candidate.get("news_flag") or {}
    if news_flag.get("riskLevel") not in (None, "none") and news_flag.get("summary"):
        base += f" Note: {news_flag['summary']}"
    return base
