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
export function startingDemand(slots, teamCount) {
  const perTeam = new Map()
  const bump = (pos, n) => perTeam.set(pos, (perTeam.get(pos) || 0) + n)
  for (const slotName of slots) {
    if (!isStartingSlot(slotName)) continue
    const eligible = eligiblePositions(slotName)
    if (!eligible) continue // a null here would be an "any position" starter, which no roster has
    if (eligible.length === 1) bump(eligible[0], 1)
    else for (const pos of eligible) bump(pos, 1 / eligible.length)
  }
  const out = new Map()
  for (const [pos, n] of perTeam) out.set(pos, Math.max(1, Math.round(n * teamCount)))
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
  let emptySlot = null
  let worst = null
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

  for (let i = 0; i < slots.length; i++) {
    const slotName = slots[i]
    // An IR slot is not a drop target: it is filled from your own roster when
    // someone gets hurt. Checked before eligibility because IR reports itself
    // as eligible for every position.
    if (slotName === 'IR') continue
    if (!isPositionEligible(slotName, position)) continue

    const playerId = slotAssignments[i]
    if (!playerId) {
      if (!emptySlot) emptySlot = slotName
      continue
    }

    const entry = projectionsById.get(playerId)
    const designation = entry?.newsFlag?.designation || poolById.get(playerId)?.newsFlag?.designation
    if (IR_ELIGIBLE_DESIGNATIONS.includes(designation)) continue

    const name = entry?.name || poolById.get(playerId)?.name || playerId
    const total = horizonTotal(pointsByWeek, playerId)
    const surplus = surplusOf(playerId)
    const thisWeek = pointsByWeek.get(playerId)?.[0]
    // Least VALUABLE, not lowest scoring: the cheapest player to lose.
    if (!worst || surplus < worst.surplus) {
      worst = { slotName, name, total, surplus, thisWeek: typeof thisWeek === 'number' ? thisWeek : null }
    }
  }

  if (emptySlot) return { kind: 'empty-slot', slotName: emptySlot }
  if (!worst) return null

  const candidateTotal = horizonTotal(pointsByWeek, candidateId)
  const candidateSurplus = surplusOf(candidateId)
  const candidateWeek = pointsByWeek.get(candidateId)?.[0] ?? null
  const shared = {
    name: worst.name,
    slotName: worst.slotName,
    weeks,
    outWeek: worst.thisWeek,
    inWeek: candidateWeek,
    outHorizon: round1(worst.total),
    inHorizon: round1(candidateTotal),
    outSurplus: round1(worst.surplus),
    inSurplus: round1(candidateSurplus),
    // Whether the verdict came from replacement-adjusted value or raw totals,
    // so the caller can word it honestly.
    adjusted: Boolean(replacement),
  }

  if (candidateSurplus > worst.surplus) return { kind: 'replace', ...shared }
  // Wins the week, loses the window: the exact case worth NOT calling an
  // upgrade.
  if (candidateWeek !== null && worst.thisWeek !== null && candidateWeek > worst.thisWeek) {
    return { kind: 'hold', ...shared }
  }
  return null
}
