"""Pipeline cache and append-only prediction log. See ARCHITECTURE.md §12."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

CACHE_SCHEMA_VERSION = "1"

FIT_KINDS = ("availability", "calibration", "blend", "week1")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(*parts: Any) -> str:
    """A stable sha256 over arbitrary JSON-able parts."""
    payload = json.dumps(
        [CACHE_SCHEMA_VERSION, *parts], sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def config_hash(scoring_config: dict[str, Any], **model_params: Any) -> str:
    """Hash of a scoring config (underscore keys dropped), so leagues can share fits."""
    scoring = {k: v for k, v in (scoring_config or {}).items() if not k.startswith("_")}
    return content_hash(scoring, model_params)


class Store:
    """No-op base class, and the live contract."""

    enabled = False
    backend = "null"

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
    """The disabled store, named so logs can say which backend is live."""


class FileStore(Store):
    """Phase 0: content-addressed caches on disk; no prediction log."""

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


class PostgresStore(Store):
    """Phase 1: shared Postgres backend with an append-only projections table."""

    enabled = True
    backend = "postgres"

    def __init__(self, dsn: str):
        import psycopg  # noqa: F401

        self.dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=True)

    def _exec(self, sql: str, params: tuple = (), fetch: str = "none"):
        """Run a statement; every backend error is swallowed here."""
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
        """Append-only: on conflict do nothing, never update."""
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


def _resolve_cache_dir(root: str) -> Path:
    """Resolve a relative FANTASY_STORE_DIR against the repo root, not the cwd."""
    path = Path(root).expanduser()
    if path.is_absolute():
        return path
    return (Path(__file__).resolve().parent.parent / path).resolve()


def open_store(
    database_url: Optional[str] = None, cache_dir: Optional[str] = None, quiet: bool = False
) -> Store:
    """The only way callers get a store; the environment decides which."""
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
            store = FileStore(_resolve_cache_dir(root))
            if not quiet:
                print(f"  store: filesystem cache at {root} (no prediction log)")
            return store
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"  store: cache dir unusable ({exc}) -- running without a store.")

    if not quiet:
        print("  store: disabled (no DATABASE_URL or FANTASY_STORE_DIR) -- pre-store behaviour")
    return NullStore()


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
    """Inverse of pack_bundle; an unknown class unpacks to None."""
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
