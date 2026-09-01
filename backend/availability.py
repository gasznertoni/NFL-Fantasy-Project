"""
Availability model: P(this player takes the field this week).

Why this is the highest-value piece in the backend. Every projection this repo
produces -- the rolling average, the FantasyPros consensus tier, the week-1
model -- is fitted on games that were actually played, so all of them answer
"how many points if he plays," while the report presents the number as an
expected value. Over 2018-2025, 20.6% of weeks inside a player's own active
span are missed (26.2% for QB, 23.9% TE, 18.5% RB, 18.4% WR), so the two
quantities differ by roughly a fifth, and by *different* fifths for different
players -- which is exactly what breaks a start/sit comparison.

Measured in simulated lineup points, modelling this is worth ~0.69 pts/week
using nothing but a historical play rate, against ~0.15 pts/week for every
window/decay/shrinkage knob in projections.py combined. With the real injury
report it is worth considerably more, because the single most valuable fact --
a player ruled Out has P(play) = 0.0006 -- is not in the play-rate history at
all. See docs/research/scoring-engine-and-model-audit.md section 6.

Design mirrors projections.py and week1.py: pure, injectable model math here,
with the nflreadpy adapter isolated at the bottom and not covered by the test
suite.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from ridge import logistic, num, predict_probability

# nflreadpy's load_injuries() report_status vocabulary. Empirical P(play) over
# 2018-2025, 57 050 player-weeks inside an active span:
#
#   Out           0.0006   (n=1 540)  <- effectively a certainty, and the single
#   Doubtful      0.0115   (n=  262)     most valuable fact the model can know
#   Questionable  0.6938   (n=2 815)
#   (not listed)  0.8271   (n=52 431)
#
# Practice participation splits Questionable further, and meaningfully:
# DNP 0.511, Limited 0.702, Full 0.790.
REPORT_STATUSES = ("Out", "Doubtful", "Questionable")
PRACTICE_STATUSES = (
    "Did Not Participate In Practice",
    "Limited Participation in Practice",
    "Full Participation in Practice",
)

# League-wide play rate, used as the prior a thin player history shrinks toward.
LEAGUE_PLAY_RATE = 0.79
# Games of prior history at which a player's own rate gets half the weight.
# Not swept -- the fit's own coefficient on the shrunk rate absorbs the scale,
# so this only has to be the right order of magnitude.
PLAY_RATE_PRIOR_GAMES = 4.0

DEFAULT_L2 = 1.0

# Depth-chart rank is the strongest predictor of whether a player records a
# game at all, and unlike play-rate history it exists before a season starts --
# which is precisely when the model is otherwise blind. Measured over 2018-24
# on players carrying NO injury designation:
#
#     position   depth 1   depth 2   depth 3+
#     QB          0.972     0.490     0.639
#     RB          0.941     0.880     0.730
#     WR          0.965     0.790     0.722
#     TE          0.928     0.788     0.643
#
# Without it every undesignated player at a position gets the same number --
# 0.76 for a QB, whether he is the week-1 starter (really 0.97) or third on
# the chart. That is a ~20-point error on exactly the players a lineup is
# built around.
#
# Entered as position-BY-depth indicators rather than one linear "depth" term,
# because the effect is not uniform across positions and is not even monotonic
# within them: a backup QB (0.49) behaves nothing like a backup RB (0.88),
# since a second-string running back still touches the ball and a second-string
# quarterback usually does not appear at all. A single slope cannot express
# that, and would average the two into a number wrong for both.
DEPTH_RANKS = (1, 2, 3)


# Applied when no model has been fitted and no history exists. Deliberately the
# league rate rather than 1.0: assuming everyone plays is the current behaviour,
# and it is the thing this module exists to stop doing.
FALLBACK_PLAY_RATE = LEAGUE_PLAY_RATE

# Positions for which availability is not a question. A team defence plays every
# week its team has a game -- there is no individual to rule out, and the report
# already skips a team on its bye -- so applying a play probability to a DST
# would discount it for a risk that does not exist. Kickers are NOT here: a
# kicker can be inactive like any other player, and the play-rate history works
# for them even though the fitted model was trained on QB/RB/WR/TE rows.
ALWAYS_AVAILABLE_POSITIONS = ("DST",)


def _bucket_rank(value: Any) -> Optional[int]:
    """Depth rank as 1/2/3, or None when unknown. Delegates to depth_charts so
    the capping rule is defined once."""
    try:
        from depth_charts import bucket_rank

        return bucket_rank(num(value))
    except Exception:
        return None


def _text(value: Any) -> str:
    """A designation as a clean string, or "" for absent.

    `value or ""` is NOT enough: pandas yields float("nan") for an empty cell
    and NaN is truthy, so the NaN itself would be returned and .strip() would
    raise. This is the same trap that hid the interception bug in scoring.py
    and the NaN playerId in generate_report.py -- worth one shared helper.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()


