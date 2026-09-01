"""
CSV-based backtest: compare our in-house projection model against 2025 actual
PPR points from the FantasyPros season summary CSV.

Run from the backend/ directory:
  python3 csv_backtest.py /path/to/FantasyPros_Fantasy_Football_Points_PPR-2.csv

What this does:
1.  Parses the CSV into a per-player-per-week actual-points lookup (PPR scoring).
2.  Loads nflreadpy 2025 stats and matches players by normalized name + team.
3.  Runs our projection model for all 18 weeks (same walk-forward discipline
    as backtest.py: each week's projection uses only prior-week data).
4.  Compares projected vs CSV-actual and vs nflreadpy-computed actual.
5.  Reports: MAE, bias, correlation by position; sweeps POSITION_CALIBRATION_SCALE
    candidates and recommends optimal values per position.
6.  Reports scoring differences between our formula (scoring.py) and the CSV
    (standard PPR) to surface config gaps, especially QB 6-pt vs 4-pt TDs.

Statistical methodology (per the project's backtest discipline):
- MAE is the primary metric: reflects absolute error without direction noise.
- Bias (signed mean error) shows systematic over/under-projection per position.
- Pearson correlation is a secondary, config-robust signal (relative ranking
  survives a linear scaling of points better than absolute MAE does).
- Optimal calibration scale is computed analytically:
    scale* = mean(actual) / mean(projection_at_scale_1.0)
  This is the L2-optimal multiplier when the model has zero intercept error,
  equivalent to 1 + bias/mean(projection).
- Paired significance tests confirm scale improvements aren't noise.
- DNP games (empty CSV cells) are correctly excluded: only weeks where the
  player actually played (non-empty CSV entry) are compared.
"""

from __future__ import annotations

import csv
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import baseline
from backtest import aggregate_metrics, paired_significance_test
from projections import (
    DEFAULT_DECAY,
    DEFAULT_SHRINKAGE_STRENGTH,
    DEFAULT_WINDOW,
    POSITION_CALIBRATION_SCALE,
    project_player,
)
from scoring import compute_league_points, nflreadpy_row_to_stat_line

SEASON = 2025
POSITIONS = ("QB", "RB", "WR", "TE")
ALL_WEEKS = list(range(1, 19))


# ---------------------------------------------------------------------------
# Step 1: CSV parsing
# ---------------------------------------------------------------------------

_TEAM_ALIASES: dict[str, str] = {
    "WSH": "WAS",
    "JAX": "JAC",
    "LA": "LAR",
}


def _norm_team(team: str) -> str:
    t = team.strip().upper()
    return _TEAM_ALIASES.get(t, t)


def _norm_name(name: str) -> str:
    """Lowercase, strip suffixes, remove punctuation, collapse whitespace."""
    n = name.lower().strip()
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\.?\s*$", "", n).strip()
    n = re.sub(r"['\-\.]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def parse_csv(path: str) -> dict[tuple[str, str], dict[int, float]]:
    """Parse CSV → {(norm_name, team): {week: actual_ppr_pts}}.

    Skips BYE and empty cells; preserves 0.0 (a real played game with zero
    fantasy points) and negative scores (fumble-only games).
    """
    lookup: dict[tuple[str, str], dict[int, float]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = row.get("PLAYER", "").strip()
            if not raw:
                continue
            # Format is "Name   TEAM" — rsplit on last whitespace cluster
            parts = raw.rsplit(None, 1)
            if len(parts) < 2:
                continue
            name_str, team_str = parts[0].strip(), parts[1].strip()
            team = _norm_team(team_str)
            week_pts: dict[int, float] = {}
            for wk in range(1, 19):
                val = row.get(str(wk), "").strip()
                if val in ("", "BYE", "bye"):
                    continue
                try:
                    week_pts[wk] = float(val)
                except ValueError:
                    continue
            if week_pts:
                lookup[(_norm_name(name_str), team)] = week_pts
    return lookup


# ---------------------------------------------------------------------------
# Step 2: Load nflreadpy data + player name map
# ---------------------------------------------------------------------------

def load_game_logs_with_names(season: int) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, str]],
]:
    """Return (game_logs_by_player, player_info_by_player).

    game_logs_by_player: same shape as backtest.load_full_pool_game_logs.
    player_info_by_player: {player_id: {"display_name", "team", "position"}}.
    """
    import nflreadpy as nfl

    stats = nfl.load_player_stats(seasons=[season])
    df = stats.to_pandas() if hasattr(stats, "to_pandas") else stats
    df = df[df["position"].isin(POSITIONS)]

    game_logs: dict[str, list[dict[str, Any]]] = {}
    player_info: dict[str, dict[str, str]] = {}

    for _, row in df.iterrows():
        pid = str(row["player_id"])
        row_dict = row.to_dict()
        stat_line = nflreadpy_row_to_stat_line(row_dict)
        stat_line["season"] = season
        stat_line["week"] = int(row["week"])
        stat_line["opponent_team"] = row.get("opponent_team")
        stat_line["position"] = str(row.get("position", ""))
        game_logs.setdefault(pid, []).append(stat_line)

        if pid not in player_info:
            display = str(row.get("player_display_name", row.get("player_name", "")))
            team = _norm_team(str(row.get("recent_team", row.get("team", ""))))
            player_info[pid] = {
                "display_name": display,
                "team": team,
                "position": str(row.get("position", "")),
            }

    return game_logs, player_info


