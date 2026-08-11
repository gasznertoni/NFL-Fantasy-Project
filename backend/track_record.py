"""
Live-season track-record aggregation.

Closes gap #4 in docs/design/backend-frontend-integration-plan.md: this
builds the *live*, in-season "how has the tool done so far this year" view
(`getTrackRecord()`'s TrackRecord shape) -- distinct from backtest.py's
historical train/holdout harness, which grades model *mechanics* against a
past season, not this tool's own running track record.

The prediction log CLAUDE.md's eval-layer section asks for ("recommendations
should be logged in a way that makes later accuracy scoring straightforward")
already exists: every weekly-report-week-N.json generate_report.py writes
IS that log entry for week N. This module reads those back in rather than
maintaining a separate prediction database.

The "outcomeCorrect" hit-rate rule (HIT_RATE_THRESHOLD below) and the
per-tier summary math were confirmed to exactly reproduce every value in
the existing frontend/public/mock/track-record.json mock fixture (11
scored predictions across 2 tiers -- both individual outcomeCorrect flags
and the aggregated predictionsScored/startSitHitRate/meanAbsoluteError)
before being written here, not chosen by feel.
"""

from __future__ import annotations

from typing import Any, Optional

# A "start" or "waiver_add" recommendation counts as correct if the actual
# outcome met at least this fraction of the projected points -- i.e. the
# player didn't badly bust relative to expectation. Overperforming is
# always correct; only falling short by more than (1 - threshold) counts
# as a miss.
HIT_RATE_THRESHOLD = 0.75

TIER_LABELS = {"consensus": "Consensus projection", "in_house_estimate": "Our estimate"}


def _outcome_correct(predicted_points: float, actual_points: Optional[float]) -> Optional[bool]:
    if actual_points is None:
        return None
    if predicted_points <= 0:
        # No meaningful ratio against a zero/negative projection (e.g. a
        # cold-start no_data projection) -- any real production counts as
        # correct, since there was no positive expectation to fall short of.
        return actual_points >= 0
    return actual_points >= HIT_RATE_THRESHOLD * predicted_points


def build_history_entry(
    season: int,
    week: int,
    player: dict[str, Any],
    tier: str,
    recommendation_type: str,
    predicted_points: float,
    actual_points: Optional[float],
) -> dict[str, Any]:
    """One TrackRecord `history[]` row. `player` must have playerId/name/position."""
    prediction_id = f"pred_{season}_{week:02d}_{player['playerId']}_{recommendation_type}"
    return {
        "predictionId": prediction_id,
        "week": week,
        "player": {
            "playerId": player["playerId"],
            "name": player["name"],
            "position": player["position"],
        },
        "tier": tier,
        "recommendationType": recommendation_type,
        "predictedPoints": predicted_points,
        "actualPoints": actual_points,
        "outcomeCorrect": _outcome_correct(predicted_points, actual_points),
    }


def history_from_weekly_report(
    season: int,
    week: int,
    weekly_report: dict[str, Any],
    actual_points_by_player: dict[str, Optional[float]],
) -> list[dict[str, Any]]:
    """Every `projections[]` entry becomes a "start" prediction row, every
    `waiverTargets[]` entry becomes a "waiver_add" row -- a generated
    report only ever lists players the tool actively projected/surfaced
    that week, so no further filtering is needed to call these
    "recommendations." A player with no entry at all in
    `actual_points_by_player` (as opposed to an explicit None) is treated
    identically -- both mean "not known yet," via `.get()`."""
    rows = []
    for entry in weekly_report.get("projections", []):
        rows.append(
            build_history_entry(
                season,
                week,
                entry,
                entry["projection"]["tier"],
                "start",
                entry["projection"]["points"],
                actual_points_by_player.get(entry["playerId"]),
            )
        )
    for entry in weekly_report.get("waiverTargets", []):
        rows.append(
            build_history_entry(
                season,
                week,
                entry,
                entry["projection"]["tier"],
                "waiver_add",
                entry["projection"]["points"],
                actual_points_by_player.get(entry["playerId"]),
            )
        )
    return rows


def summarize_tier(history: list[dict[str, Any]], tier: str, tier_label: str) -> dict[str, Any]:
    """One `summary[tier]` entry: only rows with a known actual result
    count toward predictionsScored/startSitHitRate/meanAbsoluteError -- the
    current (not-yet-played) week's logged predictions correctly stay out
    of these numbers until graded. Defaults to 0/0.0 (never null) when
    nothing is scored yet, per SummaryCard.jsx calling
    `.toFixed(1)`/`* 100` on these fields unconditionally -- a null would
    crash the view rather than just showing zeroes."""
    scored = [h for h in history if h["tier"] == tier and h["actualPoints"] is not None]
    if not scored:
        return {"tierLabel": tier_label, "predictionsScored": 0, "startSitHitRate": 0.0, "meanAbsoluteError": 0.0}

    hits = sum(1 for h in scored if h["outcomeCorrect"])
    abs_errors = [abs(h["actualPoints"] - h["predictedPoints"]) for h in scored]
    return {
        "tierLabel": tier_label,
        "predictionsScored": len(scored),
        "startSitHitRate": round(hits / len(scored), 2),
        "meanAbsoluteError": round(sum(abs_errors) / len(abs_errors), 1),
    }


def build_track_record(
    season: int,
    as_of_week: int,
    weekly_reports_by_week: dict[int, dict[str, Any]],
    actual_points_by_player_week: dict[int, dict[str, Optional[float]]],
    tier_labels: dict[str, str] = TIER_LABELS,
) -> dict[str, Any]:
    """Assemble the full TrackRecord object across weeks 1..as_of_week.

    Args:
        weekly_reports_by_week: {week: WeeklyReport dict} for every week
            1..as_of_week that has a generated report. A missing week
            simply contributes nothing (e.g. the tool wasn't run that
            week) rather than raising.
        actual_points_by_player_week: {week: {playerId: points_or_None}}.
            Omitting a week entirely, or a player within a week, means
            "not known yet" -- equivalent to mapping it to None.
    """
    history: list[dict[str, Any]] = []
    for week in range(1, as_of_week + 1):
        report = weekly_reports_by_week.get(week)
        if report is None:
            continue
        actuals = actual_points_by_player_week.get(week, {})
        history.extend(history_from_weekly_report(season, week, report, actuals))

    tiers = sorted({h["tier"] for h in history}) or sorted(tier_labels)
    summary = {tier: summarize_tier(history, tier, tier_labels.get(tier, tier)) for tier in tiers}

    return {
        "season": season,
        "asOfWeek": as_of_week,
        "summary": summary,
        "history": history,
    }