class AvailabilityModel:
    """L2-penalised logistic regression over the injury report, practice
    participation, the player's own shrunk play rate, and position.

    Held out on 2024-25 after fitting on 2018-23 (14 991 player-weeks):
    log loss 0.374 vs 0.509 for the league base rate, Brier 0.116 vs 0.164,
    AUC 0.823. Calibration is tight -- the largest gap between predicted and
    observed rate across probability deciles is 0.068, and the groups that
    drive decisions land almost exactly: Out predicts 0.007 against 0.000
    observed, Questionable 0.682 against 0.701.
    """

    def __init__(self, l2: float = DEFAULT_L2):
        self.l2 = l2
        self.beta: Optional[list[float]] = None
        self.positions: tuple[str, ...] = ()
        # Every position seen in training gets its own depth interactions --
        # unlike `positions`, no reference level is dropped, because the
        # "rank unknown" flag already carries the baseline.
        self.depth_positions: tuple[str, ...] = ()

    # -- features ----------------------------------------------------------
    def _row(self, row: dict[str, Any]) -> list[float]:
        report = _text(row.get("report_status"))
        practice = _text(row.get("practice_status"))
        features = [1.0 if report == s else 0.0 for s in REPORT_STATUSES]
        features += [1.0 if practice == s else 0.0 for s in PRACTICE_STATUSES]

        rate = num(row.get("prior_play_rate"))
        games = num(row.get("prior_games_observed")) or 0.0
        rate = LEAGUE_PLAY_RATE if rate is None else rate
        # Shrink the player's own rate toward the league rate by how much of it
        # we have actually seen -- 2 observed weeks is not evidence of a 50%
        # availability rate, it is evidence of nothing.
        shrunk = (games * rate + PLAY_RATE_PRIOR_GAMES * LEAGUE_PLAY_RATE) / (
            games + PLAY_RATE_PRIOR_GAMES
        )
        features.append(shrunk)
        # How much history there is, as its own feature: it lets the fit trust
        # the shrunk rate more when it rests on a full season than on two weeks.
        features.append(min(games, 17.0) / 17.0)

        position = row.get("position")
        features += [1.0 if position == p else 0.0 for p in self.positions]

        # position-by-depth indicators, plus one "rank unknown" flag. The
        # unknown flag matters: ~9% of player-weeks have no chart entry, and
        # that is its own state, not an average depth.
        depth = _bucket_rank(row.get("depth_rank"))
        for pos in self.depth_positions:
            for rank in DEPTH_RANKS:
                features.append(1.0 if (depth == rank and position == pos) else 0.0)
        features.append(1.0 if depth is None else 0.0)
        return features

    # -- fitting -----------------------------------------------------------
    def fit(self, rows: list[dict[str, Any]], min_rows: int = 200) -> "AvailabilityModel":
        """Fit on labelled player-weeks.

        Args:
            rows: dicts with "played" (1/0) plus any of report_status,
                practice_status, prior_play_rate, prior_games_observed,
                position. Rows must cover only weeks the player's team actually
                played and that fall inside the player's active span -- a bye
                is not a missed game, and neither is a week before the player
                was in the league. build_training_rows() enforces both.
            min_rows: below this, no fit is attempted and predict falls back to
                the shrunk play rate alone.
        """
        labelled = [r for r in rows if num(r.get("played")) is not None]
        if len(labelled) < min_rows:
            self.beta = None
            return self

        # One dummy per position seen in training, minus a reference level, so
        # the design stays full rank alongside the intercept.
        seen = sorted({r.get("position") for r in labelled if r.get("position")})
        self.positions = tuple(seen[:-1]) if len(seen) > 1 else ()
        self.depth_positions = tuple(seen)

        design = [self._row(r) for r in labelled]
        targets = [float(num(r["played"])) for r in labelled]
        self.beta = logistic(design, targets, l2=self.l2)
        return self

    # -- prediction --------------------------------------------------------
    def predict_one(self, row: dict[str, Any]) -> float:
        """P(play) in [0, 1] for one player-week."""
        if self.beta is None:
            features = self._row(row)
            # index of the shrunk-rate feature: after the report and practice
            # dummies
            return features[len(REPORT_STATUSES) + len(PRACTICE_STATUSES)]
        return predict_probability(self.beta, self._row(row))

    def predict(self, rows: Iterable[dict[str, Any]]) -> dict[str, float]:
        """{player_id: P(play)}."""
        return {r["player_id"]: self.predict_one(r) for r in rows}