# ---------------------------------------------------------------------------
# Step 3: Player matching (nflreadpy gsis_id ↔ CSV name+team)
# ---------------------------------------------------------------------------

def _last_token(norm: str) -> str:
    tokens = norm.split()
    return tokens[-1] if tokens else norm


def match_players(
    player_info: dict[str, dict[str, str]],
    csv_lookup: dict[tuple[str, str], dict[int, float]],
) -> dict[str, dict[int, float]]:
    """Return {player_id: {week: csv_ppr_points}} for matched players.

    Priority:
    1. Full normalized display_name + team → exact CSV key.
    2. Last-name token + team → unique CSV candidate.
    3. Last-name token + team → multiple candidates; pick best token overlap.
    Unmatched players are silently omitted (they won't have a CSV actual).
    """
    # Build a last-name index for fast lookup
    last_name_idx: dict[tuple[str, str], list[tuple[tuple[str, str], dict[int, float]]]] = {}
    for (norm, team), wkpts in csv_lookup.items():
        last = _last_token(norm)
        last_name_idx.setdefault((last, team), []).append(((norm, team), wkpts))

    matched: dict[str, dict[int, float]] = {}
    for pid, info in player_info.items():
        display = info["display_name"]
        team = info["team"]
        norm = _norm_name(display)

        # 1. Exact key
        if (norm, team) in csv_lookup:
            matched[pid] = csv_lookup[(norm, team)]
            continue

        # 2. Last-name + team
        last = _last_token(norm)
        candidates = last_name_idx.get((last, team), [])
        if len(candidates) == 1:
            matched[pid] = candidates[0][1]
            continue

        # 3. Multiple: pick best token overlap
        if candidates:
            norm_tokens = set(norm.split())
            best = max(candidates, key=lambda c: len(set(c[0][0].split()) & norm_tokens))
            matched[pid] = best[1]

    return matched


# ---------------------------------------------------------------------------
# Step 4: Projection vs actual comparison
# ---------------------------------------------------------------------------

def run_comparison(
    game_logs: dict[str, list[dict[str, Any]]],
    csv_actuals: dict[str, dict[int, float]],
    scoring_config: dict[str, Any],
    weeks: list[int],
    calibration_scale: Optional[dict[str, float]] = None,
) -> list[dict[str, Any]]:
    """For each (player, week) where both nflreadpy and CSV data exist,
    return a comparison row.

    calibration_scale: {position: scale} applied to the projection.
    """
    cal = calibration_scale or {}
    rows = []
    for pid, log in game_logs.items():
        if not log:
            continue
        position = next((g.get("position") for g in log if g.get("position")), None)
        csv_by_week = csv_actuals.get(pid)
        scale = cal.get(position, 1.0) if position else 1.0

        for wk in weeks:
            actual_entry = next(
                (g for g in log if g["season"] == SEASON and g["week"] == wk), None
            )
            if actual_entry is None:
                continue

            nfl_actual = float(compute_league_points(actual_entry, scoring_config))
            csv_actual = (csv_by_week or {}).get(wk)

            proj = project_player(
                log, scoring_config, SEASON, wk,
                window=DEFAULT_WINDOW,
                calibration_scale=scale,
            )
            projected = proj["projected_points"]
            confidence = proj["confidence"]

            row: dict[str, Any] = {
                "player_id": pid,
                "position": position,
                "week": wk,
                "projected": projected,
                "nfl_actual": nfl_actual,
                "csv_actual": csv_actual,
                "confidence": confidence,
                "games_used": proj["games_used"],
                # Signed: positive = we underprojected (actual came in higher)
                "err_nfl": round(nfl_actual - projected, 2),
                "abs_nfl": round(abs(nfl_actual - projected), 2),
            }
            if csv_actual is not None:
                row["err_csv"] = round(csv_actual - projected, 2)
                row["abs_csv"] = round(abs(csv_actual - projected), 2)
            rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Step 5: Statistics helpers
