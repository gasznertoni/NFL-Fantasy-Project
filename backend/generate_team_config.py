"""
CLI: builds each league's frontend/public/mock/<league>/default-team-config.json
from backend/leagues/<league>/roster.json.

Why this exists. `default-team-config.json` seeds the "My Team" tab on a
browser's first visit, and it is a list of player ids indexed BY POSITION
into that league's roster-slots.json. Until 2026-09-20 it was maintained by
hand -- its own note said "regenerate rather than hand-edit" while nothing
existed to regenerate it -- so a waiver move meant remembering to edit two
files that share no format, no key and no player identifier. One was missed:
league-2 kept seeding Quentin Johnston and Harold Fannin Jr. for seventeen
days after they were dropped.

The join this does is exactly the one the frontend had three separate bugs in
(see CLAUDE.md v21): a slot index is meaningless without the slot array it
counts against. roster.json and roster-slots.json spell the same roster two
different ways -- league-2's roster.json says `["QB","RB","RB","WR","WR",
"TE","FLEX","FLEX","K","DEF"]` where roster-slots.json says `[... "DST","K"
...]`, a different order AND a different name for the same slot -- so the
starters are placed by SLOT NAME, never by zipping the two arrays.

Everything here raises rather than warns. A generator that half-works writes
a plausible wrong fixture, which is the failure mode this repo keeps paying
for; a generator that stops names the problem while someone is looking.

Usage:
    python3 backend/generate_team_config.py                  # every league
    python3 backend/generate_team_config.py --league league-2
    python3 backend/generate_team_config.py --check          # CI: diff only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
LEAGUES_DIR = Path(__file__).resolve().parent / "leagues"
MOCK_DIR = REPO_ROOT / "frontend" / "public" / "mock"

# Mirrors SLOT_ELIGIBILITY in frontend/src/lib/teamConfig.js. Duplicated
# rather than shared because the two live in different languages, and kept
# deliberately small so the duplication is obvious if either side moves --
# a slot name missing here would silently accept anything, which is the
# behaviour the frontend was just fixed out of.
SLOT_ELIGIBILITY: dict[str, tuple[str, ...]] = {
    "QB": ("QB",),
    "RB": ("RB",),
    "WR": ("WR",),
    "TE": ("TE",),
    "DST": ("DST",),
    "K": ("K",),
    "FLEX": ("RB", "WR", "TE"),
}

# Slots that hold a player without starting him; any position is legal.
BENCH_SLOT = "BENCH"
IR_SLOT = "IR"
NON_STARTING_SLOTS = (BENCH_SLOT, IR_SLOT)

# roster.json is captured from the platform's own wording, so it carries
# whatever that platform calls a team defence. roster-slots.json is the
# frontend's vocabulary and always says DST.
SLOT_ALIASES = {"DEF": "DST", "D/ST": "DST", "PK": "K"}


class TeamConfigError(Exception):
    """A roster and its slot array or player pool do not agree. Always fatal:
    the alternative is emitting a fixture that looks fine and seats the wrong
    players."""


def canonical_slot(slot_name: str) -> str:
    return SLOT_ALIASES.get(slot_name, slot_name)


def is_position_eligible(slot_name: str, position: str) -> bool:
    """True when `position` may occupy `slot_name`. An unrecognised slot name
    is treated as bench-like (any position), matching the frontend's
    documented fallback rule."""
    eligible = SLOT_ELIGIBILITY.get(canonical_slot(slot_name))
    return eligible is None or position in eligible


def index_pool(pool_players: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """{lowercased name: [pool entries]}. A list, not a single entry: real
    name collisions exist, and resolve_player_id disambiguates on
    position+team rather than taking the first hit."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for entry in pool_players:
        by_name.setdefault(str(entry.get("name", "")).strip().lower(), []).append(entry)
    return by_name


