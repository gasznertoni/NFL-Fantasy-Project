"""Unit tests for track_record.py. The hit-rate threshold and per-tier
aggregation are checked directly against the numbers from the existing
frontend/public/mock/track-record.json mock fixture (11 scored predictions
across 2 tiers, weeks 1-2 graded, week 3 ungraded) -- not just synthetic
round numbers, so a change here that breaks the shape this fixture already
demonstrates gets caught."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from track_record import (  # noqa: E402
    build_history_entry,
    build_track_record,
    history_from_weekly_report,
    summarize_tier,
)


def player(player_id, name, position):
    return {"playerId": player_id, "name": name, "position": position}


def projection_entry(player_id, name, position, tier, points):
    return {
        "playerId": player_id,
        "name": name,
        "position": position,
        "projection": {"tier": tier, "points": points, "tierLabel": tier, "source": "x"},
    }


class TestOutcomeCorrect(unittest.TestCase):
    def test_unscored_prediction_has_null_outcome(self):
        entry = build_history_entry(2026, 3, player("p1", "A", "QB"), "consensus", "start", 20.0, None)
        self.assertIsNone(entry["actualPoints"])
        self.assertIsNone(entry["outcomeCorrect"])

    def test_actual_meeting_threshold_is_correct(self):
        # 0.75 * 20.0 = 15.0 exactly -- boundary is inclusive
        entry = build_history_entry(2026, 1, player("p1", "A", "QB"), "consensus", "start", 20.0, 15.0)
        self.assertTrue(entry["outcomeCorrect"])

    def test_actual_just_below_threshold_is_incorrect(self):
        entry = build_history_entry(2026, 1, player("p1", "A", "QB"), "consensus", "start", 20.0, 14.9)
        self.assertFalse(entry["outcomeCorrect"])

    def test_overperformance_is_always_correct(self):
        entry = build_history_entry(2026, 1, player("p1", "A", "QB"), "consensus", "start", 10.0, 50.0)
        self.assertTrue(entry["outcomeCorrect"])

    def test_zero_projection_with_any_nonnegative_actual_is_correct(self):
        entry = build_history_entry(2026, 1, player("p1", "A", "RB"), "in_house_estimate", "start", 0.0, 2.0)
        self.assertTrue(entry["outcomeCorrect"])

    def test_prediction_id_is_deterministic_and_distinguishes_recommendation_type(self):
        a = build_history_entry(2026, 1, player("p1", "A", "QB"), "consensus", "start", 20.0, None)
        b = build_history_entry(2026, 1, player("p1", "A", "QB"), "consensus", "waiver_add", 20.0, None)
        self.assertNotEqual(a["predictionId"], b["predictionId"])
        self.assertEqual(a["predictionId"], build_history_entry(2026, 1, player("p1", "A", "QB"), "consensus", "start", 20.0, None)["predictionId"])


class TestHistoryFromWeeklyReport(unittest.TestCase):
    def test_projections_become_start_and_waiver_targets_become_waiver_add(self):
        report = {
            "projections": [projection_entry("p1", "A", "QB", "consensus", 20.0)],
            "waiverTargets": [projection_entry("p2", "B", "RB", "in_house_estimate", 8.0)],
        }
        history = history_from_weekly_report(2026, 1, report, {})
        by_id = {h["player"]["playerId"]: h for h in history}
        self.assertEqual(by_id["p1"]["recommendationType"], "start")
        self.assertEqual(by_id["p2"]["recommendationType"], "waiver_add")

    def test_missing_actual_points_entry_means_ungraded(self):
        report = {"projections": [projection_entry("p1", "A", "QB", "consensus", 20.0)], "waiverTargets": []}
        history = history_from_weekly_report(2026, 1, report, {})  # p1 absent entirely
        self.assertIsNone(history[0]["actualPoints"])


class TestFixtureReplication(unittest.TestCase):
    """Reproduces frontend/public/mock/track-record.json's weeks 1-2 data
    and asserts every individual outcomeCorrect flag plus the aggregated
    summary numbers match that fixture exactly."""

    def setUp(self):
        def rep(tier, points):
            return {"tier": tier, "points": points, "tierLabel": tier, "source": "x"}

        def entry(pid, name, pos, tier, points):
            return {"playerId": pid, "name": name, "position": pos, "projection": rep(tier, points)}

        self.week1 = {
            "projections": [
                entry("p_00123", "Josh Allen", "QB", "consensus", 24.3),
                entry("p_00201", "Bijan Robinson", "RB", "consensus", 18.6),
                entry("p_00202", "Tony Pollard", "RB", "in_house_estimate", 12.4),
                entry("p_00301", "CeeDee Lamb", "WR", "consensus", 17.9),
                entry("p_00302", "Chris Olave", "WR", "consensus", 13.2),
            ],
            "waiverTargets": [entry("p_00456", "Jaylen Warren", "RB", "in_house_estimate", 9.8)],
        }
        self.week2 = {
            "projections": [
                entry("p_00123", "Josh Allen", "QB", "consensus", 23.1),
                entry("p_00201", "Bijan Robinson", "RB", "consensus", 19.4),
                entry("p_00202", "Tony Pollard", "RB", "in_house_estimate", 11.7),
                entry("p_00301", "CeeDee Lamb", "WR", "consensus", 18.2),
                entry("p_00456", "Jaylen Warren", "RB", "in_house_estimate", 11.2),
            ],
            "waiverTargets": [entry("p_00458", "Ray Davis", "RB", "in_house_estimate", 10.4)],
        }
        self.actuals = {
            1: {
                "p_00123": 27.1,
                "p_00201": 15.9,
                "p_00202": 8.6,
                "p_00301": 19.4,
                "p_00302": 5.8,
                "p_00456": 11.6,
            },
            2: {
                "p_00123": 21.4,
                "p_00201": 24.2,
                "p_00202": 3.2,
                "p_00301": 12.1,
                "p_00456": 10.8,
                "p_00458": 14.7,
            },
        }

    def test_full_track_record_matches_fixture_numbers(self):
        reports = {1: self.week1, 2: self.week2}
        track_record = build_track_record(2026, 2, reports, self.actuals)

        self.assertEqual(track_record["season"], 2026)
        self.assertEqual(track_record["asOfWeek"], 2)

        by_id = {(h["week"], h["player"]["playerId"], h["recommendationType"]): h for h in track_record["history"]}
        self.assertTrue(by_id[(1, "p_00123", "start")]["outcomeCorrect"])
        self.assertTrue(by_id[(1, "p_00201", "start")]["outcomeCorrect"])
        self.assertFalse(by_id[(1, "p_00202", "start")]["outcomeCorrect"])
        self.assertTrue(by_id[(1, "p_00301", "start")]["outcomeCorrect"])
        self.assertFalse(by_id[(1, "p_00302", "start")]["outcomeCorrect"])
        self.assertTrue(by_id[(1, "p_00456", "waiver_add")]["outcomeCorrect"])
        self.assertTrue(by_id[(2, "p_00123", "start")]["outcomeCorrect"])
        self.assertTrue(by_id[(2, "p_00201", "start")]["outcomeCorrect"])
        self.assertFalse(by_id[(2, "p_00202", "start")]["outcomeCorrect"])
        self.assertFalse(by_id[(2, "p_00301", "start")]["outcomeCorrect"])
        self.assertTrue(by_id[(2, "p_00456", "start")]["outcomeCorrect"])
        self.assertTrue(by_id[(2, "p_00458", "waiver_add")]["outcomeCorrect"])

        summary = track_record["summary"]
        self.assertEqual(summary["consensus"]["predictionsScored"], 7)
        self.assertAlmostEqual(summary["consensus"]["startSitHitRate"], 0.71)
        self.assertAlmostEqual(summary["consensus"]["meanAbsoluteError"], 3.9)
        self.assertEqual(summary["in_house_estimate"]["predictionsScored"], 5)
        self.assertAlmostEqual(summary["in_house_estimate"]["startSitHitRate"], 0.6)
        self.assertAlmostEqual(summary["in_house_estimate"]["meanAbsoluteError"], 3.8)

    def test_current_ungraded_week_is_included_but_not_scored(self):
        week3 = {
            "projections": [
                {
                    "playerId": "p_00123",
                    "name": "Josh Allen",
                    "position": "QB",
                    "projection": {"tier": "consensus", "points": 25.7, "tierLabel": "x", "source": "x"},
                }
            ],
            "waiverTargets": [],
        }
        reports = {1: self.week1, 2: self.week2, 3: week3}
        track_record = build_track_record(2026, 3, reports, self.actuals)  # actuals has no week 3 entry

        self.assertEqual(track_record["asOfWeek"], 3)
        week3_rows = [h for h in track_record["history"] if h["week"] == 3]
        self.assertEqual(len(week3_rows), 1)
        self.assertIsNone(week3_rows[0]["actualPoints"])
        self.assertIsNone(week3_rows[0]["outcomeCorrect"])
        # week 3's ungraded row must not shift the tier summaries computed above
        self.assertEqual(track_record["summary"]["consensus"]["predictionsScored"], 7)


class TestSummarizeTierEmpty(unittest.TestCase):
    def test_no_scored_predictions_returns_zeroes_not_nulls(self):
        summary = summarize_tier([], "consensus", "Consensus projection")
        self.assertEqual(summary["predictionsScored"], 0)
        self.assertEqual(summary["startSitHitRate"], 0.0)
        self.assertEqual(summary["meanAbsoluteError"], 0.0)


if __name__ == "__main__":
    unittest.main()
