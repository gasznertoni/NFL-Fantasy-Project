"""
Opponent-strength adjustment for projections.py -- a deliberately simple
alternative to the "defense vs. position, points-allowed" model that
docs/design/in-house-projection-model-spec.md section 3.4 deferred as
harder to build and unvalidated. This uses the opponent's overall
win-loss record as a proxy for how tough a matchup is, banded into a
multiplier, and plugs straight into project_player()'s existing
`opponent_multiplier` parameter -- no changes needed to projections.py
itself.

Known tradeoff, worth stating plainly: overall win-loss record conflates
offense and defense. A team with an elite offense and a mediocre defense
will read as a "tough" opponent here even though the defense specifically
might be easy to score against. This is simpler to build and reason about
than a position-specific defensive model, at the cost of that precision --
a legitimate v1 simplification, not an oversight.

Boundary convention (the four bands as given have an overlapping edge as
written -- "0.4-0.6" and "0.6-0.75" both include 0.6): each band's LOWER
bound is inclusive, upper bound is exclusive of the next band's lower
bound. So win_pct == 0.400 lands in the 1.00 band, == 0.600 lands in the
0.98 band, == 0.750 lands in the 0.95 band. One-line change in
DEFAULT_RECORD_MULTIPLIER_TIERS below if a different edge convention is
wanted.
"""

from __future__ import annotations

from typing import Any, Optional

# The exact tiers as specified: below .4 record -> boosted (easier
# matchup), above .75 -> suppressed (tougher matchup). Ascending by
# `below` (exclusive upper bound); the last entry has no `below` and acts
# as the catch-all top tier.
DEFAULT_RECORD_MULTIPLIER_TIERS = [
    {"below": 0.4, "multiplier": 1.08},
    {"below": 0.6, "multiplier": 1.00},
    {"below": 0.75, "multiplier": 0.98},
    {"below": None, "multiplier": 0.95},
]

MIN_GAMES_FOR_CURRENT_SEASON = 4  # per the brief: "after 4 games played"


def win_pct(wins: float, losses: float, ties: float = 0) -> float:
    """Standard win-percentage formula, ties worth half a win and half a
    loss. Returns 0.0 for a team with no games played yet rather than
    dividing by zero -- callers should check games_played separately if
    they need to distinguish "0.0 record" from "no data.\""""
    total = wins + losses + ties
    if total == 0:
        return 0.0
    return (wins + 0.5 * ties) / total


def team_record(
    schedule_games: list[dict[str, Any]],
    team: str,
    season: int,
    before_week: Optional[int] = None,
) -> dict[str, Any]:
    """Aggregate a team's win/loss/tie record from raw schedule rows.

    Args:
        schedule_games: rows shaped like nflreadpy's load_schedules() --
            {"season", "week", "home_team", "away_team", "home_score",
            "away_score"} (column names confirmed in
            docs/research/dst-scoring-fields.md). Games with a missing
            score (not yet played) are skipped automatically.
        team: team abbreviation to compute the record for.
        season: which season's games to aggregate.
        before_week: as-of filter, same discipline used everywhere else in
            this backend -- only games strictly before this week count.
            None means "the whole season" (safe to use for a fully
            completed prior season; do NOT pass None for the current,
            in-progress season, or future games would leak in).

    Returns:
        {"wins", "losses", "ties", "games_played", "win_pct"}.
    """
    wins = losses = ties = 0
    for g in schedule_games:
        if g.get("season") != season:
            continue
        if team not in (g.get("home_team"), g.get("away_team")):
            continue
        if before_week is not None and g.get("week", 0) >= before_week:
            continue
        home_score, away_score = g.get("home_score"), g.get("away_score")
        if home_score is None or away_score is None:
            continue  # not yet played

        team_score, opp_score = (
            (home_score, away_score) if g["home_team"] == team else (away_score, home_score)
        )
        if team_score > opp_score:
            wins += 1
        elif team_score < opp_score:
            losses += 1
        else:
            ties += 1

    games_played = wins + losses + ties
    return {
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "games_played": games_played,
        "win_pct": win_pct(wins, losses, ties),
    }


def multiplier_from_win_pct(pct: float, tiers: list[dict[str, Any]] = DEFAULT_RECORD_MULTIPLIER_TIERS) -> float:
    """Band lookup: first tier (in list order) whose `below` bound the
    win_pct is strictly under wins; a tier with `below: None` is the
    unbounded top tier and always matches if nothing earlier did."""
    for tier in tiers:
        if tier["below"] is None or pct < tier["below"]:
            return tier["multiplier"]
    # Unreachable if the tier list ends with a `below: None` catch-all as
    # DEFAULT_RECORD_MULTIPLIER_TIERS does -- guard anyway for a custom
    # tier list that omits one.
    return 1.0


def compute_opponent_multiplier(
    schedule_games: list[dict[str, Any]],
    opponent_team: str,
    target_season: int,
    target_week: int,
    tiers: list[dict[str, Any]] = DEFAULT_RECORD_MULTIPLIER_TIERS,
    min_games_for_current_season: int = MIN_GAMES_FOR_CURRENT_SEASON,
) -> dict[str, Any]:
    """The main entry point: figure out which record to use (this season's
    if the opponent has played enough games yet, else fall back to last
    season's final record) and convert it to a multiplier.

    This is a hard cutover at `min_games_for_current_season`, not a blend --
    matching what was asked for ("start with last season and move this as
    they perform each week after 4 games played"). Note this is a simpler
    rule than the design spec's original suggestion (section 3.4) to
    regress a thin sample toward a neutral value rather than switch outright
    -- a reasonable v1 simplification, flagged here in case the hard jump
    at exactly 4 games ever looks like a discontinuity worth smoothing.

    Returns:
        {"multiplier", "win_pct", "source", "games_played_this_season"} --
        source is "current_season", "prior_season", or "no_data" (neither
        season has any games for this team yet -- an edge case, e.g. a
        relocated/expansion team -- returns a neutral 1.0 rather than
        guessing).
    """
    current = team_record(schedule_games, opponent_team, target_season, before_week=target_week)

    if current["games_played"] >= min_games_for_current_season:
        pct, source = current["win_pct"], "current_season"
    else:
        prior = team_record(schedule_games, opponent_team, target_season - 1, before_week=None)
        if prior["games_played"] > 0:
            pct, source = prior["win_pct"], "prior_season"
        else:
            return {
                "multiplier": 1.0,
                "win_pct": None,
                "source": "no_data",
                "games_played_this_season": current["games_played"],
            }

    return {
        "multiplier": multiplier_from_win_pct(pct, tiers),
        "win_pct": pct,
        "source": source,
        "games_played_this_season": current["games_played"],
    }