def resolve_player_id(
    player: dict[str, Any], by_name: dict[str, list[dict[str, Any]]], where: str
) -> str:
    """The pool's player id for one roster entry.

    A roster entry may carry `playerId` already (league-1 does, league-2 does
    not). Either way the pool is consulted: an id that resolves to nobody
    renders in the app as "Assigned player not found in pool", and it is far
    cheaper to fail here than to ship that.
    """
    name = str(player.get("name", "")).strip()
    position = player.get("position")
    team = player.get("team")
    if not name:
        raise TeamConfigError(f"{where}: roster entry has no name: {player!r}")

    candidates = by_name.get(name.lower(), [])
    if not candidates:
        raise TeamConfigError(
            f"{where}: '{name}' ({position}, {team}) is not in this league's "
            f"player-pool.json. Either the roster has a typo, or the pool is stale "
            f"-- regenerate it with generate_report.py before regenerating this."
        )

    # Narrow on what the roster claims, so a name collision cannot silently
    # pick the wrong player.
    matches = [
        c for c in candidates if c.get("position") == position and c.get("team") == team
    ]
    if not matches:
        found = ", ".join(f"{c.get('position')}/{c.get('team')}" for c in candidates)
        raise TeamConfigError(
            f"{where}: '{name}' is in the pool as {found}, but the roster says "
            f"{position}/{team}. One of the two is out of date -- check which before "
            f"changing either."
        )
    if len(matches) > 1:
        raise TeamConfigError(
            f"{where}: '{name}' ({position}, {team}) matches {len(matches)} pool "
            f"entries; cannot tell them apart."
        )

    resolved = matches[0].get("playerId")
    if not resolved:
        raise TeamConfigError(f"{where}: pool entry for '{name}' has no playerId.")

    declared = player.get("playerId")
    if declared and declared != resolved:
        raise TeamConfigError(
            f"{where}: roster gives '{name}' playerId {declared!r}, pool says "
            f"{resolved!r}. Fix the roster rather than letting this generator pick."
        )
    return str(resolved)


def expected_slots(roster: dict[str, Any]) -> list[str]:
    """The slot array `roster.json` describes, in its own order and canonical
    spelling: starters, then bench, then IR."""
    starting = [canonical_slot(s) for s in roster.get("slots", [])]
    bench = [BENCH_SLOT] * int(roster.get("benchSlots", 0))
    ir = [IR_SLOT] * int(roster.get("irSlots", 0))
    return starting + bench + ir


def check_slot_agreement(roster: dict[str, Any], slots: list[str], league_id: str) -> None:
    """roster.json and roster-slots.json must describe the same multiset of
    slots. Compared as a multiset, not a sequence, because the two orders
    legitimately differ (league-2 lists K before DEF; the frontend lists DST
    before K) -- but a different COUNT means one of them is stale, which is
    precisely what made league-1's roster-slots.json carry league-2's shape
    for a month."""
    want = sorted(expected_slots(roster))
    have = sorted(canonical_slot(s) for s in slots)
    if want != have:
        raise TeamConfigError(
            f"{league_id}: roster.json describes {want} but roster-slots.json has "
            f"{have}. These must agree before a config can be generated."
        )


def build_slot_assignments(
    roster: dict[str, Any], slots: list[str], pool_players: list[dict[str, Any]], league_id: str
) -> list[str | None]:
    """The `slotAssignments` array, index-aligned to `slots`.

    Starters go to a free slot of their OWN slot name; bench and IR fill their
    sections in roster order. Every placement is checked for position
    eligibility even though correct input cannot violate it -- the check costs
    nothing and this is the one place the invariant can be enforced before a
    browser ever sees the file.
    """
    check_slot_agreement(roster, slots, league_id)
    by_name = index_pool(pool_players)

    assignments: list[str | None] = [None] * len(slots)
    seen: dict[str, str] = {}

    def place(index: int, player: dict[str, Any], where: str) -> None:
        player_id = resolve_player_id(player, by_name, where)
        if player_id in seen:
            raise TeamConfigError(
                f"{league_id}: '{player.get('name')}' ({player_id}) appears twice -- "
                f"already placed by {seen[player_id]}."
            )
        position = player.get("position")
        if not is_position_eligible(slots[index], position):
            raise TeamConfigError(
                f"{where}: '{player.get('name')}' is a {position}, which cannot occupy "
                f"a {slots[index]} slot."
            )
        assignments[index] = player_id
        seen[player_id] = where

    def next_free(slot_name: str) -> int | None:
        target = canonical_slot(slot_name)
        for i, name in enumerate(slots):
            if canonical_slot(name) == target and assignments[i] is None:
                return i
        return None

    for player in roster.get("starters", []):
        slot_name = player.get("slot")
        if not slot_name:
            raise TeamConfigError(
                f"{league_id}: starter '{player.get('name')}' has no `slot`; a starter "
                f"cannot be placed by position alone (FLEX would be ambiguous)."
            )
        index = next_free(slot_name)
        if index is None:
            raise TeamConfigError(
                f"{league_id}: no free {canonical_slot(slot_name)} slot left for "
                f"'{player.get('name')}'. roster.json lists more starters at that slot "
                f"than roster-slots.json has."
            )
        place(index, player, f"{league_id} starter {slot_name}")

    for section, slot_name in ((roster.get("bench", []), BENCH_SLOT), (roster.get("injuredReserve", []), IR_SLOT)):
        for player in section:
            index = next_free(slot_name)
            if index is None:
                raise TeamConfigError(
                    f"{league_id}: no free {slot_name} slot left for "
                    f"'{player.get('name')}'; roster.json lists more than "
                    f"roster-slots.json has."
                )
            place(index, player, f"{league_id} {slot_name.lower()}")

    return assignments


