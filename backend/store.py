"""
The pipeline's cache and prediction log, behind one swappable interface.

Implements phases 0 and 1 of docs/design/shared-data-store.md. Read that
first -- it argues *why*, and the argument is not the obvious one. The
headline reason is not caching:

    generate_track_record.py sources the prediction log from the same
    committed fixtures generate_report.py overwrites, so the eval layer's
    ground truth is a mutable file the pipeline rewrites.

Regenerate week 5 in November and October's prediction is silently replaced
by what today's model says, then graded against October's outcomes and
reported as season-long accuracy. `projections` here is append-only, which
is the fix; the caching wins below are real but secondary.

Three backends, chosen by environment, never by a caller:

    DATABASE_URL set          PostgresStore  phase 1 -- shared, durable
    FANTASY_STORE_DIR set     FileStore      phase 0 -- content-addressed on disk
    neither                   NullStore      today's behaviour, exactly

**Optional by construction.** That is open question 2 in the design doc,
settled here in favour of optional: with no environment set, every read
misses and every write is dropped, so the pipeline behaves precisely as it
did before this module existed. The cost is a little indirection; the buy is
a pipeline that still runs on a plane, and a test suite that needs no
database. It also means a store outage degrades to "recompute", never to
"crash", which is the same never-take-down-the-pipeline contract news.py
already keeps.

Nothing here raises into the pipeline. Every method swallows backend errors
and returns the miss/no-op answer, because a cache that can fail the run it
is meant to speed up is worse than no cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# Bump when a cached artefact's *meaning* changes in a way the key does not
# already capture. Part of every content hash below, so one edit here
# invalidates every cache entry rather than requiring a manual flush.
CACHE_SCHEMA_VERSION = "1"

# Model-fit kinds, matching the design doc's `model_fits.kind` column.
FIT_KINDS = ("availability", "calibration", "blend", "week1")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(*parts: Any) -> str:
    """A stable sha256 over arbitrary JSON-able parts.

    `sort_keys` matters: a dict that round-trips through JSON with different
    key order must hash the same, or every run misses its own cache. So does
    `default=str` -- a stray datetime or Path in a config should degrade to a
    stable string rather than raising and silently disabling the cache.
    """
    payload = json.dumps(
        [CACHE_SCHEMA_VERSION, *parts], sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def config_hash(scoring_config: dict[str, Any], **model_params: Any) -> str:
    """The key that makes the per-league double-fit disappear.

    `_build_calibration_for_config` and `_build_week1_for_config` are called
    inside generate_report.py's per-league loop, so with two leagues every fit
    runs twice. Two leagues sharing a scoring config hash to the same value and
    hit the same cached bundle -- which is the whole point of keying on the
    config rather than on the league id.

    Underscore-prefixed keys are dropped: the configs carry large `_note` and
    `_unconfirmed` prose blocks that document provenance and change without
    changing a single scoring rule.
    """
    scoring = {k: v for k, v in (scoring_config or {}).items() if not k.startswith("_")}
    return content_hash(scoring, model_params)


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
class Store:
    """No-op base class, and the live contract.

    Subclasses override; anything they do not override keeps this behaviour,
    which is exactly the pre-store pipeline. Deliberately a base class rather
    than a Protocol so `NullStore` is literally this and cannot drift from it.
    """

    enabled = False
    backend = "null"

    # -- caches ------------------------------------------------------------
    def get_news_summary(self, player_id: str, article_hash: str) -> Optional[dict[str, Any]]:
        return None

    def put_news_summary(
        self, player_id: str, article_hash: str, summary: str,
        designation: str, risk_level: str, model: str,
    ) -> None:
        return None

    def get_model_fit(
        self, season: int, week: int, league_id: str, cfg_hash: str, kind: str
    ) -> Optional[dict[str, Any]]:
        return None

    def put_model_fit(
        self, season: int, week: int, league_id: str, cfg_hash: str, kind: str,
        bundle: dict[str, Any],
    ) -> None:
        return None

    def get_player_games(self, season: int, source: str) -> dict[tuple[str, int], dict[str, Any]]:
        return {}

    def put_player_games(self, rows: Iterable[dict[str, Any]], source: str) -> None:
        return None

    # -- prediction log ----------------------------------------------------
    def start_run(
        self, season: int, week: int, git_sha: Optional[str], cfg_hash: Optional[str]
    ) -> Optional[str]:
        return None

    def finish_run(self, run_id: Optional[str], status: str) -> None:
        return None

    def write_projections(
        self, run_id: Optional[str], league_id: str, season: int, week: int,
        rows: Iterable[dict[str, Any]],
    ) -> int:
        return 0

    def upsert_players(self, rows: Iterable[dict[str, Any]]) -> None:
        return None

    def close(self) -> None:
        return None


class NullStore(Store):
    """Explicit name for the disabled case, so a log line can say which
    backend is live without printing 'Store'."""


# ---------------------------------------------------------------------------
# Phase 0 -- content-addressed filesystem
# ---------------------------------------------------------------------------
class FileStore(Store):
    """Phase 0: fixes the ADDRESSING without any infrastructure.

    The design doc's point is that this is worth shipping even if Postgres
    never happens -- it removes the per-league double-fit and makes a same-day
    re-run nearly free -- and that having done it, phase 1 is a backend swap
    rather than a rewrite.

    Caches only. `start_run` / `write_projections` stay no-ops here on purpose:
    an append-only prediction log on one laptop's filesystem is the very thing
    the design doc says file-shaped storage cannot provide, and pretending
    otherwise would create a second, weaker source of truth.
    """

    enabled = True
    backend = "file"

    def __init__(self, root: Path):
        self.root = Path(root)
        for sub in ("news", "fits", "games"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def _read(self, path: Path) -> Optional[dict[str, Any]]:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename: a killed run must not leave a truncated file
            # that later parses as a cache hit.
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, default=str))
            tmp.replace(path)
        except OSError:
            pass

    def get_news_summary(self, player_id, article_hash):
        return self._read(self.root / "news" / f"{player_id}-{article_hash}.json")

    def put_news_summary(self, player_id, article_hash, summary, designation, risk_level, model):
        self._write(
            self.root / "news" / f"{player_id}-{article_hash}.json",
            {"player_id": player_id, "article_hash": article_hash, "summary": summary,
             "designation": designation, "risk_level": risk_level, "model": model,
             "created_at": _now()},
        )

    def get_model_fit(self, season, week, league_id, cfg_hash, kind):
        # league_id is deliberately NOT in the path: two leagues with the same
        # scoring config share the bundle, which is the double-fit fix.
        got = self._read(self.root / "fits" / f"{season}-{week}-{kind}-{cfg_hash}.json")
        return None if got is None else got.get("bundle")

    def put_model_fit(self, season, week, league_id, cfg_hash, kind, bundle):
        self._write(
            self.root / "fits" / f"{season}-{week}-{kind}-{cfg_hash}.json",
            {"season": season, "week": week, "kind": kind, "config_hash": cfg_hash,
             "bundle": bundle, "fitted_at": _now()},
        )

    def get_player_games(self, season, source):
        got = self._read(self.root / "games" / f"{season}-{source}.json") or {}
        out: dict[tuple[str, int], dict[str, Any]] = {}
        for key, line in got.items():
            player_id, _, week = key.rpartition(":")
            try:
                out[(player_id, int(week))] = line
            except ValueError:
                continue
        return out

    def put_player_games(self, rows, source):
        by_season: dict[int, dict[str, Any]] = {}
        for row in rows:
            season = row.get("season")
            if season is None or not row.get("player_id"):
                continue
            by_season.setdefault(int(season), {})[
                f"{row['player_id']}:{int(row.get('week', 0))}"
            ] = row.get("stat_line", {})
        for season, payload in by_season.items():
            path = self.root / "games" / f"{season}-{source}.json"
            merged = {**(self._read(path) or {}), **payload}
            self._write(path, merged)


# ---------------------------------------------------------------------------
# Phase 1 -- Postgres
# ---------------------------------------------------------------------------
class PostgresStore(Store):
    """Phase 1: the same interface, backed by the schema in the design doc.

    This is the phase that fixes both the cold-runner cost and the mutable
    prediction log. `projections` is APPEND ONLY -- never updated, never
    deleted. The design doc is explicit that this should be enforced by a
    revoked UPDATE/DELETE grant on the pipeline's role rather than by
    discipline; migrations/001_shared_data_store.sql ships those grants.

    psycopg is imported lazily so the dependency stays optional: a checkout
    without it, or with DATABASE_URL unset, selects a different backend and
    never touches this class.
    """

    enabled = True
    backend = "postgres"

    def __init__(self, dsn: str):
        import psycopg  # noqa: F401  -- fail here, not at module import

        self.dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=True)

    def _exec(self, sql: str, params: tuple = (), fetch: str = "none"):
        """One place where every backend error is swallowed.

        A cache that can fail the run it exists to speed up is worse than no
        cache, so this returns the miss answer rather than propagating."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            return None

    # -- caches ------------------------------------------------------------
    def get_news_summary(self, player_id, article_hash):
        row = self._exec(
            "select summary, designation, risk_level, model from news_summaries"
            " where player_id = %s and article_hash = %s",
            (player_id, article_hash), fetch="one",
        )
        if not row:
            return None
        return {"summary": row[0], "designation": row[1], "risk_level": row[2], "model": row[3]}

    def put_news_summary(self, player_id, article_hash, summary, designation, risk_level, model):
        self._exec(
            "insert into news_summaries"
            " (player_id, article_hash, summary, designation, risk_level, model, created_at)"
            " values (%s, %s, %s, %s, %s, %s, %s)"
            " on conflict (player_id, article_hash) do nothing",
            (player_id, article_hash, summary, designation, risk_level, model, _now()),
        )

    def get_model_fit(self, season, week, league_id, cfg_hash, kind):
        # Keyed on config_hash, NOT league_id, so two leagues sharing a scoring
        # config share the fit. league_id is stored for provenance only.
        row = self._exec(
            "select bundle from model_fits"
            " where season = %s and week = %s and config_hash = %s and kind = %s limit 1",
            (season, week, cfg_hash, kind), fetch="one",
        )
        return row[0] if row else None

    def put_model_fit(self, season, week, league_id, cfg_hash, kind, bundle):
        self._exec(
            "insert into model_fits"
            " (season, week, league_id, config_hash, kind, bundle, fitted_at)"
            " values (%s, %s, %s, %s, %s, %s, %s)"
            " on conflict (season, week, league_id, config_hash, kind) do nothing",
            (season, week, league_id, cfg_hash, kind, json.dumps(bundle, default=str), _now()),
        )

    def get_player_games(self, season, source):
        rows = self._exec(
            "select player_id, week, stat_line from player_games where season = %s and source = %s",
            (season, source), fetch="all",
        ) or []
        return {(r[0], r[1]): r[2] for r in rows}

    def put_player_games(self, rows, source):
        for row in rows:
            if not row.get("player_id") or row.get("season") is None:
                continue
            self._exec(
                "insert into player_games"
                " (player_id, season, week, source, stat_line, fetched_at)"
                " values (%s, %s, %s, %s, %s, %s)"
                " on conflict (player_id, season, week, source) do update"
                " set stat_line = excluded.stat_line, fetched_at = excluded.fetched_at",
                (row["player_id"], int(row["season"]), int(row.get("week", 0)), source,
                 json.dumps(row.get("stat_line", {}), default=str), _now()),
            )

    # -- prediction log ----------------------------------------------------
    def start_run(self, season, week, git_sha, cfg_hash):
        run_id = str(uuid.uuid4())
        ok = self._exec(
            "insert into runs (run_id, season, week, git_sha, config_hash, started_at, status)"
            " values (%s, %s, %s, %s, %s, %s, 'running') returning run_id",
            (run_id, season, week, git_sha, cfg_hash, _now()), fetch="one",
        )
        return run_id if ok else None

    def finish_run(self, run_id, status):
        if not run_id:
            return
        self._exec(
            "update runs set finished_at = %s, status = %s where run_id = %s",
            (_now(), status, run_id),
        )

    def write_projections(self, run_id, league_id, season, week, rows):
        """Append-only. `do nothing` on conflict, never `do update`: a second
        write for the same (run, league, player) is a bug, and overwriting
        would recreate exactly the mutable-log defect this table removes."""
        if not run_id:
            return 0
        written = 0
        for row in rows:
            if not row.get("player_id"):
                continue
            done = self._exec(
                "insert into projections (run_id, league_id, season, week, player_id,"
                " tier, points, conditional_points, play_probability, floor, ceiling)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (run_id, league_id, player_id) do nothing returning 1",
                (run_id, league_id, season, week, row["player_id"], row.get("tier"),
                 row.get("points"), row.get("conditional_points"),
                 row.get("play_probability"), row.get("floor"), row.get("ceiling")),
                fetch="one",
            )
            written += 1 if done else 0
        return written

    def upsert_players(self, rows):
        for row in rows:
            if not row.get("player_id"):
                continue
            self._exec(
                "insert into players (player_id, name, position, team, espn_id, updated_at)"
                " values (%s, %s, %s, %s, %s, %s)"
                " on conflict (player_id) do update set name = excluded.name,"
                " position = excluded.position, team = excluded.team,"
                " updated_at = excluded.updated_at",
                (row["player_id"], row.get("name") or "", row.get("position") or "",
                 row.get("team"), row.get("espn_id"), _now()),
            )

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def open_store(
    database_url: Optional[str] = None, cache_dir: Optional[str] = None, quiet: bool = False
) -> Store:
    """The only way callers get a store. Environment decides; callers never do.

    Precedence: DATABASE_URL (phase 1) beats FANTASY_STORE_DIR (phase 0) beats
    nothing (today's behaviour). A DATABASE_URL that is set but unreachable
    falls back rather than failing the run -- a scheduled Saturday report is
    not the place to discover the database is down.
    """
    dsn = database_url if database_url is not None else os.environ.get("DATABASE_URL")
    if dsn:
        try:
            store = PostgresStore(dsn)
            if not quiet:
                print("  store: postgres (shared cache + append-only prediction log)")
            return store
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"  store: DATABASE_URL set but unusable ({exc}) -- falling back.")

    root = cache_dir if cache_dir is not None else os.environ.get("FANTASY_STORE_DIR")
    if root:
        try:
            store = FileStore(Path(root))
            if not quiet:
                print(f"  store: filesystem cache at {root} (no prediction log)")
            return store
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"  store: cache dir unusable ({exc}) -- running without a store.")

    if not quiet:
        print("  store: disabled (no DATABASE_URL or FANTASY_STORE_DIR) -- pre-store behaviour")
    return NullStore()


