# Waiver recommendations under roster-construction constraints

**Status:** implemented 2026-09-05 in `frontend/src/lib/lineup.js`. Written
before the code because the rules are judgement calls about fantasy roster
management, not arithmetic, and they were argued and agreed before shipping.

Builder's decisions on the two open questions: **option (a)** for minimums (an
already-thin position freezes rather than blocking), and **`cap(DST) = 1`
confirmed** -- streaming means swapping a D/ST, not carrying two.

Supersedes nothing. Extends `lineup.waiverReplacement`, specified in
`team-config-and-roster-status.md`.

## 1. Why

Today's advice ranks a drop purely on value over replacement. That number is
correct (see `lineup.replacementLevels`) and still produces bad advice,
because it answers *"who is worth least?"* when the real question is
*"which roster do I want to own next week?"*

Two failures observed against the real league-2 roster on 2026-09-05:

1. **It recommends dropping the backup QB.** Matthew Stafford is 15.9 points
   below replacement over four weeks, which is genuinely the lowest surplus on
   the roster — quarterbacks score more, so a QB2 who never starts looks
   terrible on a scale set by QB1s. Acting on it leaves **one quarterback**. If
   Jaxson Dart is ruled out on a Sunday morning, the lineup has an unfillable
   slot and scores zero there.

2. **It recommends adding a third D/ST it would not start.** The roster holds
   Eagles D/ST and starts exactly one. Two of the three suggested D/STs
   (Cowboys −13.1, Lions −6.5) are *worse than the one already rostered*
   (−3.8). Adding either is strictly negative, but the surplus comparison never
   looks at the incumbent at the same position, only at the cheapest player
   anywhere on the roster.

Both come from the same gap: a swap is evaluated as two isolated players
rather than as a change to a roster that has to field a legal lineup every
week.

## 2. Goals

- Never recommend a drop that leaves a starting slot unfillable.
- Never recommend adding a player at a position that is already full, unless
  it is an upgrade **on the incumbent at that position**.
- Keep the value ranking that already works; add constraints around it rather
  than replacing it.
- Derive every limit from the league's own slot configuration, so this holds
  for the 14-team league-1 and the 8-team league-2 without per-league tuning.

## 3. Non-goals

- **Per-week bye legality is not checked.** Verifying that every future week
  can still be filled is a scheduling problem, and the horizon value already
  counts a bye as zero, which penalises thin positions indirectly. In an
  8-team league the waiver pool is deep enough that a bye hole is a pickup,
  not a crisis. Revisit if a real week is ever unfillable.
- **Trade value and dynasty value are out of scope**, as they are for the rest
  of v1.
- **No multi-move plans.** One add and one drop, which is what a waiver claim
  actually is.

## 4. Roster shape, derived not hardcoded

Everything below comes from the league's `roster-slots.json` plus
`manifest.teamCount`. Two quantities:

- `starts(P)` — starting slots that only position `P` can fill.
- `flexShare(P)` — flex slots `P` is eligible for, split evenly across the
  flex-eligible positions (already implemented in `lineup.startingDemand`).

For league-2 (`QB, RB, RB, WR, WR, TE, FLEX, FLEX, DST, K` + 5 BENCH + 2 IR):

| Position | starts | flexShare | rostered today |
|---|---|---|---|
| QB | 1 | 0 | 2 |
| RB | 2 | 0.67 | 3 |
| WR | 2 | 0.67 | 6 |
| TE | 1 | 0.67 | 2 |
| K | 1 | 0 | 1 |
| DST | 1 | 0 | 1 |

## 5. Minimums and caps

Two limits per position. `min(P)` is the number below which a drop is
refused. `cap(P)` is the number above which an add is not an addition but a
replacement.

| Position | min | cap | Rationale |
|---|---|---|---|
| **K** | 1 | 1 | You start exactly one and a replacement-level kicker is always on waivers. A second kicker is a wasted roster spot in every week of the season. The least controversial rule in fantasy. |
| **DST** | 1 | 1 | Same as K by default. Streaming a matchup means *swapping* your D/ST, not carrying two. |
| **QB** | 2 | 2 | The builder's explicit requirement. One quarterback is a single point of failure on a slot that cannot be filled from elsewhere — no other position is flex-eligible into QB. A third never starts and never covers anything the second does not. |
| **RB** | `ceil(starts + flexShare) + 1` = **4** | none | Highest injury rate of any position, and the flex slots compete for the same bodies. The `+1` is cover for one loss. |
| **WR** | `ceil(starts + flexShare) + 1` = **4** | none | Same reasoning; WR is where speculative bench adds belong. |
| **TE** | `ceil(starts + flexShare)` = **2** | none | No `+1`: a second TE already covers the starter, and TE is flex-eligible so depth is shared. |