def play_rate_history(
    game_weeks_by_player: dict[str, set],
    team_weeks: set,
    as_of_week: int,
) -> dict[str, dict[str, float]]:
    """Each player's play rate over the weeks strictly before `as_of_week`.

    Args:
        game_weeks_by_player: {player_id: {weeks they recorded a game}}.
        team_weeks: every week the player's team has already played. Passing
            the TEAM's weeks rather than a range(1, as_of_week) is what keeps a
            bye out of the denominator -- a bye is not a missed game, and
            counting it as one would drag every player's rate down by a week
            and make the bias position-dependent (teams bye in different weeks).
        as_of_week: the week being projected; only earlier weeks count.

    Returns:
        {player_id: {"prior_play_rate", "prior_games_observed"}}.
    """
    eligible = {w for w in team_weeks if w < as_of_week}
    denominator = len(eligible)
    out: dict[str, dict[str, float]] = {}
    for player_id, weeks in game_weeks_by_player.items():
        if denominator == 0:
            out[player_id] = {"prior_play_rate": None, "prior_games_observed": 0.0}
            continue
        played = len(weeks & eligible)
        out[player_id] = {
            "prior_play_rate": played / denominator,
            "prior_games_observed": float(denominator),
        }
    return out


def expected_points(conditional_points: float, play_probability: float) -> float:
    """The number a start/sit decision should actually compare.

    E[points] = P(play) x E[points | play] + (1 - P(play)) x 0.

    The zero is not an approximation: a player who does not take the field
    scores exactly zero in every fantasy league, so the second term vanishes
    exactly rather than being dropped for convenience.
    """
    p = min(max(play_probability, 0.0), 1.0)
    return conditional_points * p


# ---------------------------------------------------------------------------
# Real data adapters -- NOT exercised by the test suite (network + nflreadpy).
# ---------------------------------------------------------------------------
def load_injury_report_nflreadpy(season: int, week: int) -> dict[str, dict[str, Any]]:
    """{player_id: {"report_status", "practice_status"}} for one week.

    Legitimately as-of: the injury report for week w is published before week
    w's games. A player absent from the report simply has no designation, which
    the model reads as the (empirically 0.827) no-designation state -- not as
    missing data to impute.
    """
    import nflreadpy as nfl

    try:
        frame = nfl.load_injuries(seasons=[season])
        frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
        frame = frame[(frame["season"] == season) & (frame["week"] == week)]
    except Exception:
        # Degrade to "no designation for anybody", which leaves the model
        # running on play-rate history alone rather than failing the report.
        return {}

    out: dict[str, dict[str, Any]] = {}
    for record in frame.to_dict("records"):
        player_id = record.get("gsis_id")
        if not isinstance(player_id, str) or not player_id.strip():
            continue
        out[player_id] = {
            "report_status": _text(record.get("report_status")) or None,
            "practice_status": _text(record.get("practice_status")) or None,
        }
    return out


