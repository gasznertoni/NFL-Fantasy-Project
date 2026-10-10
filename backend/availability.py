"""Availability model: P(this player takes the field this week). See ARCHITECTURE.md §6."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from ridge import logistic, num, predict_probability

REPORT_STATUSES = ("Out", "Doubtful", "Questionable")
PRACTICE_STATUSES = (
    "Did Not Participate In Practice",
    "Limited Participation in Practice",
    "Full Participation in Practice",
)

LEAGUE_PLAY_RATE = 0.79
PLAY_RATE_PRIOR_GAMES = 4.0

DEFAULT_L2 = 1.0

DEPTH_RANKS = (1, 2, 3)

FALLBACK_PLAY_RATE = LEAGUE_PLAY_RATE

# K is deliberately absent: kickers can be inactive, and are in training.
ALWAYS_AVAILABLE_POSITIONS = ("DST",)


def _bucket_rank(value: Any) -> Optional[int]:
    """Depth rank as 1/2/3, or None when unknown."""
    try:
        from depth_charts import bucket_rank

        return bucket_rank(num(value))
    except Exception:
        return None


def _text(value: Any) -> str:
    """A designation as a clean string, or "" for absent (NaN-safe)."""
    if not isinstance(value, str):
        return ""
    return value.strip()


class AvailabilityModel:
    """L2 logistic over injury report, practice, shrunk play rate, position and depth."""

    def __init__(self, l2: float = DEFAULT_L2):
        self.l2 = l2
        self.beta: Optional[list[float]] = None
        self.positions: tuple[str, ...] = ()
        self.depth_positions: tuple[str, ...] = ()

    def _row(self, row: dict[str, Any]) -> list[float]:
        report = _text(row.get("report_status"))
        practice = _text(row.get("practice_status"))
        features = [1.0 if report == s else 0.0 for s in REPORT_STATUSES]
        features += [1.0 if practice == s else 0.0 for s in PRACTICE_STATUSES]

        rate = num(row.get("prior_play_rate"))
        games = num(row.get("prior_games_observed")) or 0.0
        rate = LEAGUE_PLAY_RATE if rate is None else rate
        shrunk = (games * rate + PLAY_RATE_PRIOR_GAMES * LEAGUE_PLAY_RATE) / (
            games + PLAY_RATE_PRIOR_GAMES
        )
        features.append(shrunk)
        features.append(min(games, 17.0) / 17.0)

        position = row.get("position")
        features += [1.0 if position == p else 0.0 for p in self.positions]

        depth = _bucket_rank(row.get("depth_rank"))
        for pos in self.depth_positions:
            for rank in DEPTH_RANKS:
                features.append(1.0 if (depth == rank and position == pos) else 0.0)
        features.append(1.0 if depth is None else 0.0)
        return features

    def fit(self, rows: list[dict[str, Any]], min_rows: int = 200) -> "AvailabilityModel":
        """Fit on labelled player-weeks."""
        labelled = [r for r in rows if num(r.get("played")) is not None]
        if len(labelled) < min_rows:
            self.beta = None
            return self

        seen = sorted({r.get("position") for r in labelled if r.get("position")})
        self.positions = tuple(seen[:-1]) if len(seen) > 1 else ()
        self.depth_positions = tuple(seen)

        design = [self._row(r) for r in labelled]
        targets = [float(num(r["played"])) for r in labelled]
        self.beta = logistic(design, targets, l2=self.l2)
        return self

    def predict_one(self, row: dict[str, Any]) -> float:
        """P(play) in [0, 1] for one player-week."""
        if self.beta is None:
            features = self._row(row)
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
    """Each player's play rate over the weeks strictly before `as_of_week`."""
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
    """E[points] = P(play) x E[points | play]; the miss branch is exactly zero."""
    p = min(max(play_probability, 0.0), 1.0)
    return conditional_points * p


def load_injury_report_nflreadpy(season: int, week: int) -> dict[str, dict[str, Any]]:
    """{player_id: {report_status, practice_status}} for one week, from nflreadpy."""
    import nflreadpy as nfl

    try:
        frame = nfl.load_injuries(seasons=[season])
        frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
        frame = frame[(frame["season"] == season) & (frame["week"] == week)]
    except Exception:
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


_SLEEPER_TO_REPORT_STATUS = {
    "Out": "Out",
    "IR": "Out",
    "Doubtful": "Doubtful",
    "Questionable": "Questionable",
}


def load_injury_report_sleeper(
    players: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """{player_id: {report_status, practice_status}} from Sleeper's live feed."""
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
    """{player_id: {report_status, practice_status}} from ESPN's injury page."""
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
    """(report, source): the best available injury report for (season, week)."""
    return merge_injury_reports(
        load_injury_report_nflreadpy(season, week),
        load_injury_report_espn(players, rows=espn_rows),
        load_injury_report_sleeper(players),
    )


def merge_injury_reports(
    nflreadpy_report: dict[str, dict[str, Any]],
    espn: dict[str, dict[str, Any]],
    sleeper: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str]:
    """Combine the three reports per PLAYER, never per source."""
    live = {**sleeper, **espn}  # ESPN wins on overlap
    merged = {player_id: dict(row) for player_id, row in live.items()}
    for player_id, row in nflreadpy_report.items():
        entry = merged.setdefault(player_id, {"report_status": None, "practice_status": None})
        entry["practice_status"] = row.get("practice_status")
        if row.get("report_status") is not None:
            entry["report_status"] = row["report_status"]

    parts = [
        f"{name} {len(source)}"
        for name, source in (("nflreadpy", nflreadpy_report), ("espn", espn), ("sleeper", sleeper))
        if source
    ]
    return merged, "+".join(parts) if parts else "none"


def build_training_rows_nflreadpy(
    seasons: list[int], positions: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K")
) -> list[dict[str, Any]]:
    """Labelled player-weeks inside each player's active span where his team played."""
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
