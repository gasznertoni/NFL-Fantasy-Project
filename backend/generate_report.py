"""
Orchestration script: wires scoring.py + projections.py + fantasypros.py +
news.py + waiver_targets.py together and writes weekly-report-week-N.json
and player-pool.json in the exact shape frontend/src/lib/api.js already
expects -- the "generation script, not a live API" architecture from
docs/design/backend-frontend-integration-plan.md.

Follows that plan's v0 build order, with one update since the plan was
written: real player IDs (nflreadpy's gsis_id) as `playerId` directly, no
invented p_00123-style scheme and no permanent crosswalk table (the plan's
recommended resolution to its own "player-ID gap" open question -- this
project has no real users with saved localStorage data yet to migrate).
Gap #2 -- an actual FantasyPros pull and top-10-per-position tier
selection, originally deferred per the plan's v0 scoping ("ship 100%
in-house first") -- is now wired in via fantasypros.py (CLAUDE.md v8 Next
Steps item 3): a player who resolves to FantasyPros' top-10-for-position
consensus tier gets that projection; everyone else still gets the
in-house estimate. Waiver targets still use waiver_targets.py's templated
(non-LLM) rationale, per the plan's suggested v0 shortcut.

DST and K are now covered too (2026-08-12, closing CLAUDE.md Next Steps
item 3 -- see dst.py and kicker.py), both in the in_house_estimate tier
only: FantasyPros' free-tier consensus pull (fantasypros.py) stays scoped
to its confirmed QB/RB/WR/TE top-10-per-position coverage, not extended to
DST/K here (a separate, unscoped decision -- see backend/README.md).
projections.py's rolling-average/shrinkage model is wired through
unmodified for DST/K (no new modeling logic), but it was only backtested
against QB/RB/WR/TE (docs/research/projection-model-backtest-findings.md)
-- DST/K projections haven't been through that same rigor, an honest scope
note, not a blocker. A DST's `playerId` is its team abbreviation (e.g.
"BUF"), not a gsis_id -- a DST isn't a per-player entity, and a real,
stable ID already exists for it (see load_dst_pool_nflreadpy below and
dst.py's own docstring). A K's `playerId` is a real gsis_id, same
mechanism QB/RB/WR/TE already use.

Split, like every other module in this backend, into pure/testable
assembly functions (this module's top half) and network adapter functions
(bottom half, prefixed `load_`) that are NOT exercised by the test suite --
see tests/test_generate_report.py, which only exercises the pure half with
synthetic data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import entity_prior
from news import DEFAULT_NEWS_FLAG
from blend import VOLUME_COLUMNS
from blend import build_feature_row as blend_build_feature_row
from blend import rolling_volume
from calibration import apply_affine
from projections import (
    DEFAULT_DECAY,
    DEFAULT_WINDOW,
    POSITION_CALIBRATION_SCALE,
    project_player,
)
from waiver_targets import generate_rationale, select_waiver_targets

# nflreadpy (2025 season, confirmed hands-on) uses "LA" for the Rams; the
# frontend's mock fixtures and the rest of this project's docs use "LAR".
# One-entry map so this doesn't need to grow into a full alias table for a
# single known mismatch.
TEAM_ABBR_DISPLAY_MAP = {"LA": "LAR"}

# The positions projections.py's model was actually backtested against
# (docs/research/projection-model-backtest-findings.md) and the scope of
# fantasypros.py's consensus tier (fantasypros.POSITIONS mirrors this).
# Kept separate from ROSTER_POSITIONS below since K resolves to a real
# player via load_rosters() the same way these four do, but is excluded
# from the backtest/FantasyPros scope both still describe.
POOL_POSITIONS = ("QB", "RB", "WR", "TE")

KICKER_POSITION = "K"

# Positions resolvable via nflreadpy's load_rosters() with a real gsis_id
# -- K joins QB/RB/WR/TE here since it's a per-player entity too, just not
# part of POOL_POSITIONS' backtest/FantasyPros scope. DST is NOT in this
# list -- it isn't a per-player roster entity at all, see
# load_dst_pool_nflreadpy below.
ROSTER_POSITIONS = POOL_POSITIONS + (KICKER_POSITION,)

TIER_METADATA = {
    "consensus": {"tierLabel": "Consensus projection", "source": "FantasyPros + Rotowire"},
    "in_house_estimate": {"tierLabel": "Our estimate", "source": "In-house model"},
    # Week 1 only. Labelled distinctly from the in-house rolling average
    # because it is a different kind of number built from different inputs
    # (last season plus draft capital and the opening Vegas line, with zero
    # current-season games behind it) -- same "keep the tiers clearly
    # labelled" rule that separates the consensus tier from ours.
    "week1_model": {"tierLabel": "Week 1 estimate", "source": "In-house model (pre-season)"},
}

# Updated 2026-08-16 once real league values landed: reception=1 in
# leagues/league-1/scoring-config.json (was the half_ppr placeholder's 0.5) --
# this is a full-PPR league, confirmed hands-on from the real ESPN
# settings, not a guess. "ppr" is already a recognized key in the
# frontend's WeeklyReportView.jsx FORMAT_LABEL map (renders as "PPR"),
# so this is the only code change needed -- no frontend edit required.
# Kept as a module-level constant for backward compatibility; in the
# multi-league path (--leagues-config), each league's leagueFormat from
# leagues.json is passed explicitly and this constant is not used.
LEAGUE_FORMAT_ASSUMPTION = "ppr"

# How many past seasons of labelled week-1 rows the cold-start model trains on
# (backend/week1.py). Four gives ~1 350 rows across the four positions -- enough
# for the ~56-column per-position ridge without reaching back to a materially
# different league (scoring environment and pass rate have both drifted).
# Each training season needs the two seasons before it for its own features,
# so the real data pull spans WEEK1_TRAIN_SEASONS + 2.
WEEK1_TRAIN_SEASONS = 4

# Completed seasons pulled for the week-1 K / D/ST per-entity baselines
# (backend/week1_kdst.py). Six back, minus 2020, gives ~110 kicker and ~128
# defence (prior -> next week 1) pairs to fit the shrinkage constant on --
# comfortably above that module's MIN_FIT_OBSERVATIONS floor of 30, while
# still being recent enough that the positional mean reflects current scoring
# environments. Only the single season before the target supplies the priors
# themselves; a two-season prior was measured and was worse at both positions.
KDST_HISTORY_SEASONS = 6

# Seasons of injury-report history the availability model trains on. Injury
# data starts at 2018 in nflreadpy, and the report vocabulary has been stable
# throughout, so more is strictly better here -- 6 is a compromise with fetch
# time, not a modelling limit.
AVAILABILITY_TRAIN_SEASONS = 6

# Seasons of completed play used to fit the variance components (k), the affine
# correction, the interval quantiles and the blend. Three gives ~35k
# player-weeks, comfortably above every one of those fits' minimum.
CALIBRATION_TRAIN_SEASONS = 3

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public" / "mock"
DEFAULT_SCORING_CONFIG_PATH = Path(__file__).resolve().parent / "leagues" / "league-1" / "scoring-config.json"


def normalize_team(team: Optional[str]) -> Optional[str]:
    if team is None:
        return None
    return TEAM_ABBR_DISPLAY_MAP.get(team, team)


# ---------------------------------------------------------------------------
# Pure assembly logic -- exercised directly by tests/test_generate_report.py
# with synthetic pool/schedule/game_log/news_flag data, no network involved.
# ---------------------------------------------------------------------------


def opponent_for_team_week(
    schedule_games: list[dict[str, Any]], team: str, season: int, week: int
) -> Optional[str]:
    """The other team in this team's game for (season, week), or None if
    there's no scheduled game (a bye week, or a week past the loaded
    schedule) -- callers use None to mean "not playable this week," not
    an error."""
    for game in schedule_games:
        if game.get("season") != season or game.get("week") != week:
            continue
        home, away = game.get("home_team"), game.get("away_team")
        if home == team:
            return away
        if away == team:
            return home
    return None


def build_player_pool_entry(player: dict[str, Any], news_flag: dict[str, Any]) -> dict[str, Any]:
    return {
        "playerId": player["playerId"],
        "name": player["name"],
        "position": player["position"],
        "team": player["team"],
        "newsFlag": news_flag,
    }


def _store_projection_rows(weekly_report: dict[str, Any]) -> list[dict[str, Any]]:
    """weekly-report JSON -> store `projections` rows.

    Note `points` is the EXPECTED value and `conditional_points` the
    if-he-plays number, matching the column comments in the migration. Getting
    these the wrong way round is the single easiest mistake a downstream query
    can make, which is why both are carried explicitly rather than one being
    derived.
    """
    rows = []
    for entry in weekly_report.get("projections", []):
        projection = entry.get("projection") or {}
        rows.append({
            "player_id": entry.get("playerId"),
            "tier": projection.get("tier"),
            "points": projection.get("points"),
            "conditional_points": projection.get("conditionalPoints"),
            "play_probability": projection.get("playProbability"),
            "floor": projection.get("floor"),
            "ceiling": projection.get("ceiling"),
        })
    return rows


def _mixture_band(
    interval_model: Optional[Any],
    position: str,
    conditional_points: float,
    play_probability: Optional[float],
    fallback: tuple[Optional[float], Optional[float]] = (None, None),
) -> dict[str, Optional[float]]:
    """{"floor", "ceiling"} for a tier's projection, accounting for the chance
    the player does not play at all.

    The published band describes the player's actual outcome, which is a
    mixture: exactly zero with probability 1 - p, the conditional distribution
    otherwise. IntervalModel.interval knows how to read that off the fitted
    quantile curve. When no model is available this falls back to the
    conditional band unchanged -- deliberately NOT scaled by p, because that
    transformation is simply wrong (it holds the floor away from zero while
    pulling the ceiling toward it). See docs/research/second-audit-2026-09-02.md.
    """
    if interval_model is not None:
        band = interval_model.interval(
            position, conditional_points, play_probability=play_probability
        )
        if band is not None:
            return {"floor": round(band[0], 2), "ceiling": round(band[1], 2)}
    lo, hi = fallback
    return {
        "floor": None if lo is None else round(lo, 2),
        "ceiling": None if hi is None else round(hi, 2),
    }


def _consensus_source_label(consensus: dict[str, Any]) -> Optional[str]:
    """Which feeds actually produced this consensus projection, or None to
    fall back to the tier default (entries built before the blend recorded
    provenance)."""
    try:
        from rotowire_projections import consensus_source_label

        return consensus_source_label(consensus)
    except Exception:  # noqa: BLE001
        return None


def _projection_object(
    points: float,
    tier: str,
    floor: Optional[float] = None,
    ceiling: Optional[float] = None,
    play_probability: Optional[float] = None,
    conditional_points: Optional[float] = None,
    source: Optional[str] = None,
) -> dict[str, Any]:
    """The public projection shape.

    `points` is the EXPECTED value -- P(play) x points-if-he-plays -- which is
    the quantity a start/sit comparison should be made on. `conditionalPoints`
    keeps the if-he-plays number visible next to it, because the two answer
    different questions and a reader deserves both: "he is a 14-point player
    but only 60% likely to suit up" is a different call from "he is an
    8.4-point player".

    `floor`/`ceiling` are the 10th/90th empirical residual percentiles, not a
    normal interval -- the outcome distribution is strongly right-skewed and a
    symmetric interval over-covers (0.86 at a nominal 0.80). Optional
    throughout: a tier or a run without a fitted interval model simply omits
    them, and the frontend renders the point estimate alone as before.
    """
    meta = TIER_METADATA[tier]
    projection: dict[str, Any] = {
        "tier": tier,
        "points": points,
        "tierLabel": meta["tierLabel"],
        # `source` overrides the tier default per player. The consensus tier
        # blends two feeds whose coverage only partly overlaps, so the tier
        # label names what the tier CAN use while this names what actually fed
        # this player -- claiming two sources agreed where one had no data
        # would misrepresent the number's provenance.
        "source": source or meta["source"],
    }
    if floor is not None and ceiling is not None:
        projection["floor"] = floor
        projection["ceiling"] = ceiling
    # Both only appear when availability was actually modelled. Without it
    # conditionalPoints is identical to points by construction, and emitting
    # the pair would invite a reader to believe a distinction is being drawn
    # that this run did not draw.
    if play_probability is not None:
        projection["playProbability"] = play_probability
        if conditional_points is not None:
            projection["conditionalPoints"] = conditional_points
    return projection


def build_projection_entry(candidate: dict[str, Any], tier: str = "in_house_estimate") -> dict[str, Any]:
    """`candidate` is the internal per-player shape assembled in
    build_weekly_report_and_pool below (playerId/name/position/team/
    opponent/points/news_flag/...) -- this reshapes it into the public
    WeeklyReport `projections[]` entry shape api.js's callers expect."""
    return {
        "playerId": candidate["playerId"],
        "name": candidate["name"],
        "position": candidate["position"],
        "team": candidate["team"],
        "opponent": candidate["opponent"],
        "projection": _projection_object(
            candidate["points"],
            tier,
            floor=candidate.get("floor"),
            ceiling=candidate.get("ceiling"),
            play_probability=candidate.get("play_probability"),
            conditional_points=candidate.get("conditional_points"),
            source=candidate.get("source"),
        ),
        "newsFlag": candidate["news_flag"],
    }


