"""Unit tests for backend/depth_charts.py.

load_depth_ranks is NOT covered -- it needs the network. What is covered is the
rank bucketing and both schema parsers, which is where the real risk lives:
nflverse changed the feed shape in 2025 and both are in range for this
project's training seasons.
"""

import unittest

from depth_charts import (
    MAX_RANK,
    bucket_rank,
    rank_map_from_legacy,
    rank_map_from_snapshots,
)


class TestBucketRank(unittest.TestCase):
    def test_ranks_pass_through_up_to_the_cap(self):
        self.assertEqual(bucket_rank(1), 1)
        self.assertEqual(bucket_rank(2), 2)
        self.assertEqual(bucket_rank(3), 3)

    def test_deeper_ranks_are_capped_not_extrapolated(self):
        # Past third string the pool thins and the play rate flattens, so the
        # distinction stops carrying information.
        self.assertEqual(bucket_rank(4), MAX_RANK)
        self.assertEqual(bucket_rank(11), MAX_RANK)

    def test_floats_are_accepted(self):
        self.assertEqual(bucket_rank(2.0), 2)

    def test_unknown_and_nonsense_ranks_are_none(self):
        for value in (None, 0, -1, "x", float("nan")):
            self.assertIsNone(bucket_rank(value), f"expected None for {value!r}")


class TestLegacySchema(unittest.TestCase):
    """Through 2024: one row per player per week, `depth_team` is the rank."""

    def test_reads_week_and_rank_directly(self):
        out = rank_map_from_legacy([
                {"season": 2023, "week": 1, "gsis_id": "p1", "depth_team": "1", "game_type": "REG"},
                {"season": 2023, "week": 2, "gsis_id": "p1", "depth_team": "2", "game_type": "REG"},
        ])
        self.assertEqual(out[(2023, 1, "p1")], 1.0)
        self.assertEqual(out[(2023, 2, "p1")], 2.0)

    def test_keeps_the_best_rank_when_a_player_is_listed_twice(self):
        # A player can appear at more than one position; the lowest rank is
        # the one that describes their actual role.
        out = rank_map_from_legacy([
                {"season": 2023, "week": 1, "gsis_id": "p1", "depth_team": "3", "game_type": "REG"},
                {"season": 2023, "week": 1, "gsis_id": "p1", "depth_team": "1", "game_type": "REG"},
        ])
        self.assertEqual(out[(2023, 1, "p1")], 1.0)

    def test_postseason_rows_are_excluded(self):
        out = rank_map_from_legacy([
                {"season": 2023, "week": 1, "gsis_id": "p1", "depth_team": "1", "game_type": "POST"},
        ])
        self.assertEqual(out, {})

    def test_unparseable_ranks_are_dropped(self):
        out = rank_map_from_legacy([
                {"season": 2023, "week": 1, "gsis_id": "p1", "depth_team": "-", "game_type": "REG"},
        ])
        self.assertEqual(out, {})


class TestSnapshotSchema(unittest.TestCase):
    """2025 onward: timestamped league-wide snapshots with no week column.

    The as-of filter is the point. The feed extends past the season, so an
    unfiltered read hands a week-1 projection a chart from the following March.
    """

    def _weeks(self, **kw):
        return {2025: {int(w): t for w, t in kw.items()}}

    def test_uses_the_latest_snapshot_at_or_before_kickoff(self):
        frame = [
            {"season": 2025, "dt": "2025-09-01T00:00Z", "gsis_id": "p1", "pos_rank": 3},
            {"season": 2025, "dt": "2025-09-05T00:00Z", "gsis_id": "p1", "pos_rank": 1},
            {"season": 2025, "dt": "2025-12-01T00:00Z", "gsis_id": "p1", "pos_rank": 2},
        ]
        out = rank_map_from_snapshots(frame, self._weeks(**{"1": "2025-09-07T00:00Z"}))
        # The December snapshot is in the future for week 1 and must not be used.
        self.assertEqual(out[(2025, 1, "p1")], 1.0)

    def test_a_later_week_sees_a_later_snapshot(self):
        frame = [
            {"season": 2025, "dt": "2025-09-05T00:00Z", "gsis_id": "p1", "pos_rank": 1},
            {"season": 2025, "dt": "2025-11-01T00:00Z", "gsis_id": "p1", "pos_rank": 3},
        ]
        out = rank_map_from_snapshots(
            frame, self._weeks(**{"1": "2025-09-07T00:00Z", "10": "2025-11-09T00:00Z"})
        )
        self.assertEqual(out[(2025, 1, "p1")], 1.0)
        self.assertEqual(out[(2025, 10, "p1")], 3.0)

    def test_a_week_before_any_snapshot_gets_nothing(self):
        frame = [
            {"season": 2025, "dt": "2025-10-01T00:00Z", "gsis_id": "p1", "pos_rank": 1},
        ]
        out = rank_map_from_snapshots(frame, self._weeks(**{"1": "2025-09-07T00:00Z"}))
        self.assertEqual(out, {})

    def test_best_rank_wins_within_one_snapshot(self):
        frame = [
            {"season": 2025, "dt": "2025-09-05T00:00Z", "gsis_id": "p1", "pos_rank": 4},
            {"season": 2025, "dt": "2025-09-05T00:00Z", "gsis_id": "p1", "pos_rank": 2},
        ]
        out = rank_map_from_snapshots(frame, self._weeks(**{"1": "2025-09-07T00:00Z"}))
        self.assertEqual(out[(2025, 1, "p1")], 2.0)

    def test_no_rows_yields_nothing(self):
        out = rank_map_from_snapshots([], self._weeks(**{"1": "2025-09-07T00:00Z"}))
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
