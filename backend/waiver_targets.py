"""
Waiver-target selection + rationale generation.

Closes gap #3 in docs/design/backend-frontend-integration-plan.md's data
contract mapping: `getWeeklyReport()`'s `waiverTargets[]` needs both a
selection rule (which players get surfaced) and a free-text `rationale`
per player. The plan's suggested build order explicitly allows shipping a
"simple templated rationale" ahead of any LLM-generated version -- that's
what this module does: fully deterministic, no LLM call, no network.

No real roster-ownership-% data source exists anywhere in this project
(see CLAUDE.md's Data Sources table -- nothing there covers who-owns-whom
in the builder's actual league), so "waiver-eligible" here is a simple
in-house proxy, not a real ownership check: assume the top
DEFAULT_ROSTERED_RANK_CUTOFF[position] players at each position (by this
week's projected points) are already rostered somewhere across a 14-team
league, and only surface players ranked below that cutoff. This is a
stated v1 simplification, not a claim of real ownership data.

Operates on plain "candidate" dicts (playerId/name/position/team/opponent/
points/games_used/confidence/per_game_points/news_flag) -- the internal
per-player projection shape, not the frontend's nested `projection`
object. generate_report.py assembles the final WeeklyReport waiverTargets[]
entries (nesting `points` etc. under `projection`, per api.js's contract)
from what this module selects and writes as `rationale`.
"""

from __future__ import annotations

from typing import Any, Optional

# A 14-team league (CLAUDE.md's league size) with standard-ish depth: assume
# roughly this many players per position are rostered somewhere already.
# Deliberately generous (skips more than a literal 14-team QB1 count would)
# since flex/bench/handcuff rostering pushes real depth further down the
# position list than starters alone would suggest.
#
# DST/K added 2026-08-12 when wiring those two positions into the pool for
# the first time (CLAUDE.md Next Steps item 3) surfaced the same failure
# mode raw-points ranking already caused for QB (module docstring above):
# with no cutoff entry, every one of the league's 32 DSTs (or ~30 rostered
# kickers) is "waiver eligible," and DST/K's placeholder-config point
# totals are competitive enough with thin skill-position totals that a
# real 2025-week-10 run produced a waiver list of DST/K/DST -- not a
# useful "waiver wire" recommendation for two single-start positions.
# 14 mirrors QB/TE's cutoff (this league also starts exactly one DST and
# one K per roster, same as QB/TE -- see frontend/public/mock/roster-slots.json).
DEFAULT_ROSTERED_RANK_CUTOFF: dict[str, int] = {"QB": 14, "RB": 30, "WR": 30, "TE": 14, "DST": 14, "K": 14}

DEFAULT_TOP_N = 3

# Designations a waiver suggestion shouldn't surface -- adding a player who
# is confirmed not playing this week isn't an actionable recommendation.
EXCLUDED_DESIGNATIONS = {"Out", "IR", "Doubtful"}


def waiver_eligible_candidates(
    candidates: list[dict[str, Any]],
    rostered_rank_cutoff: dict[str, int] = DEFAULT_ROSTERED_RANK_CUTOFF,
) -> list[dict[str, Any]]:
    """Filter a full candidate pool down to plausibly-available players:
    drop anyone ruled out/doubtful/on IR this week, then drop the top
    `rostered_rank_cutoff[position]` remaining players at each position by
    projected points (assumed already rostered). Positions with no cutoff
    entry use every candidate (nothing dropped for "assumed rostered").

    Each surviving candidate gets an added `replacement_surplus` key:
    its points minus the last *rostered* (cutoff-th ranked) player's
    points at the same position. This league's 6-point passing TDs put
    QB's raw point scale well above every other position -- ranking
    waiver targets on raw points alone surfaced nothing but backup QBs in
    testing against real 2025 data, which isn't a useful "waiver wire"
    recommendation. Surplus-over-replacement is comparable across
    positions the way raw points aren't. `select_waiver_targets` ranks on
    this field, not `points`.
    """
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
    """Compare the most recent game to the average of the games before it.
    Needs at least 2 games to say anything -- with 0 or 1 games played
    there's no "before" to compare against, so trend is unknown (None),
    not "flat" (flat is a real signal: production held steady)."""
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
    """The main entry point: eligible candidates, ranked by
    points-above-replacement across all positions (see
    waiver_eligible_candidates' docstring for why raw points isn't
    cross-position comparable), top `top_n` overall -- not per-position,
    since a report only has room to surface a handful of waiver ideas and
    the strongest overall plays are more useful there than a forced
    one-per-position spread."""
    eligible = waiver_eligible_candidates(candidates, rostered_rank_cutoff)
    ranked = sorted(eligible, key=lambda c: c["replacement_surplus"], reverse=True)
    # Strip the internal ranking key before returning -- callers get back
    # candidates in the same shape they were passed in.
    return [{k: v for k, v in c.items() if k != "replacement_surplus"} for c in ranked[:top_n]]


def generate_rationale(candidate: dict[str, Any]) -> str:
    """Deterministic, templated one-sentence rationale -- no LLM call (see
    module docstring). Branches on confidence/trend rather than being a
    single fill-in-the-blank template, so the handful of surfaced targets
    don't all read identically."""
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

    news_flag = candidate.get("news_flag") or {}
    if news_flag.get("riskLevel") not in (None, "none") and news_flag.get("summary"):
        base += f" Note: {news_flag['summary']}"
    return base
