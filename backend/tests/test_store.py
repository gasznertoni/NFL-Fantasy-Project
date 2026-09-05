"""Tests for store.py -- phases 0 and 1 of the shared data store.

The Postgres backend is NOT exercised here: it needs a live database, and the
whole point of the design is that the pipeline works without one. What IS
tested is the property that makes that safe -- with no environment set, every
read misses and every write is dropped, so behaviour is identical to before
this module existed.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store import (  # noqa: E402
    FileStore, NullStore, Store, config_hash, content_hash,
    open_store, pack_bundle, unpack_bundle,
)


class TestDisabledByDefault(unittest.TestCase):
    """Optional by construction -- design doc open question 2."""

    def test_no_environment_gives_a_null_store(self):
        store = open_store(database_url="", cache_dir="", quiet=True)
        self.assertIsInstance(store, NullStore)
        self.assertFalse(store.enabled)

    def test_every_read_misses_and_every_write_is_dropped(self):
        store = open_store(database_url="", cache_dir="", quiet=True)
        store.put_news_summary("p", "h", "s", "Out", "high", "m")
        self.assertIsNone(store.get_news_summary("p", "h"))
        store.put_model_fit(2026, 1, "l", "c", "calibration", {"a": 1})
        self.assertIsNone(store.get_model_fit(2026, 1, "l", "c", "calibration"))
        self.assertEqual(store.get_player_games(2026, "nflreadpy"), {})
        self.assertIsNone(store.start_run(2026, 1, None, None))
        self.assertEqual(store.write_projections(None, "l", 2026, 1, [{"player_id": "p"}]), 0)

    def test_unreachable_database_falls_back_rather_than_raising(self):
        """A scheduled Saturday report is not the place to discover the
        database is down."""
        store = open_store(database_url="postgresql://nobody@127.0.0.1:1/none", quiet=True)
        self.assertFalse(store.enabled)


class TestHashing(unittest.TestCase):
    def test_key_order_does_not_change_the_hash(self):
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))

    def test_different_content_changes_the_hash(self):
        self.assertNotEqual(content_hash({"a": 1}), content_hash({"a": 2}))

    def test_non_json_values_do_not_raise(self):
        from datetime import datetime

        self.assertTrue(content_hash({"when": datetime(2026, 9, 1)}))

    def test_config_hash_ignores_prose_fields(self):
        """The scoring configs carry large _note/_unconfirmed blocks that
        document provenance and change without changing a scoring rule. If
        those moved the hash, every doc edit would invalidate every fit."""
        a = {"linear": {"pass_td": 6}, "_note": "written Tuesday"}
        b = {"linear": {"pass_td": 6}, "_note": "rewritten Friday", "_unconfirmed": ["x"]}
        self.assertEqual(config_hash(a), config_hash(b))

    def test_config_hash_tracks_real_rule_changes(self):
        self.assertNotEqual(
            config_hash({"linear": {"pass_td": 6}}),
            config_hash({"linear": {"pass_td": 4}}),
        )

    def test_config_hash_tracks_model_params(self):
        cfg = {"linear": {"pass_td": 6}}
        self.assertNotEqual(config_hash(cfg, window=8), config_hash(cfg, window=6))


class TestFileStore(unittest.TestCase):
    def setUp(self):
        self.store = FileStore(Path(tempfile.mkdtemp()))

    def test_news_summary_round_trips_with_its_designation(self):
        """The design doc's schema omitted `designation`. Caching without it
        would return 'Healthy' on every hit for a player Sleeper does not
        cover -- a cache that quietly downgrades an injury flag."""
        self.store.put_news_summary("p1", "h1", "Knee.", "Questionable", "low", "model-x")
        hit = self.store.get_news_summary("p1", "h1")
        self.assertEqual(hit["summary"], "Knee.")
        self.assertEqual(hit["designation"], "Questionable")
        self.assertEqual(hit["risk_level"], "low")

    def test_unknown_key_misses(self):
        self.assertIsNone(self.store.get_news_summary("p1", "nope"))

    def test_model_fit_is_shared_across_leagues_with_the_same_config(self):
        """This is the per-league double-fit fix: the lookup keys on the
        config hash and ignores league_id."""
        self.store.put_model_fit(2026, 1, "league-1", "cfg-abc", "calibration", {"slope": 1.02})
        got = self.store.get_model_fit(2026, 1, "league-2", "cfg-abc", "calibration")
        self.assertEqual(got, {"slope": 1.02})

    def test_a_different_config_does_not_collide(self):
        self.store.put_model_fit(2026, 1, "league-1", "cfg-abc", "calibration", {"slope": 1.02})
        self.assertIsNone(self.store.get_model_fit(2026, 1, "league-1", "cfg-xyz", "calibration"))

    def test_player_games_round_trip(self):
        self.store.put_player_games(
            [{"player_id": "p1", "season": 2026, "week": 3, "stat_line": {"rec_yd": 80}}],
            "nflreadpy",
        )
        self.assertEqual(
            self.store.get_player_games(2026, "nflreadpy")[("p1", 3)], {"rec_yd": 80}
        )

    def test_file_backend_keeps_no_prediction_log(self):
        """Phase 0 fixes addressing, not durability. An append-only log on one
        laptop's filesystem is the thing the design doc says file-shaped
        storage cannot provide, so this stays a no-op rather than becoming a
        second, weaker source of truth."""
        self.assertIsNone(self.store.start_run(2026, 1, "sha", "cfg"))
        self.assertEqual(self.store.write_projections(None, "l", 2026, 1, [{"player_id": "p"}]), 0)

    def test_corrupt_cache_file_reads_as_a_miss(self):
        path = self.store.root / "news" / "p1-h1.json"
        path.write_text("{ this is not json")
        self.assertIsNone(self.store.get_news_summary("p1", "h1"))


class TestBundlePacking(unittest.TestCase):
    def test_fitted_models_survive_a_json_round_trip(self):
        from calibration import IntervalModel

        model = IntervalModel().fit(
            [
                {"position": "WR", "projected": 1.0 + (i % 20),
                 "actual": 1.0 + (i % 20) + ((i * 37) % 21 - 10) / 10.0}
                for i in range(800)
            ]
        )
        bundle = {"affines": {"WR": (0.1, 1.0)}, "interval_model": model, "blend_model": None}
        restored = unpack_bundle(json.loads(json.dumps(pack_bundle(bundle))))

        self.assertIsInstance(restored["interval_model"], IntervalModel)
        self.assertEqual(
            restored["interval_model"].interval("WR", 10.0), model.interval("WR", 10.0)
        )

    def test_mixture_interval_survives_the_round_trip(self):
        """JSON turns every tuple into a list. The mixture path indexes the
        stored quantile grid, so it is the sharpest test that lists are as
        good as tuples here."""
        from calibration import IntervalModel

        model = IntervalModel().fit(
            [
                {"position": "WR", "projected": 1.0 + (i % 20),
                 "actual": 1.0 + (i % 20) + ((i * 37) % 21 - 10) / 10.0}
                for i in range(800)
            ]
        )
        restored = unpack_bundle(json.loads(json.dumps(pack_bundle({"m": model}))))["m"]
        self.assertEqual(
            restored.interval("WR", 10.0, play_probability=0.7),
            model.interval("WR", 10.0, play_probability=0.7),
        )

    def test_plain_values_pass_through_untouched(self):
        bundle = {"shrinkage_ks": {"WR": 1.3}, "blend_model": None}
        self.assertEqual(unpack_bundle(pack_bundle(bundle)), bundle)

    def test_unknown_class_degrades_to_none_rather_than_raising(self):
        packed = {"m": {"__packed_model__": "SomeFutureModel", "state": {"x": 1}}}
        self.assertIsNone(unpack_bundle(packed)["m"])


class TestNewsCaching(unittest.TestCase):
    """news.summarize_player_news is where the store saves the most: 167 of
    936 players in a week-1 fixture carry an LLM summary."""

    class _Client:
        def __init__(self):
            self.calls = 0
            outer = self

            class _Messages:
                @staticmethod
                def create(**_kwargs):
                    outer.calls += 1
                    payload = ('{"designation":"Questionable","riskLevel":"low",'
                               '"summary":"Knee, limited practice."}')
                    return type("R", (), {"content": [type("T", (), {"text": payload})()]})()

            self.messages = _Messages()

    def test_second_identical_call_is_free_and_identical(self):
        from news import summarize_player_news

        store = FileStore(Path(tempfile.mkdtemp()))
        client = self._Client()
        articles = [{"headline": "X hurt", "description": "knee"}]

        first = summarize_player_news("X", articles, client, store=store, player_id="p9")
        second = summarize_player_news("X", articles, client, store=store, player_id="p9")

        self.assertEqual(first, second)
        self.assertEqual(first["designation"], "Questionable")
        self.assertEqual(client.calls, 1)

    def test_without_a_player_id_the_cache_is_skipped(self):
        from news import summarize_player_news

        store = FileStore(Path(tempfile.mkdtemp()))
        client = self._Client()
        articles = [{"headline": "X hurt", "description": "knee"}]
        summarize_player_news("X", articles, client, store=store)
        summarize_player_news("X", articles, client, store=store)
        self.assertEqual(client.calls, 2)

    def test_a_degraded_result_is_never_cached(self):
        """Caching a failure would make one bad API call permanent."""
        from news import summarize_player_news

        class Broken:
            class messages:
                @staticmethod
                def create(**_kwargs):
                    raise RuntimeError("rate limited")

        store = FileStore(Path(tempfile.mkdtemp()))
        articles = [{"headline": "X hurt", "description": "knee"}]
        summarize_player_news("X", articles, Broken, store=store, player_id="p9")

        good = self._Client()
        flag = summarize_player_news("X", articles, good, store=store, player_id="p9")
        self.assertEqual(good.calls, 1)
        self.assertEqual(flag["summary"], "Knee, limited practice.")


if __name__ == "__main__":
    unittest.main()
