// Lineup-level arithmetic for the Weekly Report header: what the STARTING
// lineup is projected to score, and whether a better one is sitting on the
// bench. Pure functions with no React and no fetching, so the numbers can be
// checked directly rather than only through the rendered page.

import { isPositionEligible, eligiblePositions, NON_STARTING_SLOTS } from './teamConfig.js'

// A published player band is the 10th-90th percentile. For a normal that is
// +/- 1.2816 sd, which is how a band is converted to an sd and back below.
const Z_80 = 1.2816

/** Whether a slot name is part of the starting lineup (not bench, not IR). */
export function isStartingSlot(slotName) {
  return !NON_STARTING_SLOTS.includes(slotName)
}

/**
 * The starting lineup's projected total.
 *
 * `total` is a sum of EXPECTED points and is exact: expectation is linear, so
 * it holds whatever the players' outcomes do to each other.
 *
 * `low`/`high` are NOT the sum of the players' floors and ceilings. Adding
 * endpoints answers "what if all nine hit their floor in the same week",
 * which is far wider than an 80% interval for the total -- with nine players
 * it is closer to a 1-in-a-billion week. Instead each band is read as an sd,
 * the VARIANCES are summed, and the result is converted back. That assumes
 * players are independent, which is an approximation: two players in the same
 * game are correlated, so a real stack is slightly wider than this says.
 * Flagged rather than modelled -- correlation would need per-game covariance
 * this report does not carry.
 *
 * Players with no published band (D/ST and K, which the interval model does
 * not cover) contribute their points to `total` and nothing to the spread,
 * so the band is narrow rather than wrong.
 *
 * @param {string[]} slots
 * @param {(string|null)[]} slotAssignments
 * @param {Map<string, object>} projectionsById - weekly-report entries.
 * @returns {{total: number, low: number, high: number, counted: number,
 *   missing: number, banded: number}}
 */
export function lineupProjection(slots, slotAssignments, projectionsById) {
  let total = 0
  let variance = 0
  let counted = 0
  let missing = 0
  let banded = 0

  slots.forEach((slotName, i) => {
    if (!isStartingSlot(slotName)) return
    const playerId = slotAssignments[i]
    if (!playerId) {
      missing += 1
      return
    }
    const entry = projectionsById.get(playerId)
    const points = entry?.projection?.points
    if (typeof points !== 'number') {
      missing += 1
      return
    }
    counted += 1
    total += points
    const lo = entry.projection.floor
    const hi = entry.projection.ceiling
    if (typeof lo === 'number' && typeof hi === 'number' && hi > lo) {
      const sd = (hi - lo) / (2 * Z_80)
      variance += sd * sd
      banded += 1
    }
  })

  const spread = Z_80 * Math.sqrt(variance)
  return {
    total: round1(total),
    // A points total cannot go below zero, so neither can its lower bound.
    low: round1(Math.max(0, total - spread)),
    high: round1(total + spread),
    counted,
    missing,
    banded,
  }
}

/**
 * The best lineup reachable by moving bench players into starting slots, and
 * what it costs to stay put.
 *
 * Greedy: repeatedly apply the single swap that adds the most expected
 * points, until none does. Greedy rather than an exact assignment solve
 * because the swaps that matter here are near-independent (a bench WR into a
 * WR or FLEX slot), and an exact matching would add a solver to justify
 * fractions of a point. It can therefore understate the gain, never overstate
 * it -- every move it reports is real.
 *
 * Eligibility is the same check the roster editor uses, so the header can
 * never suggest a move the app would refuse.
 *
 * IR is excluded as a source: an IR player is not startable.
 *
 * @returns {{gain: number, moves: Array<{playerId, playerName, outPlayerId,
 *   outPlayerName, slotName, slotIndex, fromIndex, delta}>}}
 */
