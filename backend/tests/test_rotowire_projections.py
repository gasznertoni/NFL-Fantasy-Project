"""Provenance tests for the FantasyPros/Rotowire consensus blend."""

import unittest

from rotowire_projections import (
    FANTASYPROS_SOURCE,
    ROTOWIRE_SOURCE,
    blend_consensus_projections,
    consensus_source_label,
)

CONFIG = {"linear": {"rec_yd": 0.1, "rec_td": 6, "reception": 1}}


def proj(rec_yd=100.0, reception=8.0):
    return {"source": "consensus", "projected_points": 0.0,
            "stat_line": {"rec_yd": rec_yd, "reception": reception}}


class TestBlendProvenance(unittest.TestCase):
    """Only part of the tier is genuinely blended -- the two feeds overlap but
    do not coincide -- so every entry has to say which sources fed it. A flat
    "FantasyPros + Rotowire" on the whole tier claims two sources agreed on
    players where only one had data."""

    def test_overlapping_player_records_both_sources(self):
        out = blend_consensus_projections({"a": proj()}, {"a": proj()}, CONFIG)
        self.assertEqual(out["a"]["contributing_sources"], [FANTASYPROS_SOURCE, ROTOWIRE_SOURCE])

    def test_fantasypros_only_player_records_one_source(self):
        out = blend_consensus_projections({"a": proj()}, {}, CONFIG)
        self.assertEqual(out["a"]["contributing_sources"], [FANTASYPROS_SOURCE])

    def test_rotowire_only_player_records_one_source(self):
        out = blend_consensus_projections({}, {"a": proj()}, CONFIG)
        self.assertEqual(out["a"]["contributing_sources"], [ROTOWIRE_SOURCE])

    def test_a_single_source_projection_is_otherwise_unchanged(self):
        original = proj(rec_yd=123.0)
        out = blend_consensus_projections({"a": original}, {}, CONFIG)
        self.assertEqual(out["a"]["stat_line"], original["stat_line"])

    def test_overlapping_stat_lines_are_averaged(self):
        out = blend_consensus_projections(
            {"a": proj(rec_yd=100.0, reception=10.0)},
            {"a": proj(rec_yd=50.0, reception=6.0)},
            CONFIG,
        )
        self.assertAlmostEqual(out["a"]["stat_line"]["rec_yd"], 75.0)
        self.assertAlmostEqual(out["a"]["stat_line"]["reception"], 8.0)
        # points recomputed through OUR formula from the averaged line
        self.assertAlmostEqual(out["a"]["projected_points"], 75.0 * 0.1 + 8.0)


class TestConsensusSourceLabel(unittest.TestCase):
    def test_joins_multiple_sources(self):
        self.assertEqual(
            consensus_source_label({"contributing_sources": ["FantasyPros", "Rotowire"]}),
            "FantasyPros + Rotowire",
        )

    def test_single_source(self):
        self.assertEqual(consensus_source_label({"contributing_sources": ["Rotowire"]}), "Rotowire")

    def test_none_without_provenance_so_the_caller_falls_back(self):
        self.assertIsNone(consensus_source_label({}))
        self.assertIsNone(consensus_source_label({"contributing_sources": []}))


if __name__ == "__main__":
    unittest.main()
