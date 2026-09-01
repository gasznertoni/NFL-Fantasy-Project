"""Unit tests for backend/espn_injuries.py.

fetch_injury_rows is NOT covered -- it needs the network. Its live behaviour
(800 rows, 449 matched to a 904-player pool) is recorded in the module
docstring.
"""

import unittest

from espn_injuries import (
    STATUS_TO_REPORT_STATUS,
    _athlete_espn_id,
    index_by_player,
    news_articles_from_injury,
    parse_injury_payload,
)


def athlete(name="Jane Doe", espn_id="12345", position="WR"):
    links = (
        [{"href": f"https://www.espn.com/nfl/player/_/id/{espn_id}/slug"}] if espn_id else []
    )
    return {"displayName": name, "links": links, "position": {"abbreviation": position}}


def payload(*entries, team="Arizona Cardinals"):
    return {"injuries": [{"id": "22", "displayName": team, "injuries": list(entries)}]}


def entry(name="Jane Doe", espn_id="12345", status="Questionable", **kw):
    return {
        "athlete": athlete(name, espn_id),
        "status": status,
        "date": kw.get("date", "2026-09-01T00:00Z"),
        "shortComment": kw.get("short", "short text"),
        "longComment": kw.get("long", "long text"),
        "type": {"description": kw.get("detail", "Ankle")},
    }


class TestStatusMapping(unittest.TestCase):
    def test_active_is_not_a_designation(self):
        # A player is on this page because there is NEWS about them, not
        # because they are doubtful. Treating Active as a designation would
        # penalise everyone ESPN happens to have written about -- 411 of the
        # 800 live entries.
        self.assertIsNone(STATUS_TO_REPORT_STATUS["Active"])

    def test_unavailable_statuses_collapse_to_out(self):
        for status in ("Out", "Injured Reserve", "Suspension"):
            self.assertEqual(STATUS_TO_REPORT_STATUS[status], "Out")

    def test_uncertain_statuses_keep_their_own_level(self):
        self.assertEqual(STATUS_TO_REPORT_STATUS["Questionable"], "Questionable")
        self.assertEqual(STATUS_TO_REPORT_STATUS["Doubtful"], "Doubtful")

    def test_every_mapped_status_is_one_availability_knows(self):
        from availability import REPORT_STATUSES

        for mapped in STATUS_TO_REPORT_STATUS.values():
            if mapped is not None:
                self.assertIn(mapped, REPORT_STATUSES)


class TestAthleteEspnId(unittest.TestCase):
    """athlete.id is null on every live entry, so the id comes out of the
    profile URL. That is fragile by nature and must fail soft."""

    def test_extracts_the_id_from_a_profile_link(self):
        self.assertEqual(_athlete_espn_id(athlete(espn_id="4870808")), "4870808")

    def test_returns_none_without_links(self):
        self.assertIsNone(_athlete_espn_id({"displayName": "x"}))

    def test_returns_none_when_no_link_carries_an_id(self):
        self.assertIsNone(_athlete_espn_id({"links": [{"href": "https://espn.com/nfl/"}]}))

    def test_survives_a_null_href(self):
        self.assertIsNone(_athlete_espn_id({"links": [{"href": None}]}))


class TestParseInjuryPayload(unittest.TestCase):
    def test_flattens_team_groups_into_rows(self):
        rows = parse_injury_payload(payload(entry("A", "1"), entry("B", "2")))
        self.assertEqual([r["name"] for r in rows], ["A", "B"])
        self.assertEqual(rows[0]["team"], "Arizona Cardinals")

    def test_maps_status_to_a_report_status(self):
        rows = parse_injury_payload(payload(entry(status="Injured Reserve")))
        self.assertEqual(rows[0]["status"], "Injured Reserve")
        self.assertEqual(rows[0]["report_status"], "Out")

    def test_carries_the_commentary_through(self):
        rows = parse_injury_payload(
            payload(entry(short="spotted at practice", long="high-ankle sprain since week 1"))
        )
        self.assertEqual(rows[0]["short_comment"], "spotted at practice")
        self.assertEqual(rows[0]["long_comment"], "high-ankle sprain since week 1")
        self.assertEqual(rows[0]["detail"], "Ankle")

    def test_entries_without_a_name_are_skipped(self):
        bad = {"athlete": {"links": []}, "status": "Out"}
        self.assertEqual(parse_injury_payload(payload(bad)), [])

    def test_empty_and_malformed_payloads_yield_no_rows(self):
        self.assertEqual(parse_injury_payload({}), [])
        self.assertEqual(parse_injury_payload({"injuries": []}), [])
        self.assertEqual(parse_injury_payload({"injuries": [{"displayName": "T"}]}), [])

    def test_an_unknown_status_maps_to_no_designation(self):
        rows = parse_injury_payload(payload(entry(status="Probable-ish")))
        self.assertIsNone(rows[0]["report_status"])


class TestIndexByPlayer(unittest.TestCase):
    def setUp(self):
        self.rows = parse_injury_payload(
            payload(entry("Jane Doe", "111"), entry("John Roe", "222"))
        )

    def test_matches_on_espn_id(self):
        out = index_by_player(self.rows, [{"playerId": "p1", "name": "Someone Else", "espnId": 111}])
        self.assertEqual(out["p1"]["name"], "Jane Doe")

    def test_falls_back_to_a_case_insensitive_name(self):
        out = index_by_player(self.rows, [{"playerId": "p2", "name": "  JOHN ROE "}])
        self.assertEqual(out["p2"]["name"], "John Roe")

    def test_espn_id_wins_over_name(self):
        out = index_by_player(self.rows, [{"playerId": "p3", "name": "John Roe", "espnId": 111}])
        self.assertEqual(out["p3"]["name"], "Jane Doe")

    def test_unlisted_players_are_absent_not_none(self):
        out = index_by_player(self.rows, [{"playerId": "p4", "name": "Nobody"}])
        self.assertEqual(out, {})

    def test_a_player_without_an_espn_id_still_matches_by_name(self):
        out = index_by_player(self.rows, [{"playerId": "p5", "name": "Jane Doe", "espnId": None}])
        self.assertEqual(out["p5"]["name"], "Jane Doe")


class TestNewsArticlesFromInjury(unittest.TestCase):
    """The commentary is reshaped into news.py's article shape so the LLM
    layer can summarize it -- per-player and already on topic, unlike the
    general news feed which name-matches only ~83 of a 904-player pool."""

    def test_builds_one_article_with_status_and_detail_in_the_headline(self):
        row = parse_injury_payload(payload(entry(status="Out", detail="Knee")))[0]
        articles = news_articles_from_injury(row)
        self.assertEqual(len(articles), 1)
        self.assertIn("Out", articles[0]["headline"])
        self.assertIn("Knee", articles[0]["headline"])

    def test_uses_the_long_comment_as_the_body(self):
        row = parse_injury_payload(payload(entry(long="tore his ACL in camp")))[0]
        self.assertEqual(news_articles_from_injury(row)[0]["description"], "tore his ACL in camp")

    def test_falls_back_to_the_short_comment_when_there_is_no_long_one(self):
        row = parse_injury_payload(payload(entry(short="limited", long=None)))[0]
        self.assertEqual(news_articles_from_injury(row)[0]["description"], "limited")

    def test_an_entry_with_no_text_produces_nothing(self):
        row = parse_injury_payload(payload(entry(short=None, long=None, detail=None, status=None)))[0]
        self.assertEqual(news_articles_from_injury(row), [])


if __name__ == "__main__":
    unittest.main()