export function betterLineup(slots, slotAssignments, projectionsById, poolById) {
  const assignments = [...slotAssignments]
  const moves = []
  const pointsOf = (playerId) => {
    if (!playerId) return 0
    const p = projectionsById.get(playerId)?.projection?.points
    return typeof p === 'number' ? p : 0
  }
  const positionOf = (playerId) =>
    poolById.get(playerId)?.position || projectionsById.get(playerId)?.position || null
  const nameOf = (playerId) =>
    poolById.get(playerId)?.name || projectionsById.get(playerId)?.name || playerId

  for (let guard = 0; guard < slots.length; guard += 1) {
    let best = null
    slots.forEach((slotName, si) => {
      if (!isStartingSlot(slotName)) return
      const current = assignments[si]
      slots.forEach((benchName, bi) => {
        // Only the bench is a source. IR is not startable, and another
        // starting slot is a reshuffle rather than an upgrade -- the total is
        // unchanged by swapping two players who are both already starting.
        if (benchName !== 'BENCH') return
        const candidate = assignments[bi]
        if (!candidate) return
        const candidatePos = positionOf(candidate)
        if (!candidatePos || !isPositionEligible(slotName, candidatePos)) return
        // The displaced starter has to be legal on the bench, which BENCH
        // always is -- but check anyway so this stays correct if BENCH ever
        // gains a constraint.
        if (current && !isPositionEligible(benchName, positionOf(current))) return
        const delta = pointsOf(candidate) - pointsOf(current)
        if (delta > 0.05 && (!best || delta > best.delta)) {
          best = { slotIndex: si, fromIndex: bi, slotName, delta }
        }
      })
    })
    if (!best) break
    const incoming = assignments[best.slotIndex]
    const outgoing = assignments[best.fromIndex]
    moves.push({
      playerId: outgoing,
      playerName: nameOf(outgoing),
      outPlayerId: incoming,
      outPlayerName: incoming ? nameOf(incoming) : null,
      slotName: best.slotName,
      slotIndex: best.slotIndex,
      fromIndex: best.fromIndex,
      delta: round1(best.delta),
    })
    assignments[best.slotIndex] = outgoing
    assignments[best.fromIndex] = incoming
  }

  return {
    gain: round1(moves.reduce((sum, m) => sum + m.delta, 0)),
    moves,
  }
}

function round1(n) {
  return Math.round(n * 10) / 10
}

// ---------------------------------------------------------------------------
// Waiver replacement
// ---------------------------------------------------------------------------

/**
 * Starting demand per position across the whole league, which is what sets
 * replacement level. FLEX demand is split evenly across the flex-eligible
 * positions rather than assigned to one: a league with two FLEX slots does
 * not start two extra RBs, it starts two extra of whichever of RB/WR/TE is
 * best, and spreading the demand is the standard approximation for that.
 *
 * @returns {Map<string, number>} position -> league-wide starter count.
 */
export function startersPerTeam(slots) {
  const perTeam = new Map()
  const bump = (pos, n) => perTeam.set(pos, (perTeam.get(pos) || 0) + n)
  for (const slotName of slots) {
    if (!isStartingSlot(slotName)) continue
    const eligible = eligiblePositions(slotName)
    if (!eligible) continue // a null here would be an "any position" starter, which no roster has
    if (eligible.length === 1) bump(eligible[0], 1)
    else for (const pos of eligible) bump(pos, 1 / eligible.length)
  }
  return perTeam
}

export function startingDemand(slots, teamCount) {
  const out = new Map()
  for (const [pos, n] of startersPerTeam(slots)) out.set(pos, Math.max(1, Math.round(n * teamCount)))
  return out
}

/**
 * Replacement level per position: the horizon total of the LAST player who
 * would still be starting somewhere in the league.
 *
 * This is what makes cross-position comparison legitimate. Raw totals are not
 * comparable -- a D/ST projected for 33.5 over four weeks is not "better" than
 * a WR at 19.0, because every team already has a D/ST scoring about that and
 * the WR pool runs dry far sooner. Subtracting each position's replacement
 * level converts both to the same quantity: points you would lose by dropping
 * this player and picking up the best freely available body at his position.
 *
 * Same idea as waiver_targets.py's `replacement_surplus` on the backend,
 * derived here from the league's OWN slots and team count rather than that
 * module's fixed 14-team cutoffs -- those are wrong by roughly half for an
 * 8-team league.
 *
 * @returns {Map<string, number>} position -> replacement horizon total.
 */