def build_default_team_config(
    roster: dict[str, Any], slots: list[str], pool_players: list[dict[str, Any]], league_id: str
) -> dict[str, Any]:
    assignments = build_slot_assignments(roster, slots, pool_players, league_id)
    captured = roster.get("capturedAt", "unknown")
    return {
        "_note": (
            f"GENERATED by backend/generate_team_config.py from "
            f"backend/leagues/{league_id}/roster.json (capturedAt {captured}) -- do not "
            f"hand-edit. One entry per slot in this league's roster-slots.json, by array "
            f"index. After any roster change, re-run: python3 "
            f"backend/generate_team_config.py --league {league_id}"
        ),
        "slotAssignments": assignments,
    }


def render(config: dict[str, Any]) -> str:
    """The exact bytes written, so --check can compare text rather than
    re-parsing and guessing at formatting."""
    return json.dumps(config, indent=2) + "\n"


def league_paths(league_id: str) -> tuple[Path, Path, Path, Path]:
    return (
        LEAGUES_DIR / league_id / "roster.json",
        MOCK_DIR / league_id / "roster-slots.json",
        MOCK_DIR / league_id / "player-pool.json",
        MOCK_DIR / league_id / "default-team-config.json",
    )


def discover_leagues() -> list[str]:
    return sorted(p.name for p in LEAGUES_DIR.iterdir() if (p / "roster.json").exists())


def generate_for_league(league_id: str, check_only: bool) -> bool:
    """Returns True when the file on disk already matches (or was written)."""
    roster_path, slots_path, pool_path, out_path = league_paths(league_id)
    for path in (roster_path, slots_path, pool_path):
        if not path.exists():
            raise TeamConfigError(f"{league_id}: missing {path}")

    roster = json.loads(roster_path.read_text())
    slots = json.loads(slots_path.read_text())["slots"]
    pool = json.loads(pool_path.read_text())["players"]

    config = build_default_team_config(roster, slots, pool, league_id)
    rendered = render(config)
    current = out_path.read_text() if out_path.exists() else None

    if current == rendered:
        print(f"{league_id}: up to date ({sum(a is not None for a in config['slotAssignments'])} of {len(slots)} slots filled)")
        return True

    if check_only:
        by_id = {p["playerId"]: p for p in pool}
        old = json.loads(current)["slotAssignments"] if current else []
        for i, slot_name in enumerate(slots):
            was = old[i] if i < len(old) else None
            now = config["slotAssignments"][i]
            if was != now:
                name = lambda pid: by_id.get(pid, {}).get("name", pid) if pid else "(empty)"  # noqa: E731
                print(f"{league_id}: slot {i} ({slot_name}): {name(was)} -> {name(now)}")
        print(f"{league_id}: OUT OF DATE -- run without --check to rewrite")
        return False

    out_path.write_text(rendered)
    print(f"{league_id}: wrote {out_path.relative_to(REPO_ROOT)}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", action="append", dest="leagues", help="league id; repeatable. Default: all.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what would change and exit non-zero if anything would; write nothing.",
    )
    args = parser.parse_args(argv)

    leagues = args.leagues or discover_leagues()
    if not leagues:
        print(f"No leagues with a roster.json under {LEAGUES_DIR}", file=sys.stderr)
        return 1

    ok = True
    for league_id in leagues:
        try:
            ok = generate_for_league(league_id, args.check) and ok
        except TeamConfigError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
