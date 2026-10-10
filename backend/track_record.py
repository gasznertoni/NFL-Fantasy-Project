"""Live-season track record built from the weekly-report fixtures. See ARCHITECTURE.md §11."""

from __future__ import annotations

from typing import Any, Optional

HIT_RATE_THRESHOLD = 0.75

TIER_LABELS = {"consensus": "Consensus projection", "in_house_estimate": "Our estimate"}


def _outcome_correct(predicted_points: float, actual_points: Optional[float]) -> Optional[bool]:
    if actual_points is None:
        return None
    if predicted_points <= 0:
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
    """One TrackRecord `history[]` row."""
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
    """History rows from one report: projections are starts, waiver targets adds."""
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
    """One summary[tier] entry over graded rows only; zeros, never null."""
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
    """Assemble the full TrackRecord object across weeks 1..as_of_week."""
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
