"""Unit tests for kicker.py -- hand-built load_player_stats()-shaped rows
with known expected output, same pattern as test_scoring.py. No network."""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicker import (  # noqa: E402
    nflreadpy_kicker_row_to_stat_line,
    validate_fg_band_family,
    validate_fg_miss_band_family,
)


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


class TestBandedMissedFieldGoals(unittest.TestCase):
    """league-1's final settings (2026-09-06) charge a missed FG by distance:
    -2 inside 40, -1 from 40-49, and nothing at all beyond 50. league-2 charges
    one flat -1. Both families are emitted; a config may use only one."""

    def test_short_misses_roll_up_into_one_band(self):
        row = {"fg_missed_0_19": 1, "fg_missed_20_29": 1, "fg_missed_30_39": 2}
        self.assertEqual(nflreadpy_kicker_row_to_stat_line(row)["fg_missed_0_39"], 4)

    def test_long_bands_pass_through_individually(self):
        row = {"fg_missed_40_49": 1, "fg_missed_50_59": 2, "fg_missed_60_": 1}
        out = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(out["fg_missed_40_49"], 1)
        self.assertEqual(out["fg_missed_50_59"], 2)
        self.assertEqual(out["fg_missed_60_plus"], 1)

    def test_the_flat_family_is_still_emitted_for_league_two(self):
        row = {"fg_missed": 2, "fg_blocked": 1}
        self.assertEqual(nflreadpy_kicker_row_to_stat_line(row)["fg_missed"], 3)

    def test_blocked_kicks_are_banded_by_their_own_distance(self):
        # fg_blocked_list is ";"-separated distances. fg_blocked_distance is
        # their SUM (80 for this row) and must not be read as a distance.
        row = {"fg_blocked": 2, "fg_blocked_list": "36;44", "fg_blocked_distance": 80}
        out = nflreadpy_kicker_row_to_stat_line(row)
        self.assertEqual(out["fg_missed_0_39"], 1)
        self.assertEqual(out["fg_missed_40_49"], 1)

    def test_blocked_kicks_are_dropped_rather_than_guessed_into_a_band(self):
        out = nflreadpy_kicker_row_to_stat_line({"fg_blocked": 1, "fg_blocked_list": None})
        self.assertNotIn("fg_missed_0_39", out)
        self.assertEqual(out["fg_missed"], 1)   # still counted in the flat family

    def test_banded_misses_are_absent_when_the_kicker_missed_nothing(self):
        out = nflreadpy_kicker_row_to_stat_line({"fg_made_40_49": 2})
        for band in ("fg_missed_0_39", "fg_missed_40_49", "fg_missed_50_59"):
            self.assertNotIn(band, out)

    def test_mixing_miss_families_raises(self):
        with self.assertRaises(ValueError):
            validate_fg_miss_band_family({"fg_missed": -1, "fg_missed_40_49": -1})

    def test_each_miss_family_alone_is_fine(self):
        validate_fg_miss_band_family({"fg_missed": -1})
        validate_fg_miss_band_family({"fg_missed_0_39": -2, "fg_missed_40_49": -1})

    def test_both_shipped_configs_pass_both_family_validators(self):
        root = Path(__file__).resolve().parents[1] / "leagues"
        for name in ("league-1", "league-2"):
            linear = json.loads((root / name / "scoring-config.json").read_text())["linear"]
            validate_fg_band_family(linear)
            validate_fg_miss_band_family(linear)


if __name__ == "__main__":
    unittest.main()