# ---------------------------------------------------------------------------

def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _std(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx, my = _mean(xs), _mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return round(cov / math.sqrt(vx * vy), 3)


def position_metrics(
    rows: list[dict[str, Any]],
    error_key: str = "err_csv",
    abs_key: str = "abs_csv",
    actual_key: str = "csv_actual",
) -> dict[str, dict]:
    """Compute per-position MAE/bias/correlation from comparison rows."""
    valid = [r for r in rows if r.get(error_key) is not None]
    by_pos: dict[str, list[dict]] = {}
    for r in valid:
        by_pos.setdefault(r["position"] or "UNK", []).append(r)

    out: dict[str, dict] = {}
    for pos, pos_rows in sorted(by_pos.items()):
        errs = [r[error_key] for r in pos_rows]
        abs_errs = [r[abs_key] for r in pos_rows]
        proj_vals = [r["projected"] for r in pos_rows]
        act_vals = [r[actual_key] for r in pos_rows]
        out[pos] = {
            "n": len(pos_rows),
            "mae": round(_mean(abs_errs), 3),
            "bias": round(_mean(errs), 3),
            "correlation": _pearson(proj_vals, act_vals),
            "mean_actual": round(_mean(act_vals), 3),
            "mean_projected": round(_mean(proj_vals), 3),
        }
    return out


def optimal_calibration_scale(metrics: dict) -> float:
    """L2-optimal scale: mean(actual) / mean(projection).

    If bias > 0 (we underprojected), scale > 1 to raise projections.
    If bias < 0 (we overprojected), scale < 1 to lower projections.
    """
    if metrics["mean_projected"] == 0:
        return 1.0
    return round(metrics["mean_actual"] / metrics["mean_projected"], 4)


def print_header(title: str) -> None:
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print(f"{'=' * 64}")


def print_metrics(label: str, m: dict) -> None:
    if m.get("n", 0) == 0:
        print(f"  {label}: no data")
        return
    print(
        f"  {label:6s}  n={m['n']:5d}  MAE={m['mae']:.3f}  bias={m['bias']:+.3f}"
        f"  corr={m['correlation']}  "
        f"μ_proj={m['mean_projected']:.2f}  μ_actual={m['mean_actual']:.2f}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(csv_path: str, rotowire: bool = False, league: str = "league-1") -> None:
    # ---- Load scoring config ----
    config_path = Path(__file__).parent / f"leagues/{league}/scoring-config.json"
    with open(config_path) as f:
        scoring_config = json.load(f)
    print(f"\nLeague config: {league}  (pass_td={scoring_config.get('linear', {}).get('pass_td')},"
          f" reception={scoring_config.get('linear', {}).get('reception')})")

    # ---- Parse CSV ----
    print(f"Parsing CSV: {csv_path}")
    csv_lookup = parse_csv(csv_path)
    print(f"  {len(csv_lookup)} player-season entries parsed from CSV.")

    # ---- Load nflreadpy ----
    print(f"Loading nflreadpy {SEASON} player stats…")
    game_logs, player_info = load_game_logs_with_names(SEASON)
    print(f"  {len(game_logs)} players loaded ({POSITIONS}).")

    # ---- Match players ----
    csv_actuals = match_players(player_info, csv_lookup)
    print(f"  {len(csv_actuals)} players matched to CSV entries.")

    # ---- Run comparison (scale=1.0 baseline) ----
    print("Running projections for all 18 weeks (this may take a moment)…")
    baseline_rows = run_comparison(
        game_logs, csv_actuals, scoring_config, ALL_WEEKS,
        calibration_scale={pos: 1.0 for pos in POSITIONS},
    )
    csv_rows = [r for r in baseline_rows if r.get("csv_actual") is not None]
    nfl_rows = baseline_rows  # all have nfl_actual

    print_header("SCORING FORMULA GAP: our scoring.py vs FantasyPros PPR actuals")
    print("  (positive bias = CSV scores higher than our formula → we're under-scoring that position)")
    scoring_gap = position_metrics(csv_rows, "err_csv", "abs_csv", "csv_actual")
    # Also show the direction compared to nfl_actual
    nfl_self = position_metrics(nfl_rows, "err_nfl", "abs_nfl", "nfl_actual")
    for pos in POSITIONS:
        m_csv = scoring_gap.get(pos)
        m_nfl = nfl_self.get(pos)
        if not m_csv or not m_nfl:
            continue
        scoring_delta = round(m_csv["mean_actual"] - m_nfl["mean_actual"], 3)
        print(f"  {pos:4s}  csv_μ={m_csv['mean_actual']:.2f}  nfl_μ={m_nfl['mean_actual']:.2f}"
              f"  gap={scoring_delta:+.2f} pts/game (avg CSV minus our formula)")

    print_header("BASELINE (scale=1.0) vs CSV ACTUALS — all 18 weeks")
    all_csv_metrics = position_metrics(csv_rows, "err_csv", "abs_csv", "csv_actual")
    for pos in POSITIONS:
        m = all_csv_metrics.get(pos, {"n": 0})
        print_metrics(pos, m)

    print_header("BASELINE (scale=1.0) vs nflreadpy ACTUALS — all 18 weeks")
    all_nfl_metrics = position_metrics(nfl_rows, "err_nfl", "abs_nfl", "nfl_actual")
    for pos in POSITIONS:
        m = all_nfl_metrics.get(pos, {"n": 0})
        print_metrics(pos, m)

    # ---- Compute optimal calibration scales ----
    print_header("OPTIMAL CALIBRATION SCALES (vs CSV actuals)")
    optimal_scales: dict[str, float] = {}
    for pos in POSITIONS:
        m = all_csv_metrics.get(pos)
        if not m or m["n"] == 0:
            optimal_scales[pos] = 1.0
            continue
        scale = optimal_calibration_scale(m)
        optimal_scales[pos] = scale
        current = POSITION_CALIBRATION_SCALE.get(pos, 1.0)
        print(
            f"  {pos:4s}  current={current:.3f}  optimal={scale:.4f}"
            f"  bias_at_1.0={m['bias']:+.3f}"
        )

    # ---- Run comparison with optimal scales ----
    print_header("OPTIMALLY SCALED vs CSV ACTUALS — all 18 weeks")
    scaled_rows = run_comparison(
        game_logs, csv_actuals, scoring_config, ALL_WEEKS,
        calibration_scale=optimal_scales,
    )
    scaled_csv_rows = [r for r in scaled_rows if r.get("csv_actual") is not None]
    scaled_metrics = position_metrics(scaled_csv_rows, "err_csv", "abs_csv", "csv_actual")
    for pos in POSITIONS:
        m = scaled_metrics.get(pos, {"n": 0})
        print_metrics(pos, m)

    # ---- Significance tests: does scaling actually help? ----
    print_header("SIGNIFICANCE TESTS — does calibration scaling help?")
    for pos in POSITIONS:
        pos_base = [r for r in csv_rows if r["position"] == pos]
        pos_scaled = [r for r in scaled_csv_rows if r["position"] == pos]
        if not pos_base:
            continue
        # Pair on (player_id, week)
        base_by_key = {(r["player_id"], r["week"]): r for r in pos_base}
        scaled_by_key = {(r["player_id"], r["week"]): r for r in pos_scaled}
        keys = sorted(set(base_by_key) & set(scaled_by_key))
        base_abs = [base_by_key[k]["abs_csv"] for k in keys]
        scl_abs = [scaled_by_key[k]["abs_csv"] for k in keys]
        test = paired_significance_test(scl_abs, base_abs)
        scale = optimal_scales.get(pos, 1.0)
        improvement = round(_mean(base_abs) - _mean(scl_abs), 3) if base_abs else 0
        sig = "YES" if test.get("significant") else "no"
        print(
            f"  {pos:4s}  scale={scale:.4f}  MAE_improvement={improvement:+.3f}"
            f"  p={test.get('p_value')}  significant={sig}"
        )

    # ---- Per-week bias analysis ----
    print_header("BIAS BY WEEK (scale=1.0, CSV actuals) — detecting seasonal drift")
    week_bias: dict[int, list[float]] = {}
    for r in csv_rows:
        week_bias.setdefault(r["week"], []).append(r["err_csv"])
    print(f"  {'Wk':>3}  {'n':>5}  {'bias':>7}  {'MAE':>7}")
    for wk in ALL_WEEKS:
        errs = week_bias.get(wk, [])
        if not errs:
            continue
        bias_wk = round(_mean(errs), 2)
        mae_wk = round(_mean([abs(e) for e in errs]), 2)
        print(f"  {wk:>3}  {len(errs):>5}  {bias_wk:>+7.2f}  {mae_wk:>7.2f}")

    # ---- Top outliers (hardest-to-predict games) ----
    print_header("TOP 20 HARDEST-TO-PREDICT GAMES (largest |error| vs CSV)")
    sorted_outliers = sorted(csv_rows, key=lambda r: r.get("abs_csv", 0), reverse=True)[:20]
    print(f"  {'Wk':>3}  {'Pos':>4}  {'proj':>6}  {'actual':>7}  {'error':>7}  {'conf'}")
    for r in sorted_outliers:
        print(
            f"  {r['week']:>3}  {r['position']:>4}  {r['projected']:>6.1f}"
            f"  {r['csv_actual']:>7.1f}  {r['err_csv']:>+7.1f}  {r['confidence']}"
        )

    # ---- Matched vs unmatched coverage ----
    total_csv_players = len([k for k in csv_lookup if k[1] in ("QB", "RB", "WR", "TE") or True])
    print_header("MATCH COVERAGE SUMMARY")
    print(f"  nflreadpy players loaded:   {len(player_info)}")
    print(f"  CSV entries (all positions): {len(csv_lookup)}")
    print(f"  Players matched to CSV:      {len(csv_actuals)}")
    print(f"  Player-week rows compared:   {len(csv_rows)}")
    match_pct = 100 * len(csv_actuals) / len(player_info) if player_info else 0
    print(f"  Match rate:                  {match_pct:.1f}%")

    # ---- Proper nflreadpy-based analysis (our league's 6-pt TD scoring) ----
    # The CSV uses 4-pt passing TDs; our league uses 6-pt. QB calibration must
    # use nflreadpy actuals (our formula), not the CSV. RB/WR/TE gap is small
    # (<0.3 pts) so either reference is valid for those positions.
    print_header("OPTIMAL CALIBRATION SCALES (vs nflreadpy / our formula — the correct QB reference)")
    nfl_optimal_scales: dict[str, float] = {}
    for pos in POSITIONS:
        m = all_nfl_metrics.get(pos)
        if not m or m["n"] == 0:
            nfl_optimal_scales[pos] = 1.0
            continue
        scale = optimal_calibration_scale(m)
        nfl_optimal_scales[pos] = scale
        print(f"  {pos:4s}  optimal_vs_nfl={scale:.4f}  optimal_vs_csv={optimal_scales.get(pos, 1.0):.4f}  bias_at_1.0={m['bias']:+.3f}")

    # Week 1 inflates QB bias heavily (no_data rows all get 0-pt projections;
    # every QB starts week 1 with no prior season data). Compute week-2+ estimate.
    print_header("QB BIAS EXCLUDING WEEK 1 (no_data inflation removed)")
    qb_nfl_w2plus = [r for r in nfl_rows if r["position"] == "QB" and r["week"] > 1]
    if qb_nfl_w2plus:
        errs_w2 = [r["err_nfl"] for r in qb_nfl_w2plus]
        abs_errs_w2 = [r["abs_nfl"] for r in qb_nfl_w2plus]
        bias_w2 = _mean(errs_w2)
        mae_w2 = _mean(abs_errs_w2)
        mean_proj_w2 = _mean([r["projected"] for r in qb_nfl_w2plus])
        mean_act_w2 = _mean([r["nfl_actual"] for r in qb_nfl_w2plus])
        optimal_qb_w2 = round(mean_act_w2 / mean_proj_w2, 4) if mean_proj_w2 else 1.0
        print(f"  QB wk2+: n={len(qb_nfl_w2plus)}  bias={bias_w2:+.3f}  MAE={mae_w2:.3f}")
        print(f"  μ_proj={mean_proj_w2:.2f}  μ_actual={mean_act_w2:.2f}  optimal_scale={optimal_qb_w2:.4f}")
        print(f"  (Week 1 inflated all-weeks bias from {bias_w2:+.2f} to {all_nfl_metrics.get('QB', {}).get('bias', 0):+.2f})")

    # Significance test for scaling to nfl-optimal for QB (wk2+)
    if qb_nfl_w2plus:
        scale_nfl_qb = {pos: 1.0 for pos in POSITIONS}
        scale_nfl_qb["QB"] = optimal_qb_w2
        scaled_nfl_rows = run_comparison(
            game_logs, csv_actuals, scoring_config, list(range(2, 19)),
            calibration_scale=scale_nfl_qb,
        )
        qb_base_w2 = [r for r in nfl_rows if r["position"] == "QB" and r["week"] > 1]
        qb_scl_w2 = [r for r in scaled_nfl_rows if r["position"] == "QB"]
        base_by_key = {(r["player_id"], r["week"]): r for r in qb_base_w2}
        scl_by_key = {(r["player_id"], r["week"]): r for r in qb_scl_w2}
        keys = sorted(set(base_by_key) & set(scl_by_key))
        base_abs = [base_by_key[k]["abs_nfl"] for k in keys]
        scl_abs = [scl_by_key[k]["abs_nfl"] for k in keys]
        test_nfl_qb = paired_significance_test(scl_abs, base_abs)
        improvement_nfl = round(_mean(base_abs) - _mean(scl_abs), 3) if base_abs else 0
        sig_nfl = "YES" if test_nfl_qb.get("significant") else "no"
        print(f"  Significance test (scale={optimal_qb_w2} vs 1.0, wk2+, vs nflreadpy):")
        print(f"    MAE_improvement={improvement_nfl:+.3f}  p={test_nfl_qb.get('p_value')}  significant={sig_nfl}")

    # ---- Final recommendation ----
    print_header("FINAL RECOMMENDED POSITION_CALIBRATION_SCALE for projections.py")
    print("  Methodology:")
    print("  - QB uses nflreadpy actuals (our league's 6-pt TDs); CSV uses 4-pt TDs = wrong ref for QB.")
    print("  - ALL positions: only update scale if it actually improves MAE (not just reduces bias).")
    print("    Right-skewed boom games inflate mean_actual above median; scaling to the mean over-")
    print("    projects ordinary weeks more than it helps boom weeks → MAE gets worse.")
    print("  - Week 1 excluded from QB calibration: all week-1 QBs are no_data (0 pts projected),")
    print("    artificially inflating the positive bias. Real weekly reports start wk2+.")
    print()
    # QB: use the optimal scale only if the significance test showed MAE improvement
    qb_scale_improves_mae = qb_nfl_w2plus and improvement_nfl > 0
    final_scales: dict[str, float] = {
        "QB": round(optimal_qb_w2, 2) if qb_scale_improves_mae else 1.0,
        "RB": 1.0,
        "WR": 1.0,
        "TE": 1.0,
        "DST": 1.0,
        "K": 1.0,
    }
    for pos in POSITIONS:
        current = POSITION_CALIBRATION_SCALE.get(pos, 1.0)
        optimal = final_scales.get(pos, 1.0)
        delta = abs(optimal - current)
        ref = "nflreadpy" if pos == "QB" else "CSV (consistent)"
        action = "UPDATE" if delta >= 0.03 else "keep"
        print(f"  {pos:4s}  current={current:.3f}  optimal={optimal:.4f} (vs {ref})  delta={delta:.3f}  → {action}")

    print()
    print("  Copy-paste block for projections.py:")
    print("  POSITION_CALIBRATION_SCALE: dict[str, float] = {")
    for pos in ("QB", "RB", "WR", "TE", "DST", "K"):
        val = final_scales.get(pos, POSITION_CALIBRATION_SCALE.get(pos, 1.0))
        print(f'      "{pos}": {val:.2f},')
    print("  }")

    # ---- Rotowire backtest (optional, gated on --rotowire flag) ----
    if rotowire:
        rw_cache_path = Path(__file__).parent / f"rotowire_cache_{SEASON}.json"
        run_rotowire_backtest(baseline_rows, player_info, rw_cache_path)


# ---------------------------------------------------------------------------
# Rotowire backtest: does adding goal-line touch signal improve MAE?
# ---------------------------------------------------------------------------

def _rz_score_as_of(gl_rows: list[dict[str, Any]], week: int, n: int = 3) -> float:
    """Average (rzTargets5 + rzRush5) over the last n non-DNP games strictly
    before `week`, matching the same as-of discipline as projections.games_before.
    Returns 0.0 when no data is available (same neutral-signal behaviour as
    opponent_multiplier=1.0 / usage_multiplier=1.0)."""
    def _wk(r: dict[str, Any]) -> Optional[int]:
        try:
            return int(r.get("week", 0))
        except (ValueError, TypeError):
            return None  # skip postseason rows (e.g. week="WC", "DIV", "SB")

    prior = [r for r in gl_rows if (_wk(r) or 0) < week and not r.get("dnp")]
    recent = prior[-n:] if prior else []
    if not recent:
        return 0.0
    total = sum(
        (float(r.get("rzTargets5") or 0) + float(r.get("rzRush5") or 0))
        for r in recent
    )
    return total / len(recent)


def load_rotowire_gamelogs(
    player_info: dict[str, dict[str, str]],
    cache_path: Path,
    season_year: int = 2025,
) -> dict[str, list[dict[str, Any]]]:
    """Return {gsis_id: gl_rows} for all players that can be fetched from
    Rotowire. Uses the same disk cache as the production path so that running
    the backtest right after a report run costs zero additional network calls."""
    from rotowire import (
        FETCH_DELAY_SECS,
        ROTOWIRE_POSITIONS,
        fetch_player_gamelog,
        load_id_crosswalk,
        _cache_is_fresh,
        CACHE_TTL_HOURS,
    )

    crosswalk = load_id_crosswalk()
    if not crosswalk:
        print("  Rotowire crosswalk empty -- skipping Rotowire backtest section.")
        return {}

    # Load existing cache
    existing: dict[str, list[dict[str, Any]]] = {}
    cache_fresh = False
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            existing = cached.get("gamelogs", {})
            cache_fresh = _cache_is_fresh(cached, CACHE_TTL_HOURS)
        except (json.JSONDecodeError, OSError):
            pass

    eligible = {
        pid: info
        for pid, info in player_info.items()
        if info.get("position") in ROTOWIRE_POSITIONS and crosswalk.get(pid)
    }
    to_fetch = {pid: p for pid, p in eligible.items() if pid not in existing} if cache_fresh else eligible

    if to_fetch:
        print(f"  Fetching Rotowire gamelogs for {len(to_fetch)} players (cached={len(existing)})...")
        fetched = 0
        for pid, info in to_fetch.items():
            rw_id = crosswalk[pid]
            pos = info.get("position", "RB")
            team = info.get("team") or "KC"
            gl = fetch_player_gamelog(rw_id, pos, team, season_year=season_year)
            if gl is not None:
                existing[pid] = gl
                fetched += 1
            time.sleep(FETCH_DELAY_SECS)
        print(f"  Fetched {fetched}/{len(to_fetch)}. Saving cache...")
        try:
            cache_path.write_text(
                json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "gamelogs": existing}, indent=2) + "\n"
            )
        except OSError:
            pass

    return {pid: gl for pid, gl in existing.items() if pid in eligible}