# Sleeper's injury vocabulary -> this model's REPORT_STATUSES. Sleeper is the
# only LIVE injury source for a season nflreadpy has not published yet (its
# load_injuries() caps at the most recent completed season), which makes it the
# only option for a week-1 report before the season starts.
#
# IR and PUP collapse to "Out": both mean the player is unavailable this week,
# and "Out" is the status the model has actually seen and calibrated on
# (P(play) = 0.0006). Mapping them to their own level would mean extrapolating a
# coefficient from no training data for no gain -- the answer is already ~0.
_SLEEPER_TO_REPORT_STATUS = {
    "Out": "Out",
    "IR": "Out",
    "Doubtful": "Doubtful",
    "Questionable": "Questionable",
}


def load_injury_report_sleeper(
    players: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """{player_id: {"report_status", "practice_status"}} from Sleeper's live feed.

    Args:
        players: pool entries with "playerId", "name" and optionally "espnId" --
            Sleeper keys by ESPN id and by lowercase full name, so the pool has
            to supply the join key; this module has no player-name index of its
            own.

    Note what this source does NOT carry: practice participation. Sleeper
    publishes a designation only, so every returned row leaves practice_status
    None and the model reads it as the no-practice-data state. That costs real
    signal -- practice splits Questionable from 0.51 (DNP) to 0.79 (full) -- so
    prefer the nflreadpy report whenever the season is published, and treat this
    as the pre-season/current-week fallback it is.

    Degrades to {} on any failure, which leaves availability running on
    play-rate history alone rather than failing the report.
    """
    try:
        from news import fetch_sleeper_injury_status

        sleeper = fetch_sleeper_injury_status()
    except Exception:
        return {}
    if not sleeper:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for player in players:
        espn_id = player.get("espnId")
        entry = sleeper.get(str(espn_id) if espn_id else "") or sleeper.get(
            _text(player.get("name")).lower()
        )
        if not entry:
            continue
        status = _SLEEPER_TO_REPORT_STATUS.get(entry.get("designation"))
        if status:
            out[player["playerId"]] = {"report_status": status, "practice_status": None}
    return out


def load_injury_report_espn(
    players: list[dict[str, Any]],
    rows: Optional[list[dict[str, Any]]] = None,
) -> dict[str, dict[str, Any]]:
    """{player_id: {"report_status", "practice_status"}} from ESPN's injury page.

    `rows` lets a caller that already fetched the ~9 MB payload (for the news
    layer) pass it in rather than fetching twice.

    Like Sleeper, this carries no practice participation. Unlike Sleeper it
    covers roughly three times as many players and every entry is dated.
    Players ESPN lists as "Active" produce NO designation -- they are on the
    page because there is news about them, not because they are doubtful.
    """
    try:
        from espn_injuries import fetch_injury_rows, index_by_player

        rows = fetch_injury_rows() if rows is None else rows
        if not rows:
            return {}
        matched = index_by_player(rows, players)
    except Exception:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for player_id, row in matched.items():
        status = row.get("report_status")
        if status:
            out[player_id] = {"report_status": status, "practice_status": None}
    return out


def load_current_injury_report(
    season: int,
    week: int,
    players: list[dict[str, Any]],
    espn_rows: Optional[list[dict[str, Any]]] = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """The best available injury report for (season, week), and where it came from.

    Precedence, and the reason for each:

      1. nflreadpy  -- the only source with PRACTICE PARTICIPATION, which is
         what separates a Questionable who practised fully (P=0.79) from one
         who did not practise at all (0.51). Worth more than coverage.
      2. ESPN       -- ~449 of a 904-player pool, every entry dated. The best
         live source once nflreadpy has run out of published seasons.
      3. Sleeper    -- ~167 of the same pool. Kept as a third source because it
         is keyed differently and catches players the ESPN name/id join misses.

    ESPN and Sleeper are MERGED rather than either winning outright: they are
    reporting the same underlying official report through different pipelines,
    so a player present in only one is a coverage gap, not a disagreement.
    Where both have an opinion, ESPN's wins -- its entries carry a date, so a
    stale row is at least visible.

    Returns (report, source) so a run says which combination it actually used.
    The three are not equivalent and should not silently look the same.
    """
    report = load_injury_report_nflreadpy(season, week)
    if report:
        return report, "nflreadpy"

    espn = load_injury_report_espn(players, rows=espn_rows)
    sleeper = load_injury_report_sleeper(players)
    if espn and sleeper:
        merged = {**sleeper, **espn}  # ESPN wins on overlap
        return merged, f"espn+sleeper ({len(espn)}+{len(sleeper)} before merge)"
    if espn:
        return espn, "espn"
    return sleeper, "sleeper"


def build_training_rows_nflreadpy(
    seasons: list[int], positions: tuple[str, ...] = ("QB", "RB", "WR", "TE")
) -> list[dict[str, Any]]:
    """Labelled player-weeks for fitting: every week inside a player's active
    span where their team played, labelled with whether they recorded a game.

    The active span (first to last game of that season) is what makes the label
    meaningful. Without it, a player signed in week 10 would count as nine
    "missed" games and a season-ending injury in week 6 as eleven more, and the
    model would be fitting roster churn rather than availability.
    """
    import nflreadpy as nfl
    import pandas as pd

    stats = nfl.load_player_stats(seasons=seasons)
    stats = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
    stats = stats[(stats["season_type"] == "REG") & (stats["position"].isin(positions))]

    schedules = nfl.load_schedules()
    schedules = schedules.to_pandas() if hasattr(schedules, "to_pandas") else schedules
    schedules = schedules[
        (schedules["game_type"] == "REG") & (schedules["season"].isin(seasons))
    ]
    team_weeks = pd.concat(
        [
            schedules[["season", "week", "home_team"]].rename(columns={"home_team": "team"}),
            schedules[["season", "week", "away_team"]].rename(columns={"away_team": "team"}),
        ]
    )

    span = (
        stats.groupby(["player_id", "season"])
        .agg(
            first_week=("week", "min"),
            last_week=("week", "max"),
            team=("team", lambda s: s.mode().iat[0]),
            position=("position", "first"),
        )
        .reset_index()
    )
    grid = span.merge(team_weeks, on=["season", "team"])
    grid = grid[(grid["week"] >= grid["first_week"]) & (grid["week"] <= grid["last_week"])]
    grid = grid.merge(
        stats[["player_id", "season", "week"]].assign(played=1),
        on=["player_id", "season", "week"],
        how="left",
    )
    grid["played"] = grid["played"].fillna(0).astype(int)

    injuries = nfl.load_injuries(seasons=seasons)
    injuries = injuries.to_pandas() if hasattr(injuries, "to_pandas") else injuries
    injuries = injuries[["season", "week", "gsis_id", "report_status", "practice_status"]].rename(
        columns={"gsis_id": "player_id"}
    )
    injuries["season"] = injuries["season"].astype(int)
    injuries["week"] = injuries["week"].astype(int)
    injuries = injuries.drop_duplicates(subset=["season", "week", "player_id"])
    grid = grid.merge(injuries, on=["season", "week", "player_id"], how="left")

    # Depth-chart rank, as-of each week. Handled by depth_charts.py because
    # nflverse changed the feed's shape in 2025 and both schemas are in range.
    depth_ranks: dict = {}
    try:
        from depth_charts import load_depth_ranks

        depth_ranks = load_depth_ranks([int(s) for s in seasons])
    except Exception:
        depth_ranks = {}

    grid = grid.sort_values(["player_id", "season", "week"])
    grouped = grid.groupby(["player_id", "season"])["played"]
    grid["prior_play_rate"] = grouped.transform(lambda s: s.shift(1).expanding().mean())
    grid["prior_games_observed"] = grouped.transform(lambda s: s.shift(1).expanding().count())

    rows = []
    for record in grid.to_dict("records"):
        rows.append(
            {
                "player_id": record["player_id"],
                "season": int(record["season"]),
                "week": int(record["week"]),
                "position": record["position"],
                "played": int(record["played"]),
                "report_status": _text(record.get("report_status")) or None,
                "practice_status": _text(record.get("practice_status")) or None,
                "prior_play_rate": num(record.get("prior_play_rate")),
                "prior_games_observed": num(record.get("prior_games_observed")) or 0.0,
                "depth_rank": depth_ranks.get(
                    (int(record["season"]), int(record["week"]), record["player_id"])
                ),
            }
        )
    return rows