export function replacementLevels(slots, teamCount, pointsByWeek, poolById) {
  const demand = startingDemand(slots, teamCount)
  const byPosition = new Map()
  for (const [playerId, series] of pointsByWeek) {
    const pos = poolById.get(playerId)?.position
    if (!pos) continue
    if (!byPosition.has(pos)) byPosition.set(pos, [])
    byPosition.get(pos).push(series.reduce((a, b) => a + (typeof b === 'number' ? b : 0), 0))
  }
  const levels = new Map()
  for (const [pos, totals] of byPosition) {
    totals.sort((a, b) => b - a)
    const rank = demand.get(pos)
    // No demand for the position (it has no starting slot) or a pool thinner
    // than the league needs: replacement is zero, so surplus falls back to the
    // raw total, which is the right answer when nothing is replaceable.
    levels.set(pos, rank && rank <= totals.length ? totals[rank - 1] : 0)
  }
  return levels
}

// Designations that make a player IR-eligible. Such a player is never the drop
// candidate for a waiver add: the roster carries IR slots precisely so someone
// hurt can be parked without being released. Before this existed the drop
// suggestion picked whoever scored lowest this week, and an injured player
// scores lowest BY DEFINITION -- P(play) near zero -- so the advice was
// reliably "drop the guy you should be stashing".
//
// Questionable is deliberately absent. A Questionable player still takes the
// field 51-79% of the time depending on practice participation, is not usually
// IR-eligible, and is a legitimate thing to move on from.
export const IR_ELIGIBLE_DESIGNATIONS = ['Out', 'IR', 'Doubtful']


// Positions where carrying a spare is either mandatory or pointless, and the
// count is a policy call rather than something the slot array implies. Spec:
// docs/specs/waiver-roster-construction.md section 5.
//
//   K, DST  min 1 cap 1. You start exactly one and a replacement-level one is
//           always on waivers, so a second is a wasted roster spot in every
//           week of the season. Streaming a matchup means swapping your D/ST,
//           not carrying two.
//   QB      min 2 cap 2. Nothing is flex-eligible into QB, so one quarterback
//           is a single point of failure on a slot that cannot be covered from
//           elsewhere; a third covers nothing the second does not. This is a
//           stated preference of the builder's, not a law -- some managers punt
//           QB2 -- but it comes from a real failure mode and ships as default.
const POLICY_LIMITS = {
  QB: { min: 2, cap: 2 },
  K: { min: 1, cap: 1 },
  DST: { min: 1, cap: 1 },
}

// Positions that carry a spare body beyond what they start, because they lose
// players and because flex slots compete for the same pool. TE is absent
// deliberately: a second TE already covers the starter and TE is flex-eligible,
// so its depth is shared rather than dedicated.
const DEPTH_COVER = { RB: 1, WR: 1 }

/**
 * Per-position roster limits for one team.
 *
 * `min` is the count below which a drop is refused; `cap` is the count above
 * which an add is a replacement rather than an addition (null = uncapped).
 * Derived from the league's own slots except where POLICY_LIMITS applies, so
 * this holds for a 14-team and an 8-team league without tuning.
 *
 * @returns {Map<string, {min: number, cap: number|null}>}
 */
export function positionLimits(slots) {
  const starters = startersPerTeam(slots)
  const out = new Map()
  for (const [pos, n] of starters) {
    if (POLICY_LIMITS[pos]) {
      out.set(pos, { ...POLICY_LIMITS[pos] })
      continue
    }
    out.set(pos, { min: Math.ceil(n) + (DEPTH_COVER[pos] || 0), cap: null })
  }
  for (const [pos, lim] of Object.entries(POLICY_LIMITS)) {
    if (!out.has(pos)) out.set(pos, { ...lim })
  }
  return out
}

