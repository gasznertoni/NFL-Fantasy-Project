"""Shared path/env bootstrap for the audit-III analysis scripts.

Every script here is run from the repo root as
    .venv/bin/python docs/research/scripts/<name>.py
and needs backend/ on sys.path plus the repo root as cwd.
"""
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BACKEND = os.path.join(REPO_ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def load_env():
    """Read the gitignored .env at the repo root into os.environ (never printed)."""
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def league_config(league_id):
    import json
    path = os.path.join(BACKEND, "leagues", league_id, "scoring-config.json")
    with open(path) as fh:
        return json.load(fh)
