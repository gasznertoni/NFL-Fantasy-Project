"""Unit tests for kicker.py -- hand-built load_player_stats()-shaped rows
with known expected output, same pattern as test_scoring.py. No network."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicker import nflreadpy_kicker_row_to_stat_line  # noqa: E402


class TestFgMadeBucketAggregation(unittest.TestCase):
    def test_zero_to_thirtynine_buckets_sum_into_one_category(self):
        row = {"fg_made_0_19": 1, "fg_made_20_29": 1, "fg_made_30_39": 1}
        mapped = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(mapped["fg_made_0_39"], 3)

    def test_forty_to_fortynine_maps_directly(self):
        row = {"fg_made_40_49": 2}
        mapped = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(mapped["fg_made_40_49"], 2)

    def test_fifty_plus_buckets_sum_into_one_category(self):
        row = {"fg_made_50_59": 1, "fg_made_60_": 1}
        mapped = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(mapped["fg_made_50_plus"], 2)

    def test_real_shaped_multi_bucket_row(self):
        """Mirrors a real confirmed 2025 row (Andre Szmyt, week 6): three
        FG makes across two distance buckets in one game."""
        row = {"fg_made_0_19": 0, "fg_made_20_29": 0, "fg_made_30_39": 2, "fg_made_40_49": 0, "fg_made_50_59": 1}
        mapped = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(mapped["fg_made_0_39"], 2)
        self.assertEqual(mapped["fg_made_50_plus"], 1)
        self.assertNotIn("fg_made_40_49", mapped)


class TestBlockedKickFolding(unittest.TestCase):
    """Confirmed real finding: fg_att = fg_made + fg_missed + fg_blocked --
    a block is not counted within fg_missed upstream, but this league's
    placeholder config has no separate kicker-side blocked category, so
    it's folded into the miss category (see module docstring)."""

    def test_blocked_fg_folds_into_fg_missed(self):
        row = {"fg_missed": 0, "fg_blocked": 1}
        mapped = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(mapped["fg_missed"], 1)

    def test_normal_miss_and_block_both_count(self):
        row = {"fg_missed": 1, "fg_blocked": 1}
        mapped = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(mapped["fg_missed"], 2)

    def test_blocked_pat_folds_into_pat_missed(self):
        row = {"pat_missed": 0, "pat_blocked": 1}
        mapped = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(mapped["pat_missed"], 1)


class TestPatAndDirectFields(unittest.TestCase):
    def test_pat_made_maps_directly(self):
        row = {"pat_made": 3}
        mapped = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(mapped["pat_made"], 3)


class TestNonKickerRowsAndMissingValues(unittest.TestCase):
    def test_offense_only_row_returns_empty_dict(self):
        """A QB/RB/WR/TE row has none of these columns -- must return {},
        not raise, so this can be safely merged with
        scoring.nflreadpy_row_to_stat_line's output for every row without
        a position check."""
        row = {"passing_yards": 275, "passing_tds": 2}
        mapped = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(mapped, {})

    def test_zero_and_missing_values_are_skipped(self):
        row = {"fg_made_40_49": 0, "pat_made": None}
        mapped = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(mapped, {})

    def test_no_key_collisions_with_offense_column_map(self):
        """scoring.py's and kicker.py's output category names must stay
        disjoint so merging the two mappers' output for the same row (as
        generate_report.py's game-log loader does) can never silently
        overwrite one with the other."""
        from scoring import NFLREADPY_OFFENSE_COLUMN_MAP

        kicker_categories = {
            "fg_made_0_39", "fg_made_40_49", "fg_made_50_plus", "fg_missed", "pat_made", "pat_missed",
        }
        offense_our_categories = set(NFLREADPY_OFFENSE_COLUMN_MAP.values())
        self.assertEqual(kicker_categories & offense_our_categories, set())


if __name__ == "__main__":
    unittest.main()
