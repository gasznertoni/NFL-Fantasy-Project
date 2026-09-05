// Lineup-level arithmetic for the Weekly Report header: what the STARTING
// lineup is projected to score, and whether a better one is sitting on the
// bench. Pure functions with no React and no fetching, so the numbers can be
// checked directly rather than only through the rendered page.

import { isPositionEligible, NON_STARTING_SLOTS } from './teamConfig.js'

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
