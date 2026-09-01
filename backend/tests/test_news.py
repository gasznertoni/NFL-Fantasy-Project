"""Unit tests for news.py's non-network logic: article-to-player matching,
prompt building, and defensive validation of the LLM's JSON reply. The
actual ESPN fetch and a real Anthropic call are integration concerns
(network-dependent, confirmed unreachable from this build environment --
see news.py's module docstring) and are exercised with a fake client here
instead, not mocked HTTP."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import (  # noqa: E402
    DEFAULT_NEWS_FLAG,
    SUMMARY_FAILURES,
    _parse_json_object,
    articles_for_player,
    build_summary_prompt,
    summarize_player_news,
)


class FakeAnthropicClient:
    """Duck-types the one call path news.py uses
    (`client.messages.create(...).content[0].text`) so tests don't need the
    real `anthropic` package installed."""

    def __init__(self, reply_text=None, raise_error=None, empty_content=False):
        self.reply_text = reply_text
        self.raise_error = raise_error
        self.empty_content = empty_content
        self.last_call_kwargs = None
        self.messages = self

    def create(self, **kwargs):
        self.last_call_kwargs = kwargs
        if self.raise_error is not None:
            raise self.raise_error
        if self.empty_content:
            return FakeResponse(text=None, empty=True)
        return FakeResponse(self.reply_text)


class FakeResponse:
    def __init__(self, text, empty=False):
        self.content = [] if empty else [FakeContentBlock(text)]


class FakeContentBlock:
    def __init__(self, text):
        self.text = text


class TestArticleMatching(unittest.TestCase):
    def test_matches_by_athlete_id_category(self):
        articles = [
            {"headline": "Weekend roundup", "description": "", "categories": [{"type": "athlete", "athleteId": "4046"}]},
            {"headline": "Unrelated story", "description": "", "categories": []},
        ]
        matched = articles_for_player(articles, "Christian McCaffrey", player_espn_id="4046")
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["headline"], "Weekend roundup")

    def test_falls_back_to_full_name_substring_match(self):
        """Fallback requires full name (not last-name-only) to avoid spurious
        LLM calls for players sharing a common surname with unrelated articles."""
        articles = [
            {"headline": "Christian McCaffrey limited in practice", "description": "", "categories": []},
            {"headline": "Totally different player news", "description": "", "categories": []},
        ]
        matched = articles_for_player(articles, "Christian McCaffrey", player_espn_id=None)
        self.assertEqual(len(matched), 1)

    def test_last_name_only_no_longer_matches(self):
        """Last-name-only headlines do NOT trigger a match (intentional cost
        control -- last-name matching caused spurious LLM calls for common
        surnames like Brown/Williams/Hill with no injury-news benefit)."""
        articles = [
            {"headline": "McCaffrey limited in practice", "description": "", "categories": []},
        ]
        matched = articles_for_player(articles, "Christian McCaffrey", player_espn_id=None)
        self.assertEqual(matched, [])

    def test_no_match_returns_empty_list(self):
        articles = [{"headline": "Unrelated", "description": "nothing here", "categories": []}]
        matched = articles_for_player(articles, "Christian McCaffrey")
        self.assertEqual(matched, [])

    def test_blank_name_and_no_id_matches_nothing(self):
        articles = [
            {"headline": "Totally unrelated story", "description": "", "categories": []},
            {"headline": "Another story", "description": "", "categories": []},
        ]
        matched = articles_for_player(articles, "  ", player_espn_id=None)
        self.assertEqual(matched, [])

    def test_blank_name_with_id_only_matches_by_id(self):
        articles = [
            {"headline": "Weekend roundup", "description": "", "categories": [{"type": "athlete", "athleteId": "4046"}]},
            {"headline": "Unrelated story", "description": "", "categories": []},
        ]
        matched = articles_for_player(articles, "", player_espn_id="4046")
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["headline"], "Weekend roundup")


class TestPromptBuilding(unittest.TestCase):
    def test_prompt_includes_player_name_and_article_text(self):
        articles = [{"headline": "Player X questionable", "description": "ankle injury"}]
        prompt = build_summary_prompt("Player X", articles)
        self.assertIn("Player X", prompt)
        self.assertIn("ankle injury", prompt)
        self.assertIn("JSON", prompt)


class TestSummarizePlayerNews(unittest.TestCase):
    def test_no_articles_short_circuits_without_calling_llm(self):
        client = FakeAnthropicClient(reply_text="should never be read")
        result = summarize_player_news("Player X", [], client)
        self.assertEqual(result, DEFAULT_NEWS_FLAG)
        self.assertIsNone(client.last_call_kwargs)

    def test_valid_llm_reply_is_passed_through(self):
        reply = json.dumps({"designation": "Questionable", "riskLevel": "medium", "summary": "Limited in practice with ankle injury."})
        client = FakeAnthropicClient(reply_text=reply)
        result = summarize_player_news("Player X", [{"headline": "h", "description": "d"}], client)
        self.assertEqual(result["designation"], "Questionable")
        self.assertEqual(result["riskLevel"], "medium")
        self.assertIn("ankle", result["summary"])

    def test_invalid_designation_falls_back_to_default(self):
        reply = json.dumps({"designation": "probably fine", "riskLevel": "medium", "summary": "..."})
        client = FakeAnthropicClient(reply_text=reply)
        result = summarize_player_news("Player X", [{"headline": "h", "description": "d"}], client)
        self.assertEqual(result, DEFAULT_NEWS_FLAG)

    def test_malformed_json_falls_back_to_default(self):
        client = FakeAnthropicClient(reply_text="not json at all")
        result = summarize_player_news("Player X", [{"headline": "h", "description": "d"}], client)
        self.assertEqual(result, DEFAULT_NEWS_FLAG)

    def test_non_string_summary_is_coerced_to_none(self):
        reply = json.dumps({"designation": "Healthy", "riskLevel": "none", "summary": 12345})
        client = FakeAnthropicClient(reply_text=reply)
        result = summarize_player_news("Player X", [{"headline": "h", "description": "d"}], client)
        self.assertIsNone(result["summary"])

    def test_api_call_exception_falls_back_to_default(self):
        client = FakeAnthropicClient(raise_error=RuntimeError("rate limited"))
        result = summarize_player_news("Player X", [{"headline": "h", "description": "d"}], client)
        self.assertEqual(result, DEFAULT_NEWS_FLAG)

    def test_empty_content_response_falls_back_to_default(self):
        client = FakeAnthropicClient(empty_content=True)
        result = summarize_player_news("Player X", [{"headline": "h", "description": "d"}], client)
        self.assertEqual(result, DEFAULT_NEWS_FLAG)


class TestJsonFenceParsing(unittest.TestCase):
    """Models wrap JSON in a markdown fence even when told not to. A bare
    json.loads on that raises, summarize_player_news swallowed the raise, and
    the result was indistinguishable from "no newsworthy players" -- every LLM
    summary in every report was empty until 2026-09-01. These pin the fix."""

    def test_parses_a_json_fence(self):
        self.assertEqual(_parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})

    def test_parses_an_unlabelled_fence(self):
        self.assertEqual(_parse_json_object('```\n{"a": 1}\n```'), {"a": 1})

    def test_parses_bare_json(self):
        self.assertEqual(_parse_json_object('{"a": 1}'), {"a": 1})

    def test_parses_json_with_commentary_around_it(self):
        self.assertEqual(_parse_json_object('Sure! {"a": 1} hope that helps'), {"a": 1})

    def test_tolerates_surrounding_whitespace(self):
        self.assertEqual(_parse_json_object('\n\n  {"a": 1}  \n'), {"a": 1})

    def test_raises_on_genuinely_unparseable_text(self):
        with self.assertRaises(Exception):
            _parse_json_object("no json here at all")


class TestSummarizationFailureVisibility(unittest.TestCase):
    """A per-player degrade is only safe if someone can see how often it
    fires. A 100% failure rate previously looked exactly like success."""

    def setUp(self):
        SUMMARY_FAILURES.clear()

    def tearDown(self):
        SUMMARY_FAILURES.clear()

    def test_a_fenced_response_now_succeeds_and_records_no_failure(self):
        class FakeClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    class R:
                        content = [type("T", (), {"text": '```json\n{"designation": "Questionable", "riskLevel": "medium", "summary": "Limited in practice."}\n```'})()]
                    return R()
        flag = summarize_player_news("A Player", [{"headline": "h", "description": "d"}], FakeClient())
        self.assertEqual(flag["designation"], "Questionable")
        self.assertEqual(flag["summary"], "Limited in practice.")
        self.assertEqual(SUMMARY_FAILURES, [])

    def test_a_real_failure_is_recorded_not_swallowed(self):
        class BoomClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("rate limited")
        flag = summarize_player_news("A Player", [{"headline": "h", "description": "d"}], BoomClient())
        self.assertEqual(flag, dict(DEFAULT_NEWS_FLAG))
        self.assertEqual(len(SUMMARY_FAILURES), 1)
        self.assertIn("A Player", SUMMARY_FAILURES[0])

    def test_no_articles_is_not_a_failure(self):
        flag = summarize_player_news("A Player", [], object())
        self.assertEqual(flag, dict(DEFAULT_NEWS_FLAG))
        self.assertEqual(SUMMARY_FAILURES, [])


if __name__ == "__main__":
    unittest.main()