Sum of minimums for league-2: 2+4+4+2+1+1 = **14** against 15 roster spots,
leaving one discretionary spot. That is tight by design — a roster at its
minimums *should* have few legal drops, because it genuinely has few.

**Open question for the builder:** RB min 4 and WR min 4 would make the
current roster (RB 3) already below minimum, which would block every RB drop
and read oddly. Two options:

- **(a)** Treat minimums as *"do not go below whichever is lower: the limit,
  or what you hold today"*, so an already-thin position is frozen rather than
  unfillable. Recommended — it never blocks the UI on a state the user is
  already in.
- **(b)** Lower RB/WR minimums to `ceil(starts + flexShare)` = 3, no cover.
  Simpler, but permits dropping to exactly the number you start.

**Decided: (a).** Implemented in `canDropFrom`.

## 6. Algorithm

Given a waiver candidate `C` at position `P`:

**Step 1 — is this an addition or a replacement?**

If `rostered(P) >= cap(P)`, the position is full. The only sensible move is a
same-position upgrade:

- Let `W` = the rostered player at `P` with the lowest surplus.
- If `surplus(C) > surplus(W)` → recommend replacing `W`, described as an
  upgrade at that position.
- Otherwise → **no recommendation**, stated as such: *"You already roster a
  better D/ST (Eagles D/ST)."* Silence would leave the user wondering.

**Step 2 — otherwise, find the cheapest legal drop.**

A rostered player `D` is a legal drop when all hold:

- `D` is not in an IR slot and carries no Out/IR/Doubtful designation
  (already implemented).
- Dropping `D` leaves `rostered(pos(D)) >= min(pos(D))`, per §5 option (a).
- `D` is not the candidate's own position when that would breach `cap` — a
  no-op guard, since Step 1 caught it.

Among legal drops, take the lowest surplus. This is the current behaviour,
now filtered.

**Step 3 — is the swap worth it?**

Unchanged from today: compare `surplus(C)` against `surplus(D)` over the
horizon, and return `hold` when `C` wins this week but loses the window.

**Step 4 — nothing legal.**

If no drop is legal, return a distinct result and say so: *"No legal drop —
every bench player is needed for positional cover."* This is a real state for
a tight roster and must not silently render as "no suggestion".

## 7. Worked examples, real data (league-2, week 1, 4-week horizon)

Replacement levels: QB 68.5, K 33.8, RB 30.5, DST 30.4, WR 29.7, TE 27.4.

**Broncos D/ST (surplus +3.0).** DST is at cap (1 of 1) → Step 1. Incumbent
Eagles D/ST is −3.8. `+3.0 > −3.8` → **"Upgrade on Eagles D/ST at DST, +6.8
over the next 4 weeks."**
*Today it says: "Would replace Matthew Stafford."*

**Cowboys D/ST (−13.1) and Lions D/ST (−6.5).** DST at cap → Step 1. Both are
worse than Eagles (−3.8) → **no recommendation**, with the reason shown.
*Today both say: "Would replace Matthew Stafford."*

**A waiver RB better than replacement.** RB is uncapped → Step 2. Stafford is
excluded (QB would fall 2 → 1, below min 2). The cheapest legal drop is the
lowest-surplus RB/WR/TE that keeps its position at or above minimum — on this
roster, a WR (6 rostered, min 4).

**A kicker, any kicker.** K at cap (1 of 1) → Step 1, compared only against
Cameron Dicker. Only a genuinely better kicker surfaces, and it is described
as a kicker-for-kicker swap. This is the case the builder raised: a bench QB
is never offered up for a kicker, because the two never meet in the
algorithm.

## 8. Acceptance criteria

1. With QB at its minimum, no waiver target suggests dropping the backup QB.
2. A D/ST or K candidate is compared only against the incumbent at that
   position, and is described as an upgrade rather than a drop elsewhere.
3. A candidate worse than the incumbent at a capped position produces an
   explicit "you already have better", not silence.
4. A roster with no legal drop produces an explicit message, not silence.
5. Every limit is computed from `roster-slots.json` and `manifest.teamCount`;
   no position count is hardcoded to a league size.
6. The existing IR/injury exclusions and the horizon `hold` behaviour are
   unchanged.
7. Pure functions, unit-testable without a browser, consistent with
   `lineup.js` today.

## 9. Risks

- **Minimums can over-freeze a roster.** Mitigated by §5 option (a) and by
  criterion 4 making the state visible rather than silent.
- **`cap(DST) = 1` blocks legitimate streaming.** Someone who carries two
  D/STs to stream matchups will find this restrictive. Judged the rarer case
  in an 8-team league; revisit if it bites.
- **QB min 2 is a preference, not a law.** Some managers punt QB2 entirely.
  It is the builder's stated preference and it is derived from a real failure
  mode, so it ships as the default; it should be a constant with its
  reasoning attached, not a magic number.
