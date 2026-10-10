"""Week-1 cold-start projection model for QB/RB/WR/TE. See ARCHITECTURE.md §5."""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional

FEATURES: tuple[str, ...] = (
    "prior_ppg",
    "prior_last8_ppg",
    "prior_ppg_sd",
    "prior2_ppg",
    "prior_targets",
    "prior_carries",
    "prior_attempts",
    "prior_receptions",
    "prior_target_share",
    "prior_wopr",
    "prior_rush_yards",
    "prior_rec_yards",
    "prior_pass_yards",
    "prior_games",
    "prior_availability",
    "prior2_games",
    "draft_pick",
    "draft_round",
    "is_rookie",
    "age",
    "years_exp",
    "depth_team",
    "team_change",
    "implied_team_total",
    "total_line",
    "spread_line",
    "is_home",
    "dvp_prior",
)

DEFAULT_RIDGE_ALPHA = 120.0

UNDRAFTED_PICK = 300.0
UNDRAFTED_ROUND = 8.0

Z_CLIP = 4.0


class Week1Model:
    """Per-position ridge on FEATURES with imputation and standardisation."""

    def __init__(self, alpha: float = DEFAULT_RIDGE_ALPHA, features: Iterable[str] = FEATURES):
        self.alpha = alpha
        self.features = tuple(features)
        self._fits: dict[str, dict[str, Any]] = {}

    def fit(self, rows: list[dict[str, Any]], min_rows: int = 40) -> "Week1Model":
        """Fit one ridge per position."""
        by_pos: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            position = row.get("position")
            if position is None or row.get("actual_points") is None:
                continue
            by_pos.setdefault(position, []).append(row)

        for position, pos_rows in by_pos.items():
            medians = {
                feature: _median([_num(r.get(feature)) for r in pos_rows if _num(r.get(feature)) is not None])
                for feature in self.features
            }
            design = [self._row_vector(r, medians) for r in pos_rows]
            targets = [float(r["actual_points"]) for r in pos_rows]
            mean_target = sum(targets) / len(targets)
            if len(pos_rows) < min_rows:
                self._fits[position] = {"beta": None, "fallback": mean_target}
                continue
            mean, std = _column_stats(design)
            standardized = [_standardize(v, mean, std) for v in design]
            beta = _ridge(standardized, targets, self.alpha)
            self._fits[position] = {
                "beta": beta,
                "mean": mean,
                "std": std,
                "medians": medians,
                "fallback": mean_target,
                "ceiling": max(targets),
            }
        return self

    def predict_one(self, row: dict[str, Any]) -> dict[str, Any]:
        """Project one player's week-1 points."""
        position = row.get("position")
        fit = self._fits.get(position)
        season = row.get("season")
        if fit is None:
            return self._output(row, 0.0, "no_model", 0.0)
        if fit["beta"] is None:
            return self._output(row, fit["fallback"], "positional_fallback", 0.0)

        vector = self._row_vector(row, fit["medians"])
        standardized = _standardize(vector, fit["mean"], fit["std"], clip=Z_CLIP)
        raw = fit["beta"][0] + sum(b * x for b, x in zip(fit["beta"][1:], standardized))
        points = min(max(raw, 0.0), fit["ceiling"])

        observed = sum(1 for f in self.features if _num(row.get(f)) is not None)
        coverage = observed / len(self.features)
        confidence = "full" if coverage >= 0.75 else ("low" if coverage >= 0.3 else "thin")
        return self._output(row, points, confidence, coverage)

    def predict(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Batch predict_one, keyed by player_id."""
        return {r["player_id"]: self.predict_one(r) for r in rows}

    def _row_vector(self, row: dict[str, Any], medians: dict[str, Optional[float]]) -> list[float]:
        vector: list[float] = []
        flags: list[float] = []
        for feature in self.features:
            value = _num(row.get(feature))
            flags.append(0.0 if value is not None else 1.0)
            if value is None:
                value = medians.get(feature)
                value = 0.0 if value is None else value
            vector.append(value)
        return vector + flags

    def _output(self, row: dict[str, Any], points: float, confidence: str, coverage: float) -> dict[str, Any]:
        return {
            "source": "week1_model",
            "season": row.get("season"),
            "week": 1,
            "position": row.get("position"),
            "projected_points": round(float(points), 2),
            "confidence": confidence,
            "feature_coverage": round(coverage, 3),
            "games_used": 0,
        }


def _num(value: Any) -> Optional[float]:
    """None for anything that isn't a real, finite number -- NaN included."""
    if value is None or isinstance(value, bool):
        return float(value) if isinstance(value, bool) else None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _column_stats(matrix: list[list[float]]) -> tuple[list[float], list[float]]:
    n = len(matrix)
    width = len(matrix[0])
    mean = [sum(row[j] for row in matrix) / n for j in range(width)]
    std = []
    for j in range(width):
        var = sum((row[j] - mean[j]) ** 2 for row in matrix) / max(n - 1, 1)
        std.append(math.sqrt(var) if var > 1e-12 else 1.0)
    return mean, std


def _standardize(
    vector: list[float], mean: list[float], std: list[float], clip: Optional[float] = None
) -> list[float]:
    z = [(v - m) / s for v, m, s in zip(vector, mean, std)]
    if clip is None:
        return z
    return [max(-clip, min(clip, v)) for v in z]


def _ridge(design: list[list[float]], targets: list[float], alpha: float) -> list[float]:
    """Closed-form ridge with an unpenalised intercept, as [intercept, *coefficients]."""
    n = len(design)
    width = len(design[0])
    x = [[1.0] + row for row in design]
    size = width + 1

    xtx = [[sum(x[i][a] * x[i][b] for i in range(n)) for b in range(size)] for a in range(size)]
    xty = [sum(x[i][a] * targets[i] for i in range(n)) for a in range(size)]
    for j in range(1, size):
        xtx[j][j] += alpha

    return _solve(xtx, xty)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            continue
        m[col], m[pivot] = m[pivot], m[col]
        pv = m[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col] / pv
            if factor:
                for c in range(col, n + 1):
                    m[r][c] -= factor * m[col][c]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def build_feature_rows(
    season: int,
    prior_season_games: list[dict[str, Any]],
    week1_context: dict[str, dict[str, Any]],
    player_meta: dict[str, dict[str, Any]],
    prior_season_dvp: Optional[dict[tuple[str, str], float]] = None,
) -> list[dict[str, Any]]:
    """Assemble one feature row per player for `season`'s week 1."""
    dvp = prior_season_dvp or {}
    by_player: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for game in prior_season_games:
        by_player.setdefault(game["player_id"], {}).setdefault(int(game["season"]), []).append(game)

    team_games: dict[tuple[str, int], set] = {}
    for game in prior_season_games:
        team = game.get("team")
        if team:
            team_games.setdefault((team, int(game["season"])), set()).add(int(game["week"]))

    rows = []
    for player_id, meta in player_meta.items():
        position = meta.get("position")
        team = meta.get("team")
        if position is None or team is None:
            continue
        context = week1_context.get(team, {})
        seasons = by_player.get(player_id, {})
        prior = seasons.get(season - 1, [])
        prior2 = seasons.get(season - 2, [])

        row: dict[str, Any] = {
            "player_id": player_id,
            "season": season,
            "position": position,
            "team": team,
            "opponent": context.get("opponent"),
            "draft_pick": meta.get("draft_pick") if meta.get("draft_pick") is not None else UNDRAFTED_PICK,
            "draft_round": meta.get("draft_round") if meta.get("draft_round") is not None else UNDRAFTED_ROUND,
            "is_rookie": 1.0 if meta.get("rookie_year") == season else 0.0,
            "age": meta.get("age"),
            "years_exp": meta.get("years_exp"),
            "depth_team": meta.get("depth_team"),
            "is_home": context.get("is_home"),
            "implied_team_total": context.get("implied_team_total"),
            "total_line": context.get("total_line"),
            "spread_line": context.get("spread_line"),
            "dvp_prior": dvp.get((context.get("opponent"), position), 1.0),
        }
        row.update(_prior_season_features(prior, team, season - 1, team_games, prefix="prior"))
        row["prior2_ppg"] = _mean([g["points"] for g in prior2]) if prior2 else None
        row["prior2_games"] = float(len(prior2)) if prior2 else None
        prior_team = prior[-1].get("team") if prior else None
        row["team_change"] = 1.0 if (prior_team and prior_team != team) else 0.0
        rows.append(row)
    return rows


def _prior_season_features(
    games: list[dict[str, Any]],
    team: str,
    prior_season: int,
    team_games: dict[tuple[str, int], set],
    prefix: str,
) -> dict[str, Any]:
    if not games:
        return {
            f"{prefix}_{name}": None
            for name in (
                "ppg", "last8_ppg", "ppg_sd", "games", "availability", "targets", "carries",
                "attempts", "receptions", "target_share", "wopr", "rush_yards", "rec_yards",
                "pass_yards",
            )
        }
    ordered = sorted(games, key=lambda g: int(g["week"]))
    points = [float(g["points"]) for g in ordered]
    prior_team = ordered[-1].get("team") or team
    played = len(ordered)
    scheduled = len(team_games.get((prior_team, prior_season), set())) or 17
    return {
        f"{prefix}_ppg": _mean(points),
        f"{prefix}_last8_ppg": _mean(points[-8:]),
        f"{prefix}_ppg_sd": _stdev(points),
        f"{prefix}_games": float(played),
        f"{prefix}_availability": min(played / scheduled, 1.0),
        f"{prefix}_targets": _mean([_num(g.get("targets")) or 0.0 for g in ordered]),
        f"{prefix}_carries": _mean([_num(g.get("carries")) or 0.0 for g in ordered]),
        f"{prefix}_attempts": _mean([_num(g.get("attempts")) or 0.0 for g in ordered]),
        f"{prefix}_receptions": _mean([_num(g.get("receptions")) or 0.0 for g in ordered]),
        f"{prefix}_target_share": _mean([_num(g.get("target_share")) or 0.0 for g in ordered]),
        f"{prefix}_wopr": _mean([_num(g.get("wopr")) or 0.0 for g in ordered]),
        f"{prefix}_rush_yards": _mean([_num(g.get("rushing_yards")) or 0.0 for g in ordered]),
        f"{prefix}_rec_yards": _mean([_num(g.get("receiving_yards")) or 0.0 for g in ordered]),
        f"{prefix}_pass_yards": _mean([_num(g.get("passing_yards")) or 0.0 for g in ordered]),
    }


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _stdev(values: list[float]) -> Optional[float]:
    if len(values) < 2:
        return 0.0
    mu = sum(values) / len(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / (len(values) - 1))


def fit_interval_model(
    training_rows: list[dict[str, Any]],
    alpha: float = DEFAULT_RIDGE_ALPHA,
    min_seasons: int = 1,
    min_rows: int = 60,
    buckets: int = 3,
):
    """An IntervalModel fitted on walk-forward held-out week-1 residuals."""
    from calibration import IntervalModel

    seasons = sorted({r["season"] for r in training_rows if r.get("season") is not None})
    if len(seasons) <= min_seasons:
        return None

    residual_rows = []
    for index, season in enumerate(seasons):
        if index < min_seasons:
            continue
        train = [r for r in training_rows if r["season"] < season]
        test = [r for r in training_rows if r["season"] == season]
        if not train or not test:
            continue
        model = Week1Model(alpha=alpha).fit(train)
        for row in test:
            residual_rows.append(
                {
                    "position": row.get("position"),
                    "projected": model.predict_one(row)["projected_points"],
                    "actual": row["actual_points"],
                }
            )
    if not residual_rows:
        return None
    return IntervalModel(min_rows=min_rows, buckets=buckets).fit(residual_rows)


def training_rows_from_history(
    seasons: list[int],
    all_games: list[dict[str, Any]],
    week1_context_by_season: dict[int, dict[str, dict[str, Any]]],
    player_meta_by_season: dict[int, dict[str, dict[str, Any]]],
    dvp_by_season: Optional[dict[int, dict[tuple[str, str], float]]] = None,
) -> list[dict[str, Any]]:
    """Labelled week-1 training rows (features + actual_points) for past seasons."""
    dvp_by_season = dvp_by_season or {}
    week1_actuals = {
        (g["player_id"], int(g["season"])): float(g["points"])
        for g in all_games
        if int(g["week"]) == 1
    }
    rows = []
    for season in seasons:
        prior_games = [g for g in all_games if int(g["season"]) in (season - 1, season - 2)]
        feature_rows = build_feature_rows(
            season,
            prior_games,
            week1_context_by_season.get(season, {}),
            player_meta_by_season.get(season, {}),
            dvp_by_season.get(season),
        )
        for row in feature_rows:
            actual = week1_actuals.get((row["player_id"], season))
            if actual is None:
                continue
            rows.append({**row, "actual_points": actual})
    return rows


STAT_PASSTHROUGH = (
    "targets", "carries", "attempts", "receptions", "target_share", "wopr",
    "rushing_yards", "receiving_yards", "passing_yards",
)


def load_history_nflreadpy(seasons: list[int], scoring_config: dict[str, Any], positions=("QB", "RB", "WR", "TE")):
    """Every regular-season player-game across `seasons`, scored by league config."""
    import nflreadpy as nfl

    from scoring import compute_league_points, nflreadpy_row_to_stat_line

    frame = nfl.load_player_stats(seasons=list(seasons))
    frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
    frame = frame[(frame["season_type"] == "REG") & (frame["position"].isin(positions))]

    games = []
    for record in frame.to_dict("records"):
        stat_line = nflreadpy_row_to_stat_line(record)
        game = {
            "player_id": record["player_id"],
            "season": int(record["season"]),
            "week": int(record["week"]),
            "position": record["position"],
            "team": record["team"],
            "opponent_team": record.get("opponent_team"),
            "points": compute_league_points(stat_line, scoring_config).total,
        }
        for column in STAT_PASSTHROUGH:
            game[column] = record.get(column)
        games.append(game)
    return games


def load_week1_context_nflreadpy(season: int) -> dict[str, dict[str, Any]]:
    """Week-1 Vegas game context per team."""
    import nflreadpy as nfl

    frame = nfl.load_schedules()
    frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
    frame = frame[(frame["season"] == season) & (frame["week"] == 1) & (frame["game_type"] == "REG")]

    context: dict[str, dict[str, Any]] = {}
    for game in frame.to_dict("records"):
        total, spread = game.get("total_line"), game.get("spread_line")
        home_implied = (total + spread) / 2 if _num(total) is not None and _num(spread) is not None else None
        away_implied = (total - spread) / 2 if home_implied is not None else None
        context[game["home_team"]] = {
            "opponent": game["away_team"], "is_home": 1.0, "implied_team_total": home_implied,
            "total_line": total, "spread_line": spread,
        }
        context[game["away_team"]] = {
            "opponent": game["home_team"], "is_home": 0.0, "implied_team_total": away_implied,
            "total_line": total, "spread_line": -spread if _num(spread) is not None else None,
        }
    return context


def load_player_meta_nflreadpy(season: int, positions=("QB", "RB", "WR", "TE")) -> dict[str, dict[str, Any]]:
    """Week-1 roster snapshot: team, age, experience, draft, depth chart."""
    import nflreadpy as nfl
    import pandas as pd

    rosters = nfl.load_rosters(seasons=[season])
    rosters = rosters.to_pandas() if hasattr(rosters, "to_pandas") else rosters
    rosters = rosters[rosters["position"].isin(positions)].drop_duplicates(subset=["gsis_id"])

    depth = _load_depth_ranks(nfl, pd, season)

    picks = nfl.load_draft_picks()
    picks = picks.to_pandas() if hasattr(picks, "to_pandas") else picks
    picks = picks.dropna(subset=["gsis_id"]).drop_duplicates(subset=["gsis_id"])
    draft = {r["gsis_id"]: (r.get("pick"), r.get("round")) for r in picks.to_dict("records")}

    meta: dict[str, dict[str, Any]] = {}
    for record in rosters.to_dict("records"):
        player_id = record.get("gsis_id")
        if not player_id:
            continue
        birth_year = pd.to_datetime(record.get("birth_date"), errors="coerce")
        pick, rnd = draft.get(player_id, (None, None))
        meta[player_id] = {
            "position": record["position"],
            "team": record["team"],
            "age": (season - birth_year.year) if pd.notna(birth_year) else None,
            "years_exp": _num(record.get("years_exp")),
            "draft_pick": _num(pick),
            "draft_round": _num(rnd),
            "rookie_year": _num(record.get("rookie_year")),
            "depth_team": _num(depth.get(player_id)),
        }
    return meta


def prior_season_dvp(all_games: list[dict[str, Any]], season: int) -> dict[tuple[str, str], float]:
    """Defence-vs-position rate vs league average, regressed 50% toward 1.0."""
    by_key: dict[tuple[str, str, int], float] = {}
    for game in all_games:
        if int(game["season"]) != season or not game.get("opponent_team"):
            continue
        key = (game["opponent_team"], game["position"], int(game["week"]))
        by_key[key] = by_key.get(key, 0.0) + float(game["points"])

    per_team: dict[tuple[str, str], list[float]] = {}
    for (team, position, _week), points in by_key.items():
        per_team.setdefault((team, position), []).append(points)

    league: dict[str, list[float]] = {}
    for (_team, position), values in per_team.items():
        league.setdefault(position, []).extend(values)
    league_mean = {p: (sum(v) / len(v)) for p, v in league.items() if v}

    out: dict[tuple[str, str], float] = {}
    for (team, position), values in per_team.items():
        mean = league_mean.get(position)
        if not mean:
            continue
        out[(team, position)] = 0.5 * (sum(values) / len(values)) / mean + 0.5
    return out


def _load_depth_ranks(nfl, pd, season: int) -> dict[str, float]:
    """Week-1 depth rank per player, across both nflverse schemas; {} on failure."""
    try:
        charts = nfl.load_depth_charts(seasons=[season])
        charts = charts.to_pandas() if hasattr(charts, "to_pandas") else charts
    except Exception:
        return {}
    if charts is None or charts.empty:
        return {}

    if "depth_team" in charts.columns and "week" in charts.columns:
        charts = charts[(charts["week"] == 1) & (charts["game_type"] == "REG")]
        ranks = pd.to_numeric(charts["depth_team"], errors="coerce")
        return charts.assign(_r=ranks).groupby("gsis_id")["_r"].min().dropna().to_dict()

    if "pos_rank" in charts.columns and "dt" in charts.columns:
        stamps = pd.to_datetime(charts["dt"], errors="coerce", utc=True)
        kickoff = _week1_kickoff(nfl, pd, season)
        charts = charts.assign(_dt=stamps)
        if kickoff is not None:
            charts = charts[charts["_dt"] <= kickoff]
        if charts.empty:
            return {}
        latest = charts["_dt"].max()
        charts = charts[charts["_dt"] == latest]
        from depth_charts import slot_ranks

        ranks = slot_ranks(charts.drop(columns=["_dt"]).to_dict("records"))
        ranks = pd.to_numeric(pd.Series(ranks, index=charts.index), errors="coerce")
        return charts.assign(_r=ranks).groupby("gsis_id")["_r"].min().dropna().to_dict()

    return {}


def _week1_kickoff(nfl, pd, season: int):
    """Kickoff of the season's first regular-season game."""
    try:
        frame = nfl.load_schedules()
        frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
        frame = frame[(frame["season"] == season) & (frame["week"] == 1) & (frame["game_type"] == "REG")]
        return pd.to_datetime(frame["gameday"], errors="coerce", utc=True).min()
    except Exception:
        return None