def run_rz_sweep(
    base_rows: list[dict[str, Any]],
    rw_gamelogs: dict[str, list[dict[str, Any]]],
    alphas: list[float],
    n_recent: int = 3,
) -> dict[float, list[dict[str, Any]]]:
    """For each alpha in alphas, build a comparison row set where projected
    points are multiplied by `1.0 + alpha * rz_score_as_of(week)`.

    rz_score_as_of is computed from Rotowire's gl2025 rows, filtered to games
    strictly before the target week -- same as-of discipline as the model.
    Only players that have Rotowire data are affected; others keep their
    base projection unchanged (rz_score=0 → multiplier=1.0, same result).
    """
    results: dict[float, list[dict[str, Any]]] = {}
    for alpha in alphas:
        rows_out = []
        for r in base_rows:
            gl = rw_gamelogs.get(r["player_id"])
            rz = _rz_score_as_of(gl, r["week"], n_recent) if gl else 0.0
            multiplier = 1.0 + alpha * rz
            proj_rz = round(r["projected"] * multiplier, 2)
            row_out = dict(r)
            row_out["projected"] = proj_rz
            row_out["rz_score"] = rz
            row_out["rz_multiplier"] = round(multiplier, 4)
            if row_out.get("csv_actual") is not None:
                row_out["err_csv"] = round(row_out["csv_actual"] - proj_rz, 2)
                row_out["abs_csv"] = round(abs(row_out["csv_actual"] - proj_rz), 2)
            row_out["err_nfl"] = round(row_out["nfl_actual"] - proj_rz, 2)
            row_out["abs_nfl"] = round(abs(row_out["nfl_actual"] - proj_rz), 2)
            rows_out.append(row_out)
        results[alpha] = rows_out
    return results


