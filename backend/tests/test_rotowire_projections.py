"""Provenance tests for the FantasyPros/Rotowire consensus blend."""

import unittest

from rotowire_projections import (
    FANTASYPROS_SOURCE,
    ROTOWIRE_SOURCE,
    blend_consensus_projections,
    consensus_source_label,
    rotowire_stats_to_stat_line,
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


# league-1 from 2026-09-06: a TE reception is worth double an RB/WR one.
TE_PREMIUM = {"linear": {"rec_yd": 0.1, "rec_td": 6,
                         "reception": {"TE": 1, "default": 0.5}}}


class TestBlendCarriesPosition(unittest.TestCase):
    """The blend scored its averaged stat line without a position until
    2026-09-06, which under TE-premium scoring quietly cost every blended tight
    end half a point per catch."""

    def _sided(self, position):
        one = {"source": "consensus", "position": position, "projected_points": 0.0,
               "stat_line": {"rec_yd": 100.0, "reception": 8.0}}
        two = {"source": "consensus", "position": position, "projected_points": 0.0,
               "stat_line": {"rec_yd": 100.0, "reception": 8.0}}
        return blend_consensus_projections({"p": one}, {"p": two}, TE_PREMIUM)["p"]

    def test_a_blended_tight_end_gets_the_reception_premium(self):
        self.assertAlmostEqual(self._sided("TE")["projected_points"], 18.0)

    def test_a_blended_receiver_does_not(self):
        self.assertAlmostEqual(self._sided("WR")["projected_points"], 14.0)

    def test_the_blended_entry_reports_its_position(self):
        self.assertEqual(self._sided("TE")["position"], "TE")

    def test_position_is_taken_from_whichever_feed_has_it(self):
        one = {"source": "consensus", "projected_points": 0.0,
               "stat_line": {"rec_yd": 100.0, "reception": 8.0}}
        two = {"source": "consensus", "position": "TE", "projected_points": 0.0,
               "stat_line": {"rec_yd": 100.0, "reception": 8.0}}
        self.assertAlmostEqual(
            blend_consensus_projections({"p": one}, {"p": two}, TE_PREMIUM)["p"]["projected_points"],
            18.0)

    def test_a_non_numeric_stat_line_key_does_not_break_averaging(self):
        one = {"source": "consensus", "position": "WR", "projected_points": 0.0,
               "stat_line": {"rec_yd": 100.0, "position": "WR"}}
        two = {"source": "consensus", "position": "WR", "projected_points": 0.0,
               "stat_line": {"rec_yd": 60.0, "position": "WR"}}
        out = blend_consensus_projections({"p": one}, {"p": two}, TE_PREMIUM)["p"]
        self.assertAlmostEqual(out["stat_line"]["rec_yd"], 80.0)
        self.assertEqual(out["stat_line"]["position"], "WR")


class TestRotowireIncompletions(unittest.TestCase):
    """Rotowire publishes attempts AND completions, so unlike FantasyPros its
    incompletions are exact rather than estimated."""

    def test_incompletions_are_attempts_minus_completions(self):
        line = rotowire_stats_to_stat_line(
            {"offpassyard": 280, "offpasscomp": 24, "offpassatt": 36}, "QB")
        self.assertEqual(line["pass_completion"], 24)
        self.assertEqual(line["pass_incompletion"], 12)

    def test_attempts_are_consumed_and_not_left_in_the_stat_line(self):
        line = rotowire_stats_to_stat_line({"offpasscomp": 24, "offpassatt": 36}, "QB")
        self.assertNotIn("pass_att", line)

    def test_a_skill_player_has_no_passing_line(self):
        line = rotowire_stats_to_stat_line({"offrecatt": 6, "offrecyard": 70}, "WR")
        self.assertNotIn("pass_incompletion", line)


if __name__ == "__main__":
    unittest.main()
