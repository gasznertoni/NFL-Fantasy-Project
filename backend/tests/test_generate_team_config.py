"""
Tests for generate_team_config.py -- the roster.json -> default-team-config.json
join.

The bug that motivated this module was not a wrong calculation, it was two
files drifting apart silently: league-2's default-team-config.json kept
seeding Quentin Johnston and Harold Fannin Jr. for seventeen days after they
were dropped, because nothing generated it and nothing checked it. So the
tests that matter most here are the ones asserting the generator REFUSES a
disagreement rather than papering over it -- every case below that ends in
pytest.raises is a fixture this repo would otherwise have shipped.

The last test runs against the real checked-in league files, which makes it
the standing guard: if a roster changes and the fixture is not regenerated,
it fails.
"""

from __future__ import annotations

import json

import pytest

from generate_team_config import (
    TeamConfigError,
    build_default_team_config,
    build_slot_assignments,
    canonical_slot,
    discover_leagues,
    is_position_eligible,
    league_paths,
    render,
)

# A small league: 1 QB, 1 RB, 1 WR, 1 TE, 1 FLEX, 1 DST, 1 K, 2 BENCH, 1 IR.
SLOTS = ["QB", "RB", "WR", "TE", "FLEX", "DST", "K", "BENCH", "BENCH", "IR"]

POOL = [
    {"playerId": "p-qb1", "name": "Real Quarterback", "position": "QB", "team": "NYG"},
    {"playerId": "p-qb2", "name": "Backup Quarterback", "position": "QB", "team": "TB"},
    {"playerId": "p-rb1", "name": "Real Runningback", "position": "RB", "team": "DAL"},
    {"playerId": "p-wr1", "name": "Real Receiver", "position": "WR", "team": "SEA"},
    {"playerId": "p-wr2", "name": "Other Receiver", "position": "WR", "team": "IND"},
    {"playerId": "p-te1", "name": "Real Tightend", "position": "TE", "team": "PHI"},
    {"playerId": "JAX", "name": "Jaguars D/ST", "position": "DST", "team": "JAX"},
    {"playerId": "p-k1", "name": "Real Kicker", "position": "K", "team": "LAC"},
    {"playerId": "p-wr3", "name": "Hurt Receiver", "position": "WR", "team": "GB"},
]


def roster(**overrides):
    base = {
        "leagueId": "league-test",
        "capturedAt": "2026-09-20",
        # Deliberately NOT in roster-slots.json's order, and using the
        # platform's own "DEF" spelling -- both are real in league-2.
        "slots": ["QB", "RB", "WR", "TE", "FLEX", "K", "DEF"],
        "benchSlots": 2,
        "irSlots": 1,
        "starters": [
            {"slot": "QB", "name": "Real Quarterback", "position": "QB", "team": "NYG"},
            {"slot": "RB", "name": "Real Runningback", "position": "RB", "team": "DAL"},
            {"slot": "WR", "name": "Real Receiver", "position": "WR", "team": "SEA"},
            {"slot": "TE", "name": "Real Tightend", "position": "TE", "team": "PHI"},
            {"slot": "FLEX", "name": "Other Receiver", "position": "WR", "team": "IND"},
            {"slot": "DEF", "name": "Jaguars D/ST", "position": "DST", "team": "JAX"},
            {"slot": "K", "name": "Real Kicker", "position": "K", "team": "LAC"},
        ],
        "bench": [{"name": "Backup Quarterback", "position": "QB", "team": "TB"}],
        "injuredReserve": [],
    }
    base.update(overrides)
    return base


def build(r=None, slots=None, pool=None):
    return build_slot_assignments(r or roster(), slots or SLOTS, pool or POOL, "league-test")


# ---------------------------------------------------------------------------
# The join itself
# ---------------------------------------------------------------------------


def test_starters_are_placed_by_slot_name_not_by_order():
    # roster.json lists K before DEF; roster-slots.json has DST before K.
    # Zipping the two arrays -- the obvious implementation -- would put the
    # kicker in the DST slot and the defence in the K slot, which is exactly
    # the symptom the frontend was just fixed for.
    assignments = build()
    assert assignments[SLOTS.index("DST")] == "JAX"
    assert assignments[SLOTS.index("K")] == "p-k1"


def test_full_assignment_is_index_aligned_to_the_slot_array():
    assert build() == [
        "p-qb1",  # QB
        "p-rb1",  # RB
        "p-wr1",  # WR
        "p-te1",  # TE
        "p-wr2",  # FLEX
        "JAX",  # DST
        "p-k1",  # K
        "p-qb2",  # BENCH
        None,  # BENCH
        None,  # IR
    ]


def test_bench_and_ir_fill_their_own_sections_in_roster_order():
    # No FLEX in this variant, so the two spare receivers land on the bench
    # and IR rather than in a starting slot.
    slots = ["QB", "RB", "WR", "TE", "DST", "K", "BENCH", "BENCH", "IR"]
    r = roster(
        slots=["QB", "RB", "WR", "TE", "K", "DEF"],
        starters=[s for s in roster()["starters"] if s["slot"] != "FLEX"],
        bench=[
            {"name": "Backup Quarterback", "position": "QB", "team": "TB"},
            {"name": "Other Receiver", "position": "WR", "team": "IND"},
        ],
        injuredReserve=[{"name": "Hurt Receiver", "position": "WR", "team": "GB"}],
    )
    assignments = build_slot_assignments(r, slots, POOL, "league-test")
    assert assignments[slots.index("BENCH")] == "p-qb2"
    assert assignments[slots.index("BENCH") + 1] == "p-wr2"
    assert assignments[slots.index("IR")] == "p-wr3"