/**
 * How many players of each position the roster actually holds.
 *
 * IR slots are EXCLUDED. An IR'd quarterback does not cover you if the starter
 * is ruled out on Sunday morning, so he cannot count toward a minimum whose
 * whole purpose is being able to field a lineup.
 */
export function rosterCounts(slots, slotAssignments, poolById, projectionsById) {
  const counts = new Map()
  slots.forEach((slotName, i) => {
    if (slotName === 'IR') return
    const playerId = slotAssignments[i]
    if (!playerId) return
    const pos = poolById.get(playerId)?.position || projectionsById?.get(playerId)?.position
    if (!pos) return
    counts.set(pos, (counts.get(pos) || 0) + 1)
  })
  return counts
}

/**
 * Whether dropping one player at `pos` keeps the roster legal.
 *
 * Option (a) of the spec: the effective floor is the LOWER of the limit and
 * what is held today, so a position already under its limit is frozen rather
 * than reported as unfillable. Without this, a roster that is already thin at
 * RB would have every RB drop refused for being below a minimum it never met,
 * which reads as a bug to the user rather than as advice.
 */
export function canDropFrom(pos, counts, limits) {
  const held = counts.get(pos) || 0
  const limit = limits.get(pos)?.min
  if (limit === undefined) return held > 0
  return held - 1 >= Math.min(limit, held)
}

/** Sum of a player's expected points across the horizon. A week the player
 * does not appear in counts as ZERO, which is what a bye actually is -- and
 * is why a bye inside the horizon correctly makes someone look droppable. */
function horizonTotal(pointsByWeek, playerId) {
  const series = pointsByWeek.get(playerId)
  if (!series) return 0
  return series.reduce((sum, p) => sum + (typeof p === 'number' ? p : 0), 0)
}

/**
 * Which rostered player a waiver add should replace, if any.
 *
 * Two rules beyond "who scores least", both from real bad advice this used to
 * give:
 *
 *   1. Never an IR-slot player, and never anyone IR-eligible. See
 *      IR_ELIGIBLE_DESIGNATIONS.
 *   2. Judge on the HORIZON, not on this week. A starter with one hard
 *      matchup and a good month behind it should not be dropped for a
 *      streamer who wins a single week. When the candidate wins this week but
 *      loses over the horizon, that is reported as `hold` rather than
 *      silently suppressed -- the one-week gain is real and the user may
 *      still want it, they just should not be told it is an upgrade.
 *
 * The horizon is only as good as the future-week projections behind it.
 * Before any game is played those weeks are a positional baseline plus game
 * context, so the horizon mostly reflects schedule, not player quality; it
 * sharpens as real game logs accumulate. `weeks` is returned so the caller
 * can say how long a window it is talking about rather than implying more
 * precision than exists.
 *
 * @param {object} ctx
 * @param {Map<string, number[]>} ctx.pointsByWeek - playerId -> expected
 *   points per horizon week, index 0 being the week on screen.
 * @returns {{kind: 'empty-slot'|'replace'|'hold', ...}|null}
 */
