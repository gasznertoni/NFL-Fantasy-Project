"""Unit tests for waiver_targets.py: the eligibility filter (rostered-rank
cutoff + excluded designations), top-N overall selection, and the
templated rationale's branching (cold-start / low-confidence / trending
up / trending down / steady + injury-note appending)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from waiver_targets import (  # noqa: E402
    DEFAULT_ROSTERED_RANK_CUTOFF,
    describe_trend,
    generate_rationale,
    select_waiver_targets,
    waiver_eligible_candidates,
)

HEALTHY = {"designation": "Healthy", "riskLevel": "none", "summary": None}


def candidate(player_id, position, points, name=None, news_flag=None, games_used=4, confidence="full", per_game_points=None):
    return {
        "playerId": player_id,
        "name": name or player_id,
        "position": position,
        "team": "AAA",
        "opponent": "BBB",
        "points": points,
        "games_used": games_used,
        "confidence": confidence,
        "per_game_points": per_game_points if per_game_points is not None else [points] * games_used,
        "news_flag": news_flag or dict(HEALTHY),
    }


class TestWaiverEligibleCandidates(unittest.TestCase):
    def test_drops_top_ranked_players_per_position_as_assumed_rostered(self):
        # 2 RBs, cutoff of 1 for RB -> only the lower-ranked one is eligible
        candidates = [candidate("rb1", "RB", 20.0), candidate("rb2", "RB", 5.0)]
        eligible = waiver_eligible_candidates(candidates, rostered_rank_cutoff={"RB": 1})
        self.assertEqual([c["playerId"] for c in eligible], ["rb2"])

    def test_excludes_out_doubtful_and_ir_designations(self):
        out_flag = {"designation": "Out", "riskLevel": "high", "summary": "ruled out"}
        ir_flag = {"designation": "IR", "riskLevel": "high", "summary": "on IR"}
        doubtful_flag = {"designation": "Doubtful", "riskLevel": "high", "summary": "doubtful"}
        candidates = [
            candidate("a", "WR", 10.0, news_flag=out_flag),
            candidate("b", "WR", 9.0, news_flag=ir_flag),
            candidate("c", "WR", 8.0, news_flag=doubtful_flag),
            candidate("d", "WR", 7.0),
        ]
        eligible = waiver_eligible_candidates(candidates, rostered_rank_cutoff={"WR": 0})
        self.assertEqual([c["playerId"] for c in eligible], ["d"])

    def test_questionable_designation_is_not_excluded(self):
        questionable = {"designation": "Questionable", "riskLevel": "low", "summary": "limited"}
        candidates = [candidate("a", "TE", 5.0, news_flag=questionable)]
        eligible = waiver_eligible_candidates(candidates, rostered_rank_cutoff={"TE": 0})
        self.assertEqual(len(eligible), 1)

    def test_position_with_no_cutoff_entry_excludes_nothing(self):
        candidates = [candidate("a", "K", 8.0), candidate("b", "K", 6.0)]
        eligible = waiver_eligible_candidates(candidates, rostered_rank_cutoff={})
        self.assertEqual(len(eligible), 2)

    def test_default_cutoff_has_entries_for_dst_and_k(self):
        """Regression guard: DST/K added to the default cutoff table
        2026-08-12 after wiring those positions in for the first time
        surfaced that raw-points ranking with no cutoff let them dominate
        a real waiver-targets list (see the constant's own comment)."""
        self.assertIn("DST", DEFAULT_ROSTERED_RANK_CUTOFF)
        self.assertIn("K", DEFAULT_ROSTERED_RANK_CUTOFF)

    def test_dst_and_k_get_excluded_by_default_cutoff_not_just_raw_points(self):
        candidates = [candidate(f"dst{i}", "DST", 12.0 - i) for i in range(20)]
        eligible = waiver_eligible_candidates(candidates)  # uses DEFAULT_ROSTERED_RANK_CUTOFF
        self.assertEqual(len(eligible), 20 - DEFAULT_ROSTERED_RANK_CUTOFF["DST"])


class TestSelectWaiverTargets(unittest.TestCase):
    def test_ranks_by_points_across_positions_not_per_position(self):
        candidates = [
            candidate("rb", "RB", 9.0),
            candidate("wr", "WR", 12.0),
            candidate("te", "TE", 15.0),
            candidate("qb", "QB", 3.0),
        ]
        selected = select_waiver_targets(candidates, top_n=2, rostered_rank_cutoff={})
        self.assertEqual([c["playerId"] for c in selected], ["te", "wr"])

    def test_top_n_caps_the_result_count(self):
        candidates = [candidate(str(i), "RB", float(i)) for i in range(10)]
        selected = select_waiver_targets(candidates, top_n=3, rostered_rank_cutoff={})
        self.assertEqual(len(selected), 3)

    def test_default_cutoff_stops_dst_and_k_from_dominating_the_list(self):
        """Reproduces the shape of the real 2025-week-10 run that motivated
        DST/K's default cutoff entries: with no cutoff at all, every DST/K
        in the league is "waiver eligible" and out-ranks a genuinely
        below-replacement WR just by there being more of them. cutoff=0
        for WR here isolates that this test is specifically about DST/K's
        own cutoff working, not re-testing WR's separately-covered cutoff
        behavior."""
        cutoff = dict(DEFAULT_ROSTERED_RANK_CUTOFF, WR=0)
        candidates = [candidate(f"dst{i}", "DST", 13.0) for i in range(32)]
        candidates += [candidate(f"k{i}", "K", 12.0) for i in range(29)]
        candidates.append(candidate("wr_sleeper", "WR", 9.0))
        selected = select_waiver_targets(candidates, top_n=3, rostered_rank_cutoff=cutoff)
        positions = [c["position"] for c in selected]
        self.assertIn("WR", positions)


class TestDescribeTrend(unittest.TestCase):
    def test_fewer_than_two_games_is_unknown(self):
        self.assertIsNone(describe_trend([]))
        self.assertIsNone(describe_trend([5.0]))

    def test_recent_spike_is_up(self):
        self.assertEqual(describe_trend([5.0, 5.0, 5.0, 10.0]), "up")

    def test_recent_dip_is_down(self):
        self.assertEqual(describe_trend([10.0, 10.0, 10.0, 3.0]), "down")

    def test_stable_production_is_flat(self):
        self.assertEqual(describe_trend([8.0, 7.5, 8.2, 8.0]), "flat")

    def test_zero_prior_average_with_positive_latest_is_up(self):
        self.assertEqual(describe_trend([0.0, 0.0, 6.0]), "up")

    def test_zero_prior_average_with_zero_latest_is_none(self):
        self.assertIsNone(describe_trend([0.0, 0.0, 0.0]))


class TestGenerateRationale(unittest.TestCase):
    def test_no_data_confidence_gets_speculative_rationale(self):
        c = candidate("a", "RB", 0.0, name="Rookie Back", games_used=0, confidence="no_data", per_game_points=[])
        rationale = generate_rationale(c)
        self.assertIn("No games played yet", rationale)
        self.assertIn("Rookie Back", rationale)

    def test_low_confidence_mentions_sample_size(self):
        c = candidate("a", "WR", 9.5, games_used=1, confidence="low", per_game_points=[9.5])
        rationale = generate_rationale(c)
        self.assertIn("1 game", rationale)

    def test_upward_trend_mentions_trending_up(self):
        c = candidate("a", "WR", 8.0, games_used=4, confidence="full", per_game_points=[5.0, 5.0, 5.0, 12.0])
        self.assertIn("Trending up", generate_rationale(c))

    def test_downward_trend_mentions_cooled(self):
        c = candidate("a", "WR", 8.0, games_used=4, confidence="full", per_game_points=[10.0, 10.0, 10.0, 3.0])
        self.assertIn("cooled", generate_rationale(c))

    def test_flat_trend_gets_steady_rationale(self):
        c = candidate("a", "WR", 8.0, games_used=4, confidence="full", per_game_points=[8.0, 7.8, 8.1, 8.0])
        self.assertIn("Steady", generate_rationale(c))

    def test_elevated_risk_news_flag_appends_a_note(self):
        flag = {"designation": "Questionable", "riskLevel": "low", "summary": "Limited practice reps."}
        c = candidate("a", "WR", 8.0, games_used=4, confidence="full", per_game_points=[8.0] * 4, news_flag=flag)
        rationale = generate_rationale(c)
        self.assertIn("Note: Limited practice reps.", rationale)

    def test_no_risk_news_flag_does_not_append_a_note(self):
        c = candidate("a", "WR", 8.0, games_used=4, confidence="full", per_game_points=[8.0] * 4)
        self.assertNotIn("Note:", generate_rationale(c))


if __name__ == "__main__":
    unittest.main()