def build_waiver_target_entry(
    candidate: dict[str, Any], rationale: str, tier: str = "in_house_estimate"
) -> dict[str, Any]:
    entry = build_projection_entry(candidate, tier)
    entry["rationale"] = rationale
    return entry


def assemble_weekly_report(
    week: int,
    generated_at: str,
    projection_entries: list[dict[str, Any]],
    waiver_entries: list[dict[str, Any]],
    league_id: str = "league-1",
    league_format: str = "ppr",
) -> dict[str, Any]:
    """Assemble the weekly-report JSON dict.

    Args:
        league_id: stable string identifier for the league (keys output
            directories and localStorage). Defaults to "league-1" so callers
            that don't pass the kwarg get the same behavior as before.
        league_format: one of "ppr", "half_ppr", "standard". Passed through
            from the manifest's leagueFormat in the multi-league path so
            the report's leagueFormatAssumption field reflects the real
            per-league setting rather than the module-level constant.
    """
    return {
        "week": week,
        "leagueId": league_id,
        "leagueFormatAssumption": league_format,
        "generatedAt": generated_at,
        "projections": projection_entries,
        "waiverTargets": waiver_entries,
    }


def assemble_player_pool(pool_entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"players": pool_entries}


def build_weekly_report_and_pool(
    season: int,
    week: int,
    scoring_config: dict[str, Any],
    pool: list[dict[str, Any]],
    schedule_games: list[dict[str, Any]],
    game_logs_by_player: dict[str, list[dict[str, Any]]],
    news_flags_by_player: dict[str, dict[str, Any]],
    window: int = DEFAULT_WINDOW,
    generated_at: Optional[str] = None,
    consensus_projections: Optional[dict[str, dict[str, Any]]] = None,
    league_id: str = "league-1",
    league_format: str = "ppr",
    rostered_rank_cutoff: Optional[dict[str, int]] = None,
    rz_stats_by_player: Optional[dict[str, dict[str, Any]]] = None,
    week1_projections: Optional[dict[str, dict[str, Any]]] = None,
    play_probabilities: Optional[dict[str, float]] = None,
    positional_baselines: Optional[dict[str, float]] = None,
    entity_baselines: Optional[dict[str, float]] = None,
    shrinkage_ks: Optional[dict[str, float]] = None,
    affines: Optional[dict[str, tuple[float, float]]] = None,
    interval_model: Optional[Any] = None,
    blend_model: Optional[Any] = None,
    game_context: Optional[dict[str, dict[str, Any]]] = None,
    decay: float = DEFAULT_DECAY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The main assembly entry point, fully pure/injectable (all data
    already loaded by the caller) -- see the `load_*` adapters below for
    where `pool`/`schedule_games`/`game_logs_by_player`/
    `news_flags_by_player`/`consensus_projections` come from in a real run.

    Args:
        pool: [{"playerId", "name", "position", "team"}, ...] -- the
            season-wide player identity list (player-pool.json's source).
        schedule_games: [{"season", "week", "home_team", "away_team"}, ...].
        game_logs_by_player: {playerId: [{"season", "week", **stat_line}]}
            -- projections.py's game_log shape, per player.
        news_flags_by_player: {playerId: newsFlag}. A player missing here
            gets DEFAULT_NEWS_FLAG, not an error -- "no news layer ran for
            this player" degrades the same way news.py itself degrades on
            a failed fetch.
        consensus_projections: {playerId: {"projected_points", ...}} --
            fantasypros.build_consensus_tier's output. A player present
            here gets the FantasyPros consensus tier instead of the
            in-house estimate; a player absent (the common case -- this
            is only ever FantasyPros' top 10 per position) falls back to
            the in-house tier exactly as before this parameter existed.
            None/omitted (the default) means nobody gets the consensus
            tier -- same "off means arithmetically identical to before"
            contract as project_player's own optional parameters.
        league_id: stable string identifier passed through to
            assemble_weekly_report; appears as "leagueId" in the report
            JSON so the track-record layer and frontend can identify which
            league a file belongs to without depending on its directory
            path. Defaults to "league-1" for backward compatibility.
        league_format: one of "ppr", "half_ppr", "standard"; passed
            through to assemble_weekly_report as leagueFormatAssumption.
            In the multi-league path this comes from the manifest's
            leagueFormat field, not the module-level constant.
        rostered_rank_cutoff: per-position dict passed to
            select_waiver_targets to define the boundary between "rostered"
            and "waiver-eligible" players. None (the default) falls back to
            waiver_targets.py's DEFAULT_ROSTERED_RANK_CUTOFF (the 14-team
            default). The multi-league caller derives this from teamCount.
        rz_stats_by_player: optional {player_id: rz_stats_dict} from
            rotowire.fetch_and_cache_pool_stats. When supplied, waiver target
            rationale text is annotated with goal-line touch and tprr signals
            for players where those metrics exceed the thresholds in rotowire.py.
            None/omitted (the default) means no Rotowire annotation -- the
            rationale falls back to the existing points-trend-only text.
        week1_projections: {playerId: week1.Week1Model.predict_one output}.
            Only meaningful when week == 1, where project_player has no
            current-season games to average and every player lands in its
            no_data tier. Slots in BELOW the FantasyPros consensus tier and
            ABOVE the in-house rolling average, exactly mirroring
            consensus_projections' precedence and its "None means
            arithmetically identical to before" contract. See
            docs/research/week1-cold-start-model.md for why week 1 needs its
            own estimator rather than a flat positional baseline.
        play_probabilities: {playerId: P(play)} from availability.py. When
            supplied, every tier's `points` becomes an EXPECTED value
            (P(play) x if-he-plays) and the if-he-plays number is kept
            alongside as `conditionalPoints`. Worth +1.73 lineup pts/week on
            top of the estimator improvements -- the single largest measured
            lever in the backend. None (the default) reproduces the old
            conditional-on-playing behaviour exactly.
        entity_baselines: {playerId: float} -- a per-ENTITY baseline that wins
            over the positional one for that player. Built by week1_kdst.py for
            kickers and team defences in week 1, where a positional mean is the
            entire projection (no current-season log exists, so shrinkage
            weights the sample at zero and project_player returns the baseline
            verbatim) and therefore gives every kicker and every defence the
            same number. Empty or None restores exactly the old behaviour.
        positional_baselines: {position: float} -- what empirical-Bayes
            shrinkage pulls a thin sample toward. Keyed by POSITION, not by
            player: unlike the legacy window mode (which only shrank thin
            samples and needed a per-player thin/debut population), the
            empirical-Bayes weight applies at every sample size, so one
            baseline per position is the whole requirement. None disables
            shrinkage entirely -- project_player has nothing to shrink toward.
        shrinkage_ks: {playerId: k} for empirical-Bayes shrinkage, resolved
            from position by the caller (calibration.DEFAULT_SHRINKAGE_K).
        affines: {playerId: (a, b)} rank-preserving per-position recalibration
            from calibration.fit_affine.
        interval_model: a fitted calibration.IntervalModel, used to attach a
            floor/ceiling to each projection. None omits the interval fields.
        blend_model: a fitted blend.BlendModel. When supplied, the in-house
            tier's point estimate is refined with rolling volume and the
            week's game context (+volume/Vegas, audit recommendation 5).
        game_context: {team: context} from context.load_game_context_nflreadpy,
            needed by blend_model. Absent teams fall back to league-average
            context rather than failing.
        decay: recency-decay factor threaded to project_player and to the
            rolling-volume window, so both sides of the blend see the same
            weighting.

    Returns:
        (weekly_report, player_pool) -- weekly_report covers only players
        whose team has a scheduled game this week (bye-week players are
        skipped, not zero-projected); player_pool covers the full
        season-wide pool regardless of this week's schedule.
    """
    consensus_projections = consensus_projections or {}
    candidates = []
    blend_rows: list[dict[str, Any]] = []
    for player in pool:
        opponent = opponent_for_team_week(schedule_games, player["team"], season, week)
        if opponent is None:
            continue  # bye week (or a week outside the loaded schedule) -- not playable
        news_flag = news_flags_by_player.get(player["playerId"]) or dict(DEFAULT_NEWS_FLAG)
        consensus = consensus_projections.get(player["playerId"])
        if consensus is not None:
            candidate = {
                "playerId": player["playerId"],
                "name": player["name"],
                "position": player["position"],
                "team": player["team"],
                "opponent": opponent,
                "points": round(
                    consensus["projected_points"]
                    * ((play_probabilities or {}).get(player["playerId"], 1.0)),
                    2,
                ),
                # FantasyPros' consensus is a conditional-on-playing number too
                # -- it is a projected stat line for a player who suits up -- so
                # it needs the same availability multiplier, or the two tiers
                # would not be comparable in the same lineup.
                "conditional_points": consensus["projected_points"],
                "play_probability": (
                    None
                    if (play_probabilities or {}).get(player["playerId"]) is None
                    else round(play_probabilities[player["playerId"]], 3)
                ),
                # Interval: attached from the in-season residual model, which
                # buckets residuals by projection size and position. That model
                # was fitted on the in-house estimator's errors, so this is an
                # APPROXIMATION for a vendor projection -- but a defensible and
                # conservative one. Most of the band's width is irreducible
                # weekly outcome variance (audit 1 put the ICC ceiling on any
                # player-identity model at R^2 0.25-0.47), which belongs to the
                # player-week rather than to whoever produced the number. If the
                # consensus is the better estimator, this band is slightly too
                # WIDE, which is the safe direction to be wrong in. Showing no
                # band at all on the top-10-per-position players -- the ones
                # actually started -- was the worse option.
                **_mixture_band(
                    interval_model,
                    player["position"],
                    consensus["projected_points"],
                    (play_probabilities or {}).get(player["playerId"]),
                ),
                # NOTE: the affine recalibration is deliberately NOT applied
                # here, walking back part of the second audit's own
                # recommendation. `affines` corrects the in-house rolling
                # average's measured over-dispersion (fitted slope 0.826). A
                # vendor consensus projection has its own, unmeasured,
                # calibration -- and we have no held-out consensus history to
                # fit one from, because neither free feed publishes past weeks.
                # Applying our correction to their number would inject a bias
                # we have never measured, which is exactly the "guess dressed as
                # a measurement" week1.py refuses to make. Left uncorrected and
                # documented instead.
                "tier": "consensus",
                "source": _consensus_source_label(consensus),
                # Not meaningful for the consensus tier (FantasyPros doesn't
                # expose a game-by-game history, only the projection) --
                # harmless placeholders, not read by build_projection_entry.
                # waiver_targets.py never sees these values in practice:
                # its rostered-rank cutoff (>=14 per position) always
                # exceeds FantasyPros' top-10-per-position depth, so a
                # consensus-tier player is never in the waiver-eligible
                # pool to begin with.
                "games_used": 0,
                "confidence": None,
                "per_game_points": [],
                "news_flag": news_flag,
            }
        elif (week1_projections or {}).get(player["playerId"]) is not None:
            week1_projection = week1_projections[player["playerId"]]
            candidate = {
                "playerId": player["playerId"],
                "name": player["name"],
                "position": player["position"],
                "team": player["team"],
                "opponent": opponent,
                "points": round(
                    week1_projection["projected_points"]
                    * ((play_probabilities or {}).get(player["playerId"], 1.0)),
                    2,
                ),
                "conditional_points": week1_projection["projected_points"],
                # Mixture band, not the conditional band scaled by P(play) --
                # see IntervalModel.interval and the in-house tier below.
                **_mixture_band(
                    week1_projection.get("_interval_model"),
                    player["position"],
                    week1_projection["projected_points"],
                    (play_probabilities or {}).get(player["playerId"]),
                    fallback=(week1_projection.get("floor"), week1_projection.get("ceiling")),
                ),
                "play_probability": (
                    None
                    if (play_probabilities or {}).get(player["playerId"]) is None
                    else round(play_probabilities[player["playerId"]], 3)
                ),
                "tier": "week1_model",
                # This tier is cold-start by definition: there is no
                # current-season game log to show behind the number, which is
                # exactly what games_used=0 communicates to the report view.
                "games_used": 0,
                "confidence": week1_projection.get("confidence"),
                "per_game_points": [],
                "news_flag": news_flag,
            }
        else:
            player_id = player["playerId"]
            game_log = game_logs_by_player.get(player_id, [])
            cal_scale = POSITION_CALIBRATION_SCALE.get(player["position"], 1.0)
            projection = project_player(
                game_log, scoring_config, season, week,
                window=window, decay=decay, calibration_scale=cal_scale,
                positional_baseline=(
                    (entity_baselines or {}).get(player_id)
                    if (entity_baselines or {}).get(player_id) is not None
                    else (positional_baselines or {}).get(player["position"])
                ),
                shrinkage_k=(shrinkage_ks or {}).get(player_id),
                play_probability=(play_probabilities or {}).get(player_id),
                affine=(affines or {}).get(player_id),
            )
            points = projection["conditional_points"]
            if blend_model is not None:
                # The blend refines the CONDITIONAL number; availability is
                # re-applied below so the two stay composable and either can be
                # switched off without touching the other.
                volume = rolling_volume(game_log, season, week, window, decay)
                context = (game_context or {}).get(player["team"])
                blend_row = blend_build_feature_row(
                    player_id, player["position"], projection, volume, context
                )
                blend_rows.append(blend_row)
                points = blend_model.predict_one(blend_row)
                points = apply_affine(points, (affines or {}).get(player_id))

            probability = (play_probabilities or {}).get(player_id)
            expected = points if probability is None else points * probability

            floor = ceiling = None
            if interval_model is not None:
                # `points` is the CONDITIONAL projection, which is what the
                # residual quantiles were fitted against. The play probability
                # goes INTO the interval rather than being multiplied onto it
                # afterwards: the outcome is a mixture (zero if he does not
                # play), and the mixture's percentiles are the conditional
                # ones read at shifted levels. See IntervalModel.interval and
                # docs/research/second-audit-2026-09-02.md section 2 -- the old
                # `lo * p, hi * p` covered 26.5% at p=0.50 against a nominal 80%.
                band = interval_model.interval(
                    player["position"], points, play_probability=probability
                )
                if band is not None:
                    lo, hi = band
                    floor, ceiling = round(lo, 2), round(hi, 2)

            candidate = {
                "playerId": player_id,
                "name": player["name"],
                "position": player["position"],
                "team": player["team"],
                "opponent": opponent,
                "points": round(expected, 2),
                "conditional_points": round(points, 2),
                "play_probability": None if probability is None else round(probability, 3),
                "floor": floor,
                "ceiling": ceiling,
                "tier": "in_house_estimate",
                "games_used": projection["games_used"],
                "confidence": projection["confidence"],
                "per_game_points": projection["per_game_points"],
                "news_flag": news_flag,
            }
        candidates.append(candidate)

    if blend_model is not None:
        blend_model.check_coverage(blend_rows)

    projection_entries = [build_projection_entry(c, c["tier"]) for c in candidates]

    # Pass rostered_rank_cutoff through only when explicitly provided; None
    # means "use waiver_targets.py's own DEFAULT_ROSTERED_RANK_CUTOFF" so
    # the single-league code path is arithmetically identical to before.
    if rostered_rank_cutoff is not None:
        waiver_candidates = select_waiver_targets(candidates, rostered_rank_cutoff=rostered_rank_cutoff)
    else:
        waiver_candidates = select_waiver_targets(candidates)
    _rz = rz_stats_by_player or {}
    waiver_entries = [
        build_waiver_target_entry(
            c,
            generate_rationale(c, rz_stats=_rz.get(c["playerId"])),
            c["tier"],
        )
        for c in waiver_candidates
    ]

    weekly_report = assemble_weekly_report(
        week, generated_at or now_iso(), projection_entries, waiver_entries,
        league_id=league_id, league_format=league_format,
    )

    pool_entries = [
        build_player_pool_entry(p, news_flags_by_player.get(p["playerId"]) or dict(DEFAULT_NEWS_FLAG))
        for p in pool
    ]
    player_pool = assemble_player_pool(pool_entries)

    return weekly_report, player_pool


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _update_manifest(
    manifest_path: "Path",
    week: int,
    current_week: Optional[int] = None,
    team_count: Optional[int] = None,
) -> None:
    """Write/update manifest.json for a league's fixture directory.

    Tracks three different things that are easy to confuse:

      weeks        every week with a fixture on disk.
      latestWeek   the HIGHEST of those. This is a statement about files, not
                   about the season -- regenerating the whole season makes it
                   18 on the day before week 1.
      currentWeek  the week the season is actually on, from the real schedule
                   (season_week.py). This is what a UI should open on.
      teamCount    how many teams are in the league. Published because
                   replacement level is a per-league quantity: the Nth best
                   player at a position is startable somewhere in an 8-team
                   league and long since rostered in a 14-team one, and a
                   frontend comparing players across positions cannot compute
                   that without it.

    The frontend used to default to latestWeek and so opened on week 18 all
    preseason. Written as its own field rather than by redefining latestWeek,
    because "newest fixture" is still what the Explore view's week list needs.
    Omitted (not guessed) when the schedule cannot be read, so a consumer can
    tell "unknown" from "week 1".
    """
    existing: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    weeks: list[int] = existing.get("weeks", [])
    if week not in weeks:
        weeks.append(week)
    weeks.sort()
    manifest: dict[str, Any] = {"latestWeek": weeks[-1], "weeks": weeks}
    resolved = current_week if current_week is not None else existing.get("currentWeek")
    if resolved is not None:
        manifest["currentWeek"] = resolved
    teams = team_count if team_count is not None else existing.get("teamCount")
    if teams is not None:
        manifest["teamCount"] = teams
    manifest_path.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")


# ---------------------------------------------------------------------------
# Real data adapters -- NOT exercised by the test suite (network + nflreadpy
# required, same caveat as projections.load_recent_games_nflreadpy and
# news.fetch_espn_news). Run generate_report.py directly to exercise these.
# ---------------------------------------------------------------------------


def load_player_pool_nflreadpy(season: int) -> list[dict[str, Any]]:
    """Interim player-pool source per the integration plan's orchestration
    section: real ESPN league roster access doesn't exist yet, so this
    uses nflreadpy's full active-roster snapshot as a stand-in -- covers
    the general player pool, not this specific league's rostered players.

    Covers ROSTER_POSITIONS (QB/RB/WR/TE/K), not just POOL_POSITIONS --
    kickers resolve to a real gsis_id via this exact same roster snapshot
    (confirmed hands-on 2026-08-12: 33 real active 2025 kickers, e.g.
    Chris Boswell -> 00-0031136), so there's no reason to give them a
    separate pool-loading path the way DST needs (see
    load_dst_pool_nflreadpy, which is NOT sourced from this function --
    a DST is a team, not a roster entry)."""
    import nflreadpy as nfl

    try:
        rosters = nfl.load_rosters(seasons=[season])
        df = rosters.to_pandas() if hasattr(rosters, "to_pandas") else rosters
        # Include all 53-man rostered players, not just active — IR, inactive,
        # PUP, etc. should still appear in the report with their injury status
        # visible rather than silently vanishing from every list. Practice
        # squad players (TRC) are excluded because they're not eligible to
        # play or be rostered in most fantasy formats.
        PRACTICE_SQUAD_STATUSES = {"TRC", "PS"}
        df = df[~df["status"].isin(PRACTICE_SQUAD_STATUSES) & df["position"].isin(ROSTER_POSITIONS)]

        pool = []
        seen_ids = set()
        for _, row in df.iterrows():
            player_id = row.get("gsis_id")
            # `not player_id` does NOT catch a missing id here: pandas hands
            # back float("nan") for an empty gsis_id, and NaN is truthy, so a
            # NaN sailed through into the fixture as "playerId": NaN and made
            # the whole file fail json.dumps(allow_nan=False) at write time --
            # every projection lost for one unidentifiable roster row (seen on
            # the real 2025 roster feed). Test for a non-empty string instead.
            if not isinstance(player_id, str) or not player_id.strip() or player_id in seen_ids:
                # A mid-season trade can leave a player with more than one
                # row in a season-level roster snapshot -- keep the first.
                continue
            seen_ids.add(player_id)
            pool.append(
                {
                    "playerId": player_id,
                    "name": row.get("full_name"),
                    "position": row["position"],
                    "team": normalize_team(row.get("team")),
                    "espnId": row.get("espn_id"),
                }
            )
        return pool
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_player_pool_nflreadpy: nflreadpy's load_rosters() response shape didn't "
            "match this adapter's assumptions (expected columns include 'gsis_id', "
            "'full_name', 'position', 'team', 'status', 'espn_id'). Check nflreadpy's "
            "actual column names for your installed version and update this function."
        ) from exc


def load_schedule_nflreadpy(season: int) -> list[dict[str, Any]]:
    import nflreadpy as nfl

    try:
        schedules = nfl.load_schedules(seasons=[season])
        df = schedules.to_pandas() if hasattr(schedules, "to_pandas") else schedules
        return [
            {
                "season": int(row["season"]),
                "week": int(row["week"]),
                "home_team": normalize_team(row.get("home_team")),
                "away_team": normalize_team(row.get("away_team")),
            }
            for _, row in df.iterrows()
        ]
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_schedule_nflreadpy: nflreadpy's load_schedules() response shape didn't "
            "match this adapter's assumptions (expected columns include 'season', 'week', "
            "'home_team', 'away_team'). Check nflreadpy's actual column names for your "
            "installed version and update this function."
        ) from exc


def load_all_game_logs_nflreadpy(season: int) -> dict[str, list[dict[str, Any]]]:
    """Bulk equivalent of projections.load_recent_games_nflreadpy: one
    load_player_stats() call for the whole season instead of one per
    player, then grouped by player_id -- generating a full weekly report
    means every player in the pool needs a game log, so a per-player
    network call each would be needlessly slow.

    Merges scoring.nflreadpy_row_to_stat_line (offense) with
    kicker.nflreadpy_kicker_row_to_stat_line (K) on every row rather than
    branching on position: a QB/RB/WR/TE row has none of the kicker
    columns and a K row has none of the offense columns, and the two
    modules' output category names are confirmed disjoint (see
    tests/test_kicker.py), so the union is always exactly the row's real
    stat line either way -- no position check needed. This function
    already iterates every position in load_player_stats(), not just
    ROSTER_POSITIONS, so K rows were already reaching this loop before
    today; they just produced an empty stat_line (silently unused, since
    K wasn't in the pool) until this merge."""
    import nflreadpy as nfl

    from kicker import nflreadpy_kicker_row_to_stat_line
    from scoring import nflreadpy_row_to_stat_line

    try:
        stats = nfl.load_player_stats(seasons=[season])
    except ConnectionError:
        # nflverse hasn't published a stats file for this season yet --
        # confirmed for real against season=2026 before Week 1: the
        # current season's parquet 404s until games have actually been
        # played. Not a bug -- every player is a legitimate week-1
        # cold-start per projections.py's own as-of discipline ("a week 1
        # projection with no current-season games yet correctly returns
        # zero eligible games"), so degrade to "no game logs" rather than
        # crashing the whole report.
        print(f"  no nflreadpy player-stats file for season {season} yet -- treating as a full cold start.")
        return {}

    try:
        df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
        if "season_type" in df.columns:
            # Postseason weeks restart at 1 -- mixing them into a REG-season
            # game log would corrupt the as-of week ordering project_player
            # relies on.
            df = df[df["season_type"] == "REG"]

        game_logs: dict[str, list[dict[str, Any]]] = {}
        for _, row in df.iterrows():
            player_id = row.get("player_id")
            if not player_id:
                continue
            row_dict = row.to_dict()
            stat_line = {**nflreadpy_row_to_stat_line(row_dict), **nflreadpy_kicker_row_to_stat_line(row_dict)}
            stat_line["season"] = season
            stat_line["week"] = int(row["week"])
            stat_line["opponent_team"] = normalize_team(row.get("opponent_team"))
            # The blend was fitted on these columns (calibration_fit.load_game_logs
            # copies them); without them every volume feature is imputed at
            # prediction time and the blend collapses toward a positional mean.
            for column in VOLUME_COLUMNS:
                if column in row_dict:
                    stat_line[column] = row_dict[column]
            game_logs.setdefault(player_id, []).append(stat_line)
        return game_logs
    except (KeyError, AttributeError) as exc:
        raise RuntimeError(
            "load_all_game_logs_nflreadpy: nflreadpy's load_player_stats() response shape "
            "didn't match this adapter's assumptions (expected columns include 'player_id', "
            "'week', 'season_type', 'opponent_team'). Check nflreadpy's actual column names "
            "for your installed version and update this function."
        ) from exc


def load_dst_pool_and_game_logs_nflreadpy(season: int) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """DST equivalent of load_player_pool_nflreadpy + load_all_game_logs_nflreadpy
    combined into one adapter, since dst.py's pool and game-log sources
    (load_schedules(), load_teams(), load_team_stats()) are distinct calls
    from the player-roster/player-stats ones those two functions use, and
    a DST's game log needs the pool's own team list anyway to know which
    teams to build for.

    Same season-not-published-yet degrade-gracefully contract as
    load_all_game_logs_nflreadpy: nflverse's team-stats file 404s
    (ConnectionError) exactly when its player-stats file does, for the
    same reason (no games played yet this season) -- the DST pool itself
    still loads fine (it only needs the schedule + team list, both
    published pre-season), it's only the game logs that go empty, giving
    every DST the same week-1 cold start every offensive player already
    gets in that scenario."""
    from dst import build_dst_game_logs, load_dst_pool_nflreadpy, load_schedule_with_scores_nflreadpy, load_team_stats_nflreadpy

    pool = load_dst_pool_nflreadpy(season)
    try:
        team_stats_rows = load_team_stats_nflreadpy(season)
    except ConnectionError:
        print(f"  no nflreadpy team-stats file for season {season} yet -- DST treated as a full cold start too.")
        return pool, {}

    schedule_games = load_schedule_with_scores_nflreadpy(season)
    game_logs = build_dst_game_logs(team_stats_rows, schedule_games)
    return pool, game_logs


def build_consensus_tier_or_empty(season: int, week: int, scoring_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """FANTASYPROS_API_KEY missing, or any part of the fetch/crosswalk
    pipeline failing, degrades to an empty consensus tier -- every player
    falls back to the in-house estimate. Same "skip cleanly" contract
    build_news_client_or_none already establishes for the news layer,
    extended to cover network/API failures too, not just a missing key --
    a real, if unlikely, way for this to fail on the day of an actual
    weekly-report run, since the whole point of running this script is
    getting *a* report out, not blocking on one vendor's tier."""
    import os

    from fantasypros import build_consensus_tier, fetch_consensus_tier, load_fantasypros_id_crosswalk_nflreadpy

    api_key = os.environ.get("FANTASYPROS_API_KEY")
    if not api_key:
        return {}
    try:
        raw_players_by_position = fetch_consensus_tier(season, week, api_key)
        crosswalk = load_fantasypros_id_crosswalk_nflreadpy()
        return build_consensus_tier(raw_players_by_position, scoring_config, crosswalk)
    except Exception as exc:  # noqa: BLE001 -- degrade gracefully, don't block the report
        print(f"  FantasyPros consensus tier fetch failed ({exc}) -- falling back to in-house estimate for everyone.")
        return {}


def build_news_client_or_none() -> tuple[Any, Optional[str]]:
    """(client, reason) -- client is None when summarization can't run, and
    `reason` says WHY so the caller can print something actionable.

    Both failure modes used to collapse into one "ANTHROPIC_API_KEY not set"
    message, which is actively misleading: a missing `anthropic` package
    reported as a missing key and sent you looking at the wrong thing. That
    exact confusion cost a full pipeline run on 2026-09-01.

    Callers still treat a None client as "skip summarization, default newsFlag
    for everyone," per news.py's own degrade-gracefully contract."""
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "ANTHROPIC_API_KEY not set"
    try:
        from news import build_anthropic_client

        return build_anthropic_client(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def load_news_flags_nflreadpy(
    pool: list[dict[str, Any]],
    client: Any,
    espn_injury_rows: Optional[list[dict[str, Any]]] = None,
    store: Any = None,
) -> dict[str, dict[str, Any]]:
    """One fetch_espn_news() call for the whole pool, then per-player
    matching + summarization. Sleeper injury designations are fetched once
    and overlaid as the authoritative designation/riskLevel (Sleeper's
    injury report is more reliable than the LLM's read of a general news
    feed). LLM summary text is still used for the narrative where available."""
    from news import (
        apply_sleeper_designation,
        articles_for_player,
        fetch_espn_news,
        fetch_sleeper_injury_status,
        summarize_player_news,
    )

    articles = fetch_espn_news()
    sleeper_data = fetch_sleeper_injury_status()

    # ESPN's injury report is a far better LLM input than the general news
    # feed: it is per-player and already about availability, where the news
    # endpoint returns 50 league-wide articles that name-match only ~83 of a
    # 904-player pool. Both are used -- injury commentary first, general
    # articles appended -- so a player in the news for a non-injury reason
    # (a trade, a suspension, a depth-chart change) is still covered.
    espn_injury_by_player: dict[str, dict[str, Any]] = {}
    if espn_injury_rows:
        try:
            from espn_injuries import index_by_player

            espn_injury_by_player = index_by_player(espn_injury_rows, pool)
        except Exception:
            espn_injury_by_player = {}

    flags: dict[str, dict[str, Any]] = {}
    for player in pool:
        player_articles = []
        injury_row = espn_injury_by_player.get(player["playerId"])
        if injury_row:
            from espn_injuries import news_articles_from_injury

            player_articles += news_articles_from_injury(injury_row)
        player_articles += articles_for_player(articles, player["name"], player.get("espnId"))
        if not player_articles or client is None:
            flag = dict(DEFAULT_NEWS_FLAG)
        else:
            flag = summarize_player_news(
                player["name"], player_articles, client,
                store=store, player_id=player["playerId"],
            )
        flags[player["playerId"]] = apply_sleeper_designation(
            flag, player["name"], player.get("espnId"), sleeper_data
        )

    if client is not None:
        from news import SUMMARY_FAILURES

        summarized = sum(1 for f in flags.values() if f.get("summary"))
        # Report the failure count out loud. A per-player degrade that nobody
        # counts is how every LLM summarization in this pipeline failed
        # silently -- a markdown code fence around the JSON, swallowed by a
        # bare except, for every player in every run.
        print(
            f"  news: {len(articles)} ESPN articles, {summarized} players flagged"
            + (f", {len(SUMMARY_FAILURES)} summarization failures" if SUMMARY_FAILURES else "")
        )
        for failure in SUMMARY_FAILURES[:3]:
            print(f"    ! {failure}")
    return flags


def load_rotowire_stats_or_empty(
    pool: list[dict[str, Any]],
    season: int,
) -> dict[str, dict[str, Any]]:
    """Fetch Rotowire red zone / route-efficiency stats for the player pool,
    with a disk cache so repeated runs within 24 hours make zero network calls.

    Degrades to {} on any failure (missing package, network error, crosswalk
    load failure) -- callers treat an empty dict as "no Rotowire data this run."
    """
    try:
        from rotowire import fetch_and_cache_pool_stats, load_id_crosswalk

        crosswalk = load_id_crosswalk()
        if not crosswalk:
            print("  Rotowire crosswalk empty -- skipping Rotowire fetch.")
            return {}
        cache_path = Path(__file__).resolve().parent / f"rotowire_cache_{season}.json"
        return fetch_and_cache_pool_stats(pool, crosswalk, cache_path, season_year=season)
    except Exception as exc:  # noqa: BLE001
        print(f"  Rotowire fetch failed ({exc}) -- skipping Rotowire annotations.")
        return {}


def load_env_file(env_path: Optional[Path] = None) -> None:
    """Load the repo-root .env into os.environ before anything reads a key.

    Every key in this script is read straight off os.environ. The GitHub
    Actions workflows inject them as real environment variables, so CI was
    always fine -- but a local run saw an empty environment even with a
    fully-populated .env sitting one directory up, and nothing failed. The
    run just quietly produced a different report: no ANTHROPIC_API_KEY skips
    the news layer, no FANTASYPROS_API_KEY skips the consensus tier.

    That is not cosmetic. On 2026-09-20 a keyless local run put Dallas
    Goedert in the in-house tier at 3.52 points; with the key loaded he
    resolves to the consensus tier at 13.53 -- the difference between
    benching and starting him. Two easily-missed lines in a hundred lines of
    log output were the only evidence.

    `override=False` is python-dotenv's default and is deliberate here: the
    workflows' injected secrets must beat whatever a stale local .env
    carries.

    python-dotenv is in requirements.txt but deliberately NOT in
    requirements-dev.txt -- the test suite runs on pytest + requests alone
    (see that file's own note) and never calls main(). So a missing
    dependency warns and continues rather than raising: the script degrades
    to exactly the pre-2026-09-20 behaviour, and says so, because the whole
    point of this function is that the silent version cost a lineup call.

    `env_path` defaults to the repo-root .env and is a parameter so the tests
    can point at a temporary file -- the real .env is gitignored, so a test
    that depended on it would pass locally and prove nothing in CI.
    """
    import os

    try:
        from dotenv import load_dotenv
    except ImportError:
        print(
            "  python-dotenv not installed -- .env not read. Any key not already "
            "in the environment will be treated as absent (news layer and "
            "FantasyPros tier skipped)."
        )
        return

    if env_path is None:
        env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    load_dotenv(env_path)

    # Say which keys are live. The failure this function exists to prevent was
    # invisible precisely because absence was only ever reported downstream,
    # one line per consumer, long after the run had committed to it.
    present = [
        name
        for name in ("ANTHROPIC_API_KEY", "FANTASYPROS_API_KEY")
        if os.environ.get(name)
    ]
    missing = [
        name
        for name in ("ANTHROPIC_API_KEY", "FANTASYPROS_API_KEY")
        if not os.environ.get(name)
    ]
    print(
        f"  env: loaded {env_path.name}"
        + (f" -- {', '.join(present)} set" if present else "")
        + (f"; {', '.join(missing)} still missing" if missing else "")
    )


def should_skip_red_zone_fetch(
    week: int,
    game_logs: dict[str, Any],
    skip_flag: bool,
) -> tuple[bool, Optional[str]]:
    """Whether to skip the Rotowire red-zone fetch, and why.

    The fetch reads CURRENT-SEASON per-game logs, so it has nothing to return
    until a game has been played -- and it costs one request per player at a
    0.5s delay, about 25 minutes of no-op.

    This used to be `week == 1`, which reads the season's state off the week
    NUMBER. That is only the same thing during a season already under way.
    Regenerating weeks 2-18 of 2026 before kickoff paid the full no-op on all
    fifteen weeks and had to be worked around by hand.

    An empty `game_logs` is the direct signal: load_all_game_logs_nflreadpy and
    its DST counterpart both return nothing (and say so) for a season that has
    not started. Week 1 is kept as an explicit subset because it is true a
    priori and does not depend on a load having succeeded.

    Returns (skip, reason); reason is None when the fetch should run.
    """
    if skip_flag:
        return True, "--skip-rotowire set"
    if week == 1:
        return True, "week 1"
    if not game_logs:
        return True, "no games played yet this season"
    return False, None


def main(argv: Optional[list[str]] = None) -> None:
    import argparse
    import os

    load_env_file()

    # nflreadpy caches in MEMORY by default, so every run re-downloads every
    # file -- and a full report touches a dozen of them across several seasons.
    # That is slow, and it is what eventually got this project rate-limited:
    # nflverse's GitHub release assets start answering 404 (not 429) under
    # repeated traffic, which killed a run mid-way after ten minutes of work.
    # A filesystem cache with nflreadpy's own 24h duration makes repeat runs
    # nearly free and drops the request count to almost nothing.
    #
    # Set as a DEFAULT, not an override: an explicit NFLREADPY_CACHE in the
    # environment still wins, so CI or a debugging session can force a cold
    # fetch.
    os.environ.setdefault("NFLREADPY_CACHE", "filesystem")

    parser = argparse.ArgumentParser(
        description=(
            "Generate weekly-report-week-N.json and player-pool.json from real data "
            "sources (nflreadpy rosters/stats/schedules + ESPN news), per "
            "docs/design/backend-frontend-integration-plan.md."
        )
    )
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--skip-news",
        action="store_true",
        help="skip the ESPN news fetch + LLM summarization step; every player gets the default healthy newsFlag",
    )
    parser.add_argument(
        "--skip-fantasypros",
        action="store_true",
        help="skip the FantasyPros consensus-tier pull; every player gets the in-house estimate tier",
    )
    parser.add_argument(
        "--skip-availability",
        action="store_true",
        help=(
            "skip the availability model (backend/availability.py); projections stay "
            "conditional-on-playing, i.e. the pre-2026-09-01 behaviour"
        ),
    )
    parser.add_argument(
        "--skip-calibration",
        action="store_true",
        help=(
            "skip empirical-Bayes shrinkage, the affine recalibration and the prediction "
            "interval; projections are the raw rolling average"
        ),
    )
    parser.add_argument(
        "--skip-blend",
        action="store_true",
        help="skip the rolling-volume + Vegas-context blend (backend/blend.py)",
    )
    parser.add_argument(
        "--skip-week1-model",
        action="store_true",
        help=(
            "skip the week-1 cold-start model (backend/week1.py); week-1 players fall back to "
            "project_player's flat no_data baseline. No effect for weeks 2+, where the model never runs."
        ),
    )
    parser.add_argument(
        "--skip-rotowire",
        action="store_true",
        help="skip the Rotowire red zone / route-efficiency fetch; waiver rationale omits those annotations",
    )
    # --leagues-config and --scoring-config are mutually exclusive: passing
    # both is an argparse error. When neither is provided, --scoring-config
    # defaults to DEFAULT_SCORING_CONFIG_PATH (single-league path, same
    # behavior as before this argument existed).
    config_group = parser.add_mutually_exclusive_group()
    config_group.add_argument(
        "--leagues-config",
        type=Path,
        default=None,
        help=(
            "path to leagues.json manifest; when set, generates one report per "
            "league defined in the manifest. Mutually exclusive with --scoring-config."
        ),
    )
    config_group.add_argument(
        "--scoring-config",
        type=Path,
        default=DEFAULT_SCORING_CONFIG_PATH,
        help=(
            "path to a single-league scoring config JSON. "
            "Mutually exclusive with --leagues-config."
        ),
    )
    args = parser.parse_args(argv)

    # ---------------------------------------------------------------------------
    # Shared data loading -- happens once regardless of single vs multi-league.
    # Scoring-config-dependent steps (consensus-tier scoring) happen per-league.
    # ---------------------------------------------------------------------------

    print(f"Loading {args.season} player pool (nflreadpy rosters)...")
    pool = load_player_pool_nflreadpy(args.season)
    print(f"  {len(pool)} active {'/'.join(ROSTER_POSITIONS)} players")

    print(f"Loading {args.season} DST pool (nflreadpy schedules/teams)...")
    dst_pool, dst_game_logs = load_dst_pool_and_game_logs_nflreadpy(args.season)
    print(f"  {len(dst_pool)} DSTs, {len(dst_game_logs)} with at least one game logged")
    pool = pool + dst_pool

    print(f"Loading {args.season} schedule...")
    # The shared store (backend/store.py). Optional by construction: with no
    # DATABASE_URL and no FANTASY_STORE_DIR this is a NullStore and every call
    # below is a no-op, so the pipeline behaves exactly as it did before it
    # existed. See docs/design/shared-data-store.md.
    from store import config_hash as _config_hash
    from store import open_store, pack_bundle, unpack_bundle

    store = open_store()
    run_id = None
    if store.enabled:
        import subprocess

        try:
            git_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
            ).stdout.strip() or None
        except Exception:  # noqa: BLE001
            git_sha = None
        run_id = store.start_run(args.season, args.week, git_sha, None)
        if run_id:
            print(f"  store: run {run_id} opened")

    schedule_games = load_schedule_nflreadpy(args.season)

    # The week the SEASON is on, which is not the week being generated and not
    # the newest fixture on disk. Recorded in each league's manifest so the
    # frontend opens on it -- see _update_manifest. Never fatal: a failure here
    # leaves currentWeek off the manifest and the UI falls back, rather than
    # losing the whole report over a nice-to-have field.
    current_week: Optional[int] = None
    try:
        from season_week import load_current_week_nflreadpy

        current_week = load_current_week_nflreadpy(args.season)
        print(f"  season is on week {current_week}")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not resolve the current week ({exc}) -- manifest omits it.")

    print(f"Loading {args.season} game logs (nflreadpy player stats)...")
    game_logs = load_all_game_logs_nflreadpy(args.season)
    game_logs.update(dst_game_logs)  # DST keyed by team abbreviation, players by gsis_id -- no collision

    # Fetched once (~9 MB) and shared by the news layer and the availability
    # model, which both want it for different reasons.
    espn_injury_rows: list[dict[str, Any]] = []
    try:
        from espn_injuries import fetch_injury_rows

        espn_injury_rows = fetch_injury_rows()
        if espn_injury_rows:
            print(f"Loaded ESPN injury report: {len(espn_injury_rows)} listed players.")
        else:
            from espn_injuries import FETCH_FAILURES

            reason = FETCH_FAILURES[-1] if FETCH_FAILURES else "no rows returned"
            print(f"  ESPN injury report empty ({reason}) -- falling back to Sleeper alone.")
    except Exception as exc:  # noqa: BLE001
        print(f"  ESPN injury report unavailable ({exc}) -- falling back to Sleeper alone.")

    if args.skip_news:
        print("--skip-news set: every player gets the default healthy newsFlag.")
        news_flags: dict[str, dict[str, Any]] = {}
    else:
        client, client_error = build_news_client_or_none()
        if client is None:
            print(
                f"News summarization skipped ({client_error}) -- default newsFlag for everyone. "
                "Sleeper injury designations are still applied."
            )
        else:
            print("Fetching + summarizing ESPN news...")
        news_flags = load_news_flags_nflreadpy(pool, client, espn_injury_rows, store=store)

    # Fetch raw FantasyPros + Rotowire data once (network calls); scoring is
    # applied per-league below so each league's projected_points reflect its
    # own config. Both sources cover the same 10-per-position cap; blending
    # them averages the stat lines for overlapping players, giving a more
    # accurate consensus estimate than either alone.
    raw_fp_players_by_position: Optional[dict[str, Any]] = None
    fp_crosswalk: Optional[dict[str, Any]] = None
    if args.skip_fantasypros:
        print("--skip-fantasypros set: every player gets the in-house estimate tier.")
    elif os.environ.get("FANTASYPROS_API_KEY") is None:
        print("FANTASYPROS_API_KEY not set -- FantasyPros consensus tier skipped.")
    else:
        print(f"Fetching FantasyPros consensus tier ({'/'.join(POOL_POSITIONS)})...")
        try:
            from fantasypros import fetch_consensus_tier, load_fantasypros_id_crosswalk_nflreadpy

            raw_fp_players_by_position = fetch_consensus_tier(
                args.season, args.week, os.environ["FANTASYPROS_API_KEY"]
            )
            fp_crosswalk = load_fantasypros_id_crosswalk_nflreadpy()
        except Exception as exc:  # noqa: BLE001
            print(f"  FantasyPros fetch failed ({exc}) -- FantasyPros tier skipped.")
            raw_fp_players_by_position = None
            fp_crosswalk = None

    raw_rw_players_by_position: Optional[dict[str, Any]] = None
    if not args.skip_fantasypros:
        print(f"Fetching Rotowire weekly projections ({'/'.join(POOL_POSITIONS)})...")
        try:
            from rotowire_projections import fetch_rotowire_projections

            raw_rw_players_by_position = fetch_rotowire_projections(args.week)
        except Exception as exc:  # noqa: BLE001
            print(f"  Rotowire projections fetch failed ({exc}) -- Rotowire tier skipped.")
            raw_rw_players_by_position = None

    def _build_consensus_for_config(
        scoring_config: dict[str, Any],
        pool_for_crosswalk: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Apply scoring config to already-fetched FantasyPros + Rotowire data,
        blend the two by averaging stat lines for overlapping players.
        Returns {} (all in-house) if both fetches failed/skipped."""
        from rotowire_projections import (
            blend_consensus_projections,
            build_name_team_crosswalk,
            build_rotowire_consensus_tier,
        )

        fp_result: dict[str, dict[str, Any]] = {}
        if raw_fp_players_by_position is not None:
            try:
                from fantasypros import build_consensus_tier

                fp_result = build_consensus_tier(raw_fp_players_by_position, scoring_config, fp_crosswalk)
            except Exception as exc:  # noqa: BLE001
                print(f"  FantasyPros scoring failed ({exc}).")

        rw_result: dict[str, dict[str, Any]] = {}
        if raw_rw_players_by_position is not None and pool_for_crosswalk:
            try:
                xwalk = build_name_team_crosswalk(pool_for_crosswalk)
                rw_result = build_rotowire_consensus_tier(raw_rw_players_by_position, scoring_config, xwalk)
            except Exception as exc:  # noqa: BLE001
                print(f"  Rotowire scoring failed ({exc}).")

        if not fp_result and not rw_result:
            return {}

        result = blend_consensus_projections(fp_result, rw_result, scoring_config)
        fp_only = len(fp_result) - len(set(fp_result) & set(rw_result))
        rw_only = len(rw_result) - len(set(fp_result) & set(rw_result))
        both = len(set(fp_result) & set(rw_result))
        print(
            f"  {len(result)} players resolved to the consensus tier "
            f"(both={both}, FP-only={fp_only}, RW-only={rw_only})"
        )
        return result


    # ------------------------------------------------------------------
    # Week-1 cold-start tier. Only ever built when --week 1: from week 2 on,
    # project_player has real current-season games and this model's inputs
    # (last season + draft capital + the opening line) are strictly worse
    # than what actually happened this year. Fitted per run rather than
    # persisted -- the fit is a few hundred rows per position and takes well
    # under a second, so there is no artefact to version or go stale.
    # ------------------------------------------------------------------
    def _build_week1_for_config(scoring_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        if args.week != 1 or args.skip_week1_model:
            return {}
        try:
            import week1 as week1_module

            train_seasons = list(range(args.season - WEEK1_TRAIN_SEASONS, args.season))
            history_seasons = list(range(train_seasons[0] - 2, args.season))
            history = week1_module.load_history_nflreadpy(history_seasons, scoring_config)
            context = {s: week1_module.load_week1_context_nflreadpy(s) for s in history_seasons + [args.season]}
            meta = {s: week1_module.load_player_meta_nflreadpy(s) for s in history_seasons + [args.season]}
            dvp = {s: week1_module.prior_season_dvp(history, s - 1) for s in history_seasons + [args.season]}

            training_rows = week1_module.training_rows_from_history(
                train_seasons, history, context, meta, dvp
            )
            model = week1_module.Week1Model().fit(training_rows)
            target_rows = week1_module.build_feature_rows(
                args.season,
                [g for g in history if int(g["season"]) in (args.season - 1, args.season - 2)],
                context.get(args.season, {}),
                meta.get(args.season, {}),
                dvp.get(args.season),
            )
            result = model.predict(target_rows)
            # Intervals for this tier come from the week-1 model's OWN held-out
            # residuals, not the in-season band -- different estimator, different
            # error profile. Attached here so the caller does not have to know
            # which tier a player landed in.
            interval_model = week1_module.fit_interval_model(training_rows)
            if interval_model is not None:
                for row in target_rows:
                    projection = result.get(row["player_id"])
                    if projection is None:
                        continue
                    band = interval_model.interval(row["position"], projection["projected_points"])
                    if band is not None:
                        projection["floor"], projection["ceiling"] = band
                    # The band above is CONDITIONAL on playing. Carry the fitted
                    # model forward so assemble_weekly_report can re-derive the
                    # mixture band once P(play) is known -- multiplying the
                    # conditional endpoints by p is not the same thing, and was
                    # the defect this fixes.
                    projection["_interval_model"] = interval_model
            print(f"  {len(result)} players projected by the week-1 cold-start model "
                  f"(trained on {len(training_rows)} rows from {train_seasons[0]}-{train_seasons[-1]}"
                  f"{'; interval from held-out residuals' if interval_model else '; no interval'})")
            return result
        except Exception as exc:  # noqa: BLE001
            # Same degrade-gracefully contract as the consensus tier: a failure
            # here drops every player back to project_player's no_data
            # baseline, which is what week 1 did before this model existed.
            print(f"  Week-1 model failed ({exc}) -- falling back to the in-house no_data baseline.")
            return {}


    # ------------------------------------------------------------------
    # Availability, calibration and the volume/context blend -- the three
    # pieces of docs/research/scoring-engine-and-model-audit.md section 8 that
    # need real history to fit. Built once per run and shared across leagues:
    # none of them depends on league scoring except the blend, which is
    # refitted per config below because its target is league points.
    #
    # Measured together on held-out 2024-25, scoring missed games as the zeros
    # they are: +3.70 lineup pts/week over the previous estimator, of which
    # +1.73 is availability alone (p < 1e-240).
    # ------------------------------------------------------------------
    availability_model = None
    play_probabilities: dict[str, float] = {}
    if args.skip_availability:
        print("--skip-availability set: projections stay conditional-on-playing.")
    else:
        try:
            import availability as availability_module

            train_seasons = list(range(args.season - AVAILABILITY_TRAIN_SEASONS, args.season))
            rows = availability_module.build_training_rows_nflreadpy(train_seasons)
            availability_model = availability_module.AvailabilityModel().fit(rows)

            # nflreadpy's injury feed stops at the most recent COMPLETED season,
            # so a pre-season or week-1 report for the upcoming season finds
            # nothing there and falls back to Sleeper's live designations --
            # the only source that knows who is hurt right now. The source is
            # printed because the two are not equivalent: Sleeper has no
            # practice participation, which is what separates a Questionable
            # who practised fully (0.79) from one who did not practise (0.51).
            report_by_player, report_source = availability_module.load_current_injury_report(
                args.season, args.week, pool, espn_rows=espn_injury_rows
            )
            # Play-rate history for the CURRENT season, as-of this week. Uses
            # each player's own team's weeks as the denominator so a bye is not
            # counted as a missed game.
            weeks_by_player: dict[str, set] = {}
            team_of_player: dict[str, Optional[str]] = {}
            for player_id, log in game_logs.items():
                weeks_by_player[player_id] = {
                    g["week"] for g in log if g.get("season") == args.season
                }
                team_of_player[player_id] = next(
                    (g.get("team") for g in reversed(log) if g.get("team")), None
                )
            team_weeks: dict[str, set] = {}
            for sched in schedule_games:
                if sched.get("season") != args.season or sched.get("week", 0) >= args.week:
                    continue
                for side in ("home_team", "away_team"):
                    if sched.get(side):
                        team_weeks.setdefault(sched[side], set()).add(sched["week"])

            # Depth-chart rank for the week being projected. This is what
            # keeps an undesignated week-1 starter from being handed the same
            # flat positional base rate as a third-stringer -- worth AUC
            # 0.823 -> 0.854 held out, and it moves a QB1 from 0.88 to 0.96
            # against an actual 0.98.
            depth_ranks: dict = {}
            try:
                from depth_charts import load_depth_ranks

                depth_ranks = load_depth_ranks([args.season])
                covered = sum(
                    1 for p in pool if (args.season, args.week, p["playerId"]) in depth_ranks
                )
                print(f"  depth chart: {covered}/{len(pool)} players ranked")
            except Exception as exc:  # noqa: BLE001
                print(f"  depth chart unavailable ({exc}) -- availability runs without it.")

            for player in pool:
                player_id = player["playerId"]
                if player["position"] in availability_module.ALWAYS_AVAILABLE_POSITIONS:
                    play_probabilities[player_id] = 1.0
                    continue
                played = weeks_by_player.get(player_id, set())
                scheduled = team_weeks.get(player["team"], set())
                observed = len(scheduled)
                rate = (len(played & scheduled) / observed) if observed else None
                designation = report_by_player.get(player_id, {})
                play_probabilities[player_id] = availability_model.predict_one(
                    {
                        "position": player["position"],
                        "prior_play_rate": rate,
                        "prior_games_observed": float(observed),
                        "report_status": designation.get("report_status"),
                        "practice_status": designation.get("practice_status"),
                        "depth_rank": depth_ranks.get((args.season, args.week, player_id)),
                    }
                )
            out_count = sum(1 for v in play_probabilities.values() if v < 0.2)
            print(
                f"  availability modelled for {len(play_probabilities)} players "
                f"({out_count} below 20% likely to play; injury report via "
                f"{report_source}, {len(report_by_player)} designations)"
            )
        except Exception as exc:  # noqa: BLE001
            # Same degrade-gracefully contract as every other optional layer:
            # no availability means projections revert to conditional-on-playing,
            # which is what they were before this existed.
            print(f"  Availability model failed ({exc}) -- projections stay conditional-on-playing.")
            play_probabilities = {}

    # Opportunity-based expected touchdowns and snap share. Loaded once and
    # shared: the TD/non-TD split is denominated in points and therefore has to
    # be applied per league, but the raw feeds do not depend on scoring.
    expected_td_rows: dict[tuple[int, int, str], dict[str, Any]] = {}
    snap_shares: dict[tuple[int, int, str], float] = {}
    if not args.skip_blend:
        try:
            from expected_td import load_expected_td_rows, load_snap_shares

            seasons_for_features = [args.season - 1, args.season]
            expected_td_rows = load_expected_td_rows(seasons_for_features)
            snap_shares = load_snap_shares(seasons_for_features)
            print(
                f"  {len(expected_td_rows)} expected-TD rows, "
                f"{len(snap_shares)} snap-share rows loaded."
            )
        except Exception as exc:  # noqa: BLE001
            # Same degrade-gracefully contract as every other optional layer --
            # without these the blend simply falls back to the feature set it
            # had before, which is the pre-2026-09-02 behaviour.
            print(f"  Expected-TD / snap-share feeds unavailable ({exc}) -- blend uses volume only.")

    def _game_logs_for_config(scoring_config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """game_logs with offense_pct and the TD/non-TD split attached.

        The TD/non-TD split is in points, so it is refitted per league -- a
        passing touchdown is worth 6 in league-1 and 4 in league-2, and a shared
        split would be wrong for one of them.

        Both the multi-league and the single-league path must go through here.
        The single-league path used to pass the raw logs straight through, so
        offense_pct -- a blend feature calibration_fit trains on -- was missing
        for every player it predicted, and check_coverage raised on it.
        """
        if not (expected_td_rows or snap_shares):
            return game_logs
        from expected_td import enrich_game_logs

        return enrich_game_logs(game_logs, expected_td_rows, snap_shares, scoring_config)

    game_context: dict[str, dict[str, Any]] = {}
    if not args.skip_blend:
        try:
            from context import load_game_context_nflreadpy

            game_context = load_game_context_nflreadpy(args.season, args.week)
        except Exception as exc:  # noqa: BLE001
            print(f"  Game context (Vegas lines) unavailable ({exc}) -- blend uses league averages.")


    def _build_calibration_for_config(scoring_config: dict[str, Any]) -> dict[str, Any]:
        """Fit the pieces whose target is league points, so they have to be
        refitted per league config: the per-position empirical-Bayes k, the
        rank-preserving affine correction, the residual-quantile interval
        model, and the volume/context blend.

        Everything is fitted on completed prior seasons only -- the same
        walk-forward discipline the projections themselves follow, so nothing
        here can see the week being projected.
        """
        out: dict[str, Any] = {
            "positional_baselines": {}, "shrinkage_ks": {}, "affines": {},
            "interval_model": None, "blend_model": None,
        }
        if args.skip_calibration:
            return out
        try:
            import calibration as calibration_module
            from calibration_fit import fit_from_history

            # Keyed on the SCORING CONFIG, not the league. Two leagues sharing
            # a config hit the same cached bundle, which is what removes the
            # per-league double-fit this loop otherwise pays for.
            cfg_hash = _config_hash(
                scoring_config,
                window=args.window,
                seasons=CALIBRATION_TRAIN_SEASONS,
                blend=not args.skip_blend,
            )
            cached = store.get_model_fit(
                args.season, args.week, "", cfg_hash, "calibration"
            )
            if cached is not None:
                print("  calibration: reused a cached fit (same scoring config)")
                return unpack_bundle(cached)

            fitted = fit_from_history(
                scoring_config,
                season=args.season,
                seasons=list(range(args.season - CALIBRATION_TRAIN_SEASONS, args.season)),
                window=args.window,
                fit_blend=not args.skip_blend,
            )
            store.put_model_fit(
                args.season, args.week, "", cfg_hash, "calibration", pack_bundle(fitted)
            )
            return fitted
        except Exception as exc:  # noqa: BLE001
            print(f"  Calibration/blend fit failed ({exc}) -- using uncalibrated projections.")
            return out

    # ------------------------------------------------------------------
    # Week-1 per-entity baselines for K and D/ST. Without these both
    # positions collapse to a single positional mean in week 1 -- every
    # kicker projecting the same number, every defence projecting the same
    # number -- which leaves two of the eight starting slots unrankable.
    # See backend/week1_kdst.py for the measurement behind it.
    # ------------------------------------------------------------------
    _kdst_cache: dict[str, dict[str, float]] = {}

    def _build_kdst_baselines_for_config(scoring_config: dict[str, Any]) -> dict[str, float]:
        if args.week != 1 or args.skip_week1_model:
            return {}
        # Keyed on the scoring config, not the league: the estimate is a
        # prior-season average scored through that config, so two leagues
        # sharing one config share the answer.
        cache_key = _config_hash(scoring_config, seasons=KDST_HISTORY_SEASONS)
        if cache_key in _kdst_cache:
            return _kdst_cache[cache_key]
        merged: dict[str, float] = {}
        try:
            import week1_kdst

            history_seasons = [
                s for s in range(args.season - KDST_HISTORY_SEASONS, args.season)
                # 2020 is excluded everywhere in this project's week-1 work:
                # empty stadiums and distorted lines make it a poor prior.
                if s != 2020
            ]
            fits = week1_kdst.fit_from_history(args.season, scoring_config, history_seasons)
            for position, fit in fits.items():
                merged.update(fit["estimates"])
                print(
                    f"  week-1 {position}: {len(fit['estimates'])} per-entity baselines "
                    f"(k={fit['k']} fitted on {fit['n_fit']} rows, "
                    f"positional mean {fit['positional_mean']})"
                )
        except Exception as exc:  # noqa: BLE001
            # Never fatal: falling back to the flat positional baseline is the
            # previous behaviour, which was usable if uninformative.
            print(f"  week-1 K/DST per-entity baselines failed ({exc}) -- using flat baselines.")
            merged = {}
        _kdst_cache[cache_key] = merged
        return merged

    # ------------------------------------------------------------------
    # Weeks 2+: per-player prior-season shrinkage targets for QB/RB/WR/TE.
    # Without these the in-house tier collapses toward one number per
    # position in the early weeks -- in the real 2026 week-2 report, every
    # non-consensus tight end landed in a 2.33-4.91 band (sd 0.56) and a
    # 95%-snap starter ranked 126th of 209. See backend/entity_prior.py for
    # the measurement, including what survives the blend and what does not.
    # ------------------------------------------------------------------
    _entity_prior_cache: dict[str, dict[str, float]] = {}

    def _build_entity_priors_for_config(
        scoring_config: dict[str, Any], positional_baselines: dict[str, Any]
    ) -> dict[str, float]:
        if args.week < entity_prior.FIRST_COVERED_WEEK:
            return {}
        if not positional_baselines:
            # Nothing to shrink toward means nothing to build a target from --
            # the same degrade project_player already makes.
            return {}
        cache_key = _config_hash(scoring_config, seasons=1)
        if cache_key in _entity_prior_cache:
            return _entity_prior_cache[cache_key]
        built: dict[str, float] = {}
        try:
            prior_logs = entity_prior.load_prior_season_logs_nflreadpy(args.season)
            means = entity_prior.entity_season_means(prior_logs, scoring_config)
            built = entity_prior.entity_baselines(means, positional_baselines)
            print(
                f"  prior-season shrinkage targets: {len(built)} players "
                f"(QB/RB/WR/TE, from {args.season - 1}, k={entity_prior.DEFAULT_PRIOR_K})"
            )
        except Exception as exc:  # noqa: BLE001
            # Never fatal: the flat positional baseline is the previous
            # behaviour -- collapsed, but not broken.
            print(f"  prior-season shrinkage targets failed ({exc}) -- using flat baselines.")
            built = {}
        _entity_prior_cache[cache_key] = built
        return built

    # Rotowire red zone / route-efficiency stats -- fetched once per run (cached
    # to disk, so repeated same-day runs are free). The result is shared across
    # leagues since it's player-level, not league-scoring-dependent.
    rz_stats: dict[str, dict[str, Any]] = {}
    skip_rz, skip_rz_reason = should_skip_red_zone_fetch(
        args.week, game_logs, args.skip_rotowire
    )
    if skip_rz:
        # Reasoning lives in should_skip_red_zone_fetch's docstring.
        print(
            f"Skipping the Rotowire red-zone fetch ({skip_rz_reason}) -- "
            "waiver rationale will omit red zone / tprr annotations."
        )
    else:
        print(f"Fetching Rotowire red zone / route stats ({args.season} season)...")
        rz_stats = load_rotowire_stats_or_empty(pool, args.season)
        if rz_stats:
            print(f"  {len(rz_stats)} players with Rotowire data.")

    # ---------------------------------------------------------------------------
    # Multi-league path: load manifest, loop over leagues.
    # ---------------------------------------------------------------------------

    if args.leagues_config:
        leagues_manifest = json.loads(args.leagues_config.read_text())
        backend_dir = Path(__file__).resolve().parent

        for league in leagues_manifest["leagues"]:
            league_id: str = league["leagueId"]
            league_format: str = league["leagueFormat"]
            team_count: int = league["teamCount"]

            scoring_config_path = backend_dir / league["scoringConfigPath"]
            scoring_config = json.loads(scoring_config_path.read_text())

            if scoring_config.get("_PLACEHOLDER"):
                print(
                    f"WARNING: {league_id} scoring config is still a placeholder -- "
                    "projections will be inaccurate until real values are entered"
                )
            for unconfirmed in scoring_config.get("_unconfirmed", []):
                # Named, measured caveats rather than a blanket flag. A warning
                # that fires on every run is a warning nobody reads -- and
                # league-1's did, for two versions after its values became real.
                print(f"  NOTE ({league_id}): {unconfirmed.split(' -- ')[0]} is unconfirmed.")

            # Per-position waiver cutoff derived from team count -- reproduces
            # DEFAULT_ROSTERED_RANK_CUTOFF's formula (see waiver_targets.py).
            rostered_rank_cutoff = {
                "QB": team_count,
                "RB": team_count * 2,
                "WR": team_count * 2,
                "TE": team_count,
                "DST": team_count,
                "K": team_count,
            }

            print(f"\n--- Generating report for {league_id} ({league['displayName']}) ---")
            consensus_projections = _build_consensus_for_config(scoring_config, pool)

            league_game_logs = _game_logs_for_config(scoring_config)

            out_dir = backend_dir / league["outDir"]
            out_dir.mkdir(parents=True, exist_ok=True)

            # Resolved once rather than splatted inline: the prior-season
            # targets need this bundle's positional_baselines to shrink toward.
            calibration_bundle = _build_calibration_for_config(scoring_config)

            weekly_report, player_pool = build_weekly_report_and_pool(
                args.season,
                args.week,
                scoring_config,
                pool,
                schedule_games,
                league_game_logs,
                news_flags,
                window=args.window,
                consensus_projections=consensus_projections,
                week1_projections=_build_week1_for_config(scoring_config),
                entity_baselines={
                    # Disjoint by construction -- K/D/ST in week 1, QB/RB/WR/TE
                    # from week 2 -- so the merge order cannot matter.
                    **_build_kdst_baselines_for_config(scoring_config),
                    **_build_entity_priors_for_config(
                        scoring_config, calibration_bundle.get("positional_baselines") or {}
                    ),
                },
                play_probabilities=play_probabilities,
                game_context=game_context,
                decay=DEFAULT_DECAY,
                **calibration_bundle,
                league_id=league_id,
                league_format=league_format,
                rostered_rank_cutoff=rostered_rank_cutoff,
                rz_stats_by_player=rz_stats,
            )

            report_path = out_dir / f"weekly-report-week-{args.week}.json"
            pool_path = out_dir / "player-pool.json"
            report_path.write_text(json.dumps(weekly_report, indent=2, allow_nan=False) + "\n")
            pool_path.write_text(json.dumps(player_pool, indent=2, allow_nan=False) + "\n")
            manifest_path = out_dir / "manifest.json"
            _update_manifest(manifest_path, args.week, current_week, team_count)
            print(
                f"Wrote {report_path} "
                f"({len(weekly_report['projections'])} projections, {len(weekly_report['waiverTargets'])} waiver targets)"
            )
            print(f"Wrote {pool_path} ({len(player_pool['players'])} players)")

            # Alongside the fixture, never instead of it. The fixture stays the
            # only thing the frontend reads (design doc, "explicitly out of
            # scope"); this is the append-only record the eval layer will move
            # onto in phase 2.
            written = store.write_projections(
                run_id, league_id, args.season, args.week,
                _store_projection_rows(weekly_report),
            )
            if written:
                print(f"  store: logged {written} projections for {league_id}")

    # ---------------------------------------------------------------------------
    # Single-league path: backward-compatible behavior, unchanged.
    # ---------------------------------------------------------------------------

    else:
        scoring_config = json.loads(args.scoring_config.read_text())
        consensus_projections = _build_consensus_for_config(scoring_config, pool)

        calibration_bundle = _build_calibration_for_config(scoring_config)

        weekly_report, player_pool = build_weekly_report_and_pool(
            args.season,
            args.week,
            scoring_config,
            pool,
            schedule_games,
            _game_logs_for_config(scoring_config),
            news_flags,
            window=args.window,
            consensus_projections=consensus_projections,
            week1_projections=_build_week1_for_config(scoring_config),
            entity_baselines={
                **_build_kdst_baselines_for_config(scoring_config),
                **_build_entity_priors_for_config(
                    scoring_config, calibration_bundle.get("positional_baselines") or {}
                ),
            },
            play_probabilities=play_probabilities,
            game_context=game_context,
            decay=DEFAULT_DECAY,
            **calibration_bundle,
            rz_stats_by_player=rz_stats,
        )

        args.out_dir.mkdir(parents=True, exist_ok=True)
        report_path = args.out_dir / f"weekly-report-week-{args.week}.json"
        pool_path = args.out_dir / "player-pool.json"
        report_path.write_text(json.dumps(weekly_report, indent=2, allow_nan=False) + "\n")
        pool_path.write_text(json.dumps(player_pool, indent=2, allow_nan=False) + "\n")
        manifest_path = args.out_dir / "manifest.json"
        _update_manifest(manifest_path, args.week, current_week)

        print(
            f"Wrote {report_path} "
            f"({len(weekly_report['projections'])} projections, {len(weekly_report['waiverTargets'])} waiver targets)"
        )
        print(f"Wrote {pool_path} ({len(player_pool['players'])} players)")

        written = store.write_projections(
            run_id, "single", args.season, args.week,
            _store_projection_rows(weekly_report),
        )
        if written:
            print(f"  store: logged {written} projections")

    # Close the run last, whatever path ran. A run left 'running' is a run that
    # crashed, which is exactly what the status column is for -- so this is
    # deliberately not in a finally that would mark a crash as 'ok'.
    store.finish_run(run_id, "ok")
    store.close()


if __name__ == "__main__":
    main()