export function waiverReplacement({
  position,
  slots,
  slotAssignments,
  poolById,
  projectionsById,
  pointsByWeek,
  candidateId,
  weeks,
  replacement,
}) {
  // Value over replacement at the player's OWN position. Without this a D/ST
  // at 33.5 outranks a WR at 19.0, which is not a real comparison -- see
  // replacementLevels. Falls back to the raw total when no levels are
  // supplied, which keeps this usable before the horizon has loaded.
  const surplusOf = (playerId) => {
    const total = horizonTotal(pointsByWeek, playerId)
    if (!replacement) return total
    const pos = poolById.get(playerId)?.position || projectionsById.get(playerId)?.position
    return total - (replacement.get(pos) || 0)
  }
  const nameOf = (playerId) =>
    projectionsById.get(playerId)?.name || poolById.get(playerId)?.name || playerId
  const posOf = (playerId) =>
    poolById.get(playerId)?.position || projectionsById.get(playerId)?.position
  const weekOf = (playerId) => {
    const v = pointsByWeek.get(playerId)?.[0]
    return typeof v === 'number' ? v : null
  }
  const blocked = (playerId, slotName) =>
    slotName === 'IR' ||
    IR_ELIGIBLE_DESIGNATIONS.includes(
      projectionsById.get(playerId)?.newsFlag?.designation ||
        poolById.get(playerId)?.newsFlag?.designation,
    )

  const limits = positionLimits(slots)
  const counts = rosterCounts(slots, slotAssignments, poolById, projectionsById)
  const cap = limits.get(position)?.cap ?? null

  const base = {
    weeks,
    adjusted: Boolean(replacement),
    inWeek: weekOf(candidateId),
    inHorizon: round1(horizonTotal(pointsByWeek, candidateId)),
    inSurplus: round1(surplusOf(candidateId)),
  }

  // -- Step 1: the position is already full. This is a same-position upgrade
  // question, not a drop-someone-else question -- which is exactly why a bench
  // QB is never offered up for a kicker: the two never meet here.
  if (cap !== null && (counts.get(position) || 0) >= cap) {
    let incumbent = null
    slots.forEach((slotName, i) => {
      const playerId = slotAssignments[i]
      if (!playerId || slotName === 'IR') return
      if (posOf(playerId) !== position) return
      const surplus = surplusOf(playerId)
      if (!incumbent || surplus < incumbent.surplus) {
        incumbent = { playerId, slotName, surplus, name: nameOf(playerId) }
      }
    })
    if (!incumbent) return null
    const shared = {
      ...base,
      name: incumbent.name,
      slotName: incumbent.slotName,
      outWeek: weekOf(incumbent.playerId),
      outHorizon: round1(horizonTotal(pointsByWeek, incumbent.playerId)),
      outSurplus: round1(incumbent.surplus),
      position,
    }
    if (surplusOf(candidateId) > incumbent.surplus) {
      return { kind: 'upgrade', gain: round1(surplusOf(candidateId) - incumbent.surplus), ...shared }
    }
    // Said out loud rather than returned as null: "no suggestion" reads as a
    // missing feature, "you already have better" is the actual answer.
    return { kind: 'have-better', ...shared }
  }

  // -- Step 2: cheapest LEGAL drop anywhere on the roster.
  let emptySlot = null
  let worst = null
  let blockedByMinimum = false

  for (let i = 0; i < slots.length; i++) {
    const slotName = slots[i]
    if (slotName === 'IR') continue
    if (!isPositionEligible(slotName, position)) continue

    const playerId = slotAssignments[i]
    if (!playerId) {
      if (!emptySlot) emptySlot = slotName
      continue
    }
    if (blocked(playerId, slotName)) continue

    const pos = posOf(playerId)
    if (pos && !canDropFrom(pos, counts, limits)) {
      // The case that started this: dropping the backup QB would leave one.
      blockedByMinimum = true
      continue
    }

    const surplus = surplusOf(playerId)
    if (!worst || surplus < worst.surplus) {
      worst = {
        slotName,
        name: nameOf(playerId),
        surplus,
        total: horizonTotal(pointsByWeek, playerId),
        thisWeek: weekOf(playerId),
      }
    }
  }

  if (emptySlot) return { kind: 'empty-slot', slotName: emptySlot }
  if (!worst) return blockedByMinimum ? { kind: 'no-legal-drop', ...base } : null

  const shared = {
    ...base,
    name: worst.name,
    slotName: worst.slotName,
    outWeek: worst.thisWeek,
    outHorizon: round1(worst.total),
    outSurplus: round1(worst.surplus),
  }

  if (surplusOf(candidateId) > worst.surplus) return { kind: 'replace', ...shared }
  // Wins the week, loses the window: the exact case worth NOT calling an
  // upgrade.
  if (base.inWeek !== null && worst.thisWeek !== null && base.inWeek > worst.thisWeek) {
    return { kind: 'hold', ...shared }
  }
  return null
}