def test_a_dst_resolves_by_its_team_abbreviation_id():
    # D/ST entries carry a team abbreviation where everyone else carries an
    # nflverse id; the pool lookup must not assume the id shape.
    assert build()[SLOTS.index("DST")] == "JAX"


def test_declared_player_id_is_accepted_when_it_agrees_with_the_pool():
    r = roster()
    r["starters"][0]["playerId"] = "p-qb1"  # league-1 carries these
    assert build(r)[0] == "p-qb1"


# ---------------------------------------------------------------------------
# What it must refuse -- each of these would otherwise ship a wrong fixture
# ---------------------------------------------------------------------------


def test_refuses_a_player_missing_from_the_pool():
    r = roster()
    r["bench"] = [{"name": "Ghost Player", "position": "WR", "team": "GB"}]
    with pytest.raises(TeamConfigError, match="not in this league's player-pool"):
        build(r)


def test_refuses_a_player_whose_pool_entry_contradicts_the_roster():
    # The exact shape of a stale roster: right name, wrong team. Silently
    # resolving by name would seat a player who moved teams.
    r = roster()
    r["bench"] = [{"name": "Backup Quarterback", "position": "QB", "team": "BAL"}]
    with pytest.raises(TeamConfigError, match="but the roster says"):
        build(r)


def test_refuses_a_declared_player_id_that_disagrees_with_the_pool():
    r = roster()
    r["starters"][0]["playerId"] = "p-qb2"
    with pytest.raises(TeamConfigError, match="Fix the roster"):
        build(r)


def test_refuses_the_same_player_twice():
    r = roster()
    r["bench"] = [{"name": "Real Receiver", "position": "WR", "team": "SEA"}]
    with pytest.raises(TeamConfigError, match="appears twice"):
        build(r)


def test_refuses_a_slot_count_disagreement():
    # league-1's roster-slots.json carried league-2's shape for a month. This
    # is the check that would have caught it.
    with pytest.raises(TeamConfigError, match="must agree"):
        build(slots=SLOTS + ["BENCH"])


def test_refuses_a_starter_with_no_slot():
    r = roster()
    del r["starters"][4]["slot"]  # the FLEX -- position alone is ambiguous
    with pytest.raises(TeamConfigError, match="no `slot`"):
        build(r)


def test_refuses_a_position_that_cannot_occupy_its_slot():
    r = roster()
    r["starters"][0]["slot"] = "WR"  # a QB into the WR slot
    r["slots"] = ["WR", "RB", "WR", "TE", "FLEX", "K", "DEF"]
    with pytest.raises(TeamConfigError, match="cannot occupy"):
        build(r, slots=["WR", "RB", "WR", "TE", "FLEX", "DST", "K", "BENCH", "BENCH", "IR"])


def test_refuses_more_bench_players_than_bench_slots():
    r = roster(benchSlots=1)
    r["bench"] = [
        {"name": "Backup Quarterback", "position": "QB", "team": "TB"},
        {"name": "Other Receiver", "position": "WR", "team": "IND"},
    ]
    r["starters"] = [s for s in r["starters"] if s["slot"] != "FLEX"]
    r["slots"] = ["QB", "RB", "WR", "TE", "K", "DEF"]
    slots = ["QB", "RB", "WR", "TE", "DST", "K", "BENCH", "IR"]
    with pytest.raises(TeamConfigError, match="no free BENCH slot"):
        build_slot_assignments(r, slots, POOL, "league-test")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_slot_aliases_normalise_platform_spellings():
    assert canonical_slot("DEF") == "DST"
    assert canonical_slot("D/ST") == "DST"
    assert canonical_slot("FLEX") == "FLEX"


def test_eligibility_matches_the_frontend_rule():
    assert is_position_eligible("FLEX", "TE")
    assert not is_position_eligible("FLEX", "QB")
    assert is_position_eligible("DEF", "DST")
    assert not is_position_eligible("DST", "WR")
    # BENCH, IR and any unknown slot name take anything.
    assert is_position_eligible("BENCH", "DST")
    assert is_position_eligible("IR", "K")
    assert is_position_eligible("SUPERFLEX", "QB")


def test_note_records_provenance_and_the_regenerate_command():
    note = build_default_team_config(roster(), SLOTS, POOL, "league-test")["_note"]
    assert "GENERATED" in note and "do not hand-edit" in note
    assert "--league league-test" in note


def test_render_is_stable_and_newline_terminated():
    config = build_default_team_config(roster(), SLOTS, POOL, "league-test")
    text = render(config)
    assert text.endswith("]\n}\n")
    assert json.loads(text)["slotAssignments"] == config["slotAssignments"]


# ---------------------------------------------------------------------------
# The standing guard, against the real checked-in files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("league_id", discover_leagues())
def test_checked_in_fixture_matches_its_roster(league_id):
    """Fails when a roster.json changes and default-team-config.json is not
    regenerated -- the drift this whole module exists to prevent."""
    roster_path, slots_path, pool_path, out_path = league_paths(league_id)
    generated = build_default_team_config(
        json.loads(roster_path.read_text()),
        json.loads(slots_path.read_text())["slots"],
        json.loads(pool_path.read_text())["players"],
        league_id,
    )
    assert out_path.read_text() == render(generated), (
        f"{out_path.name} is out of date for {league_id}. "
        f"Run: python3 backend/generate_team_config.py --league {league_id}"
    )