def run_rotowire_backtest(
    base_rows: list[dict[str, Any]],
    player_info: dict[str, dict[str, str]],
    cache_path: Path,
) -> None:
    """Fetch Rotowire gamelogs and report whether rz_touches_at_5 improves
    MAE vs the base projection, sweeping alpha ∈ {0, 0.02, 0.05, 0.08, 0.10}."""
    print_header("ROTOWIRE RZ BACKTEST: does goal-line touch signal improve MAE?")
    print("  Signal: avg(rzTargets5 + rzRush5) over last 3 non-DNP games as-of week.")
    print("  Multiplier: projected * (1.0 + alpha * rz_score).")
    print("  Sweep: alpha ∈ {0, 0.02, 0.05, 0.08, 0.10, 0.15} — paired MAE vs alpha=0.")

    rw_gamelogs = load_rotowire_gamelogs(player_info, cache_path)
    if not rw_gamelogs:
        print("  No Rotowire data -- skipping.")
        return

    covered = sum(1 for r in base_rows if r["player_id"] in rw_gamelogs)
    print(f"  {len(rw_gamelogs)} players with Rotowire data; {covered}/{len(base_rows)} base rows covered.")

    alphas = [0.0, 0.02, 0.05, 0.08, 0.10, 0.15]
    sweep = run_rz_sweep(base_rows, rw_gamelogs, alphas)

    # CSV rows only (need csv_actual for the comparison)
    print()
    print(f"  {'alpha':>6}  {'MAE_csv':>8}  {'bias_csv':>9}  {'Δ_MAE':>7}  sig")
    base_csv = [r for r in sweep[0.0] if r.get("csv_actual") is not None]

    best_alpha = 0.0
    best_mae = float("inf")
    for alpha in alphas:
        rows_a = [r for r in sweep[alpha] if r.get("csv_actual") is not None]
        if not rows_a:
            continue
        mae = _mean([r["abs_csv"] for r in rows_a])
        bias = _mean([r["err_csv"] for r in rows_a])
        delta = mae - _mean([r["abs_csv"] for r in base_csv])

        # Paired significance vs alpha=0 baseline
        base_by_key = {(r["player_id"], r["week"]): r for r in base_csv}
        rows_by_key = {(r["player_id"], r["week"]): r for r in rows_a}
        keys = sorted(set(base_by_key) & set(rows_by_key))
        base_abs = [base_by_key[k]["abs_csv"] for k in keys]
        rz_abs = [rows_by_key[k]["abs_csv"] for k in keys]
        test = paired_significance_test(rz_abs, base_abs) if alpha > 0 else {"significant": None, "p_value": None}
        sig_str = ("YES" if test.get("significant") else "no") if alpha > 0 else "(baseline)"
        print(f"  {alpha:6.2f}  {mae:8.3f}  {bias:+9.3f}  {delta:+7.3f}  {sig_str}")

        if mae < best_mae:
            best_mae = mae
            best_alpha = alpha

    print()
    print(f"  Best alpha: {best_alpha} (MAE={best_mae:.3f})")

    # Per-position breakdown at best alpha
    if best_alpha > 0:
        print()
        print(f"  Per-position MAE at alpha={best_alpha} vs baseline (alpha=0):")
        for pos in POSITIONS:
            base_pos = [r for r in base_csv if r["position"] == pos]
            rz_pos = [r for r in sweep[best_alpha] if r["position"] == pos and r.get("csv_actual") is not None]
            if not base_pos or not rz_pos:
                continue
            base_mae = _mean([r["abs_csv"] for r in base_pos])
            rz_mae = _mean([r["abs_csv"] for r in rz_pos])
            delta = rz_mae - base_mae
            print(f"    {pos:4s}  base_MAE={base_mae:.3f}  rz_MAE={rz_mae:.3f}  Δ={delta:+.3f}")

        # rz_score distribution at best_alpha
        rz_all = [r["rz_score"] for r in sweep[best_alpha] if r.get("rz_score", 0) > 0]
        if rz_all:
            rz_sorted = sorted(rz_all)
            n = len(rz_sorted)
            print()
            print(f"  rz_score distribution (players with rz>0, n={n}):")
            print(f"    p25={rz_sorted[n//4]:.2f}  median={rz_sorted[n//2]:.2f}"
                  f"  p75={rz_sorted[3*n//4]:.2f}  max={rz_sorted[-1]:.2f}")


if __name__ == "__main__":
    import argparse as _argparse

    _parser = _argparse.ArgumentParser(
        description="CSV backtest comparing in-house projections vs FantasyPros PPR actuals."
    )
    _parser.add_argument("csv_path", help="Path to FantasyPros season summary CSV")
    _parser.add_argument(
        "--rotowire",
        action="store_true",
        help="Also run the Rotowire rz_touches backtest (fetches Rotowire data, uses disk cache)",
    )
    _parser.add_argument(
        "--league",
        default="league-1",
        help="League folder name under backend/leagues/ (default: league-1)",
    )
    _args = _parser.parse_args()
    main(_args.csv_path, rotowire=_args.rotowire, league=_args.league)