# ---------------------------------------------------------------------------
# Bundle packing
# ---------------------------------------------------------------------------
# The design doc says the fit functions "already return serialisable bundles".
# That is true but not obviously so: calibration_fit.fit_from_history returns
# plain dicts alongside two fitted OBJECTS (calibration.IntervalModel,
# blend.BlendModel). Both hold nothing but floats, strings and containers, so
# their __dict__ round-trips through JSON -- verified on fitted instances, not
# assumed from empty ones.
#
# The one real hazard is that JSON turns every tuple into a list. Both classes
# only ever unpack and index those structures, never hash them or compare them
# to a tuple literal, so the round-trip is behaviour-preserving. A future field
# that needs a real tuple would break silently here, which is why unpacking is
# a named function with this note attached rather than an inline dict lookup.
_PACKED = "__packed_model__"


def pack_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """A fit bundle with any fitted model objects replaced by JSON."""
    out: dict[str, Any] = {}
    for key, value in (bundle or {}).items():
        if hasattr(value, "__dict__") and not isinstance(value, (dict, list, tuple, str)):
            out[key] = {_PACKED: type(value).__name__, "state": dict(value.__dict__)}
        else:
            out[key] = value
    return out


def unpack_bundle(packed: dict[str, Any]) -> dict[str, Any]:
    """Inverse of pack_bundle. An unknown class name yields None for that slot,
    so a bundle cached by a newer version degrades to "refit that piece"
    instead of raising."""
    from importlib import import_module

    known = {"IntervalModel": "calibration", "BlendModel": "blend"}
    out: dict[str, Any] = {}
    for key, value in (packed or {}).items():
        if isinstance(value, dict) and _PACKED in value:
            module_name = known.get(value[_PACKED])
            if not module_name:
                out[key] = None
                continue
            try:
                cls = getattr(import_module(module_name), value[_PACKED])
                obj = cls.__new__(cls)
                obj.__dict__.update(value.get("state") or {})
                out[key] = obj
            except Exception:  # noqa: BLE001
                out[key] = None
        else:
            out[key] = value
    return out
