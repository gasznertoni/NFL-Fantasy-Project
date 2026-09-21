// Client-side persistence for the user's manually-configured fantasy roster
// (spec: docs/specs/team-config-and-roster-status.md). There is no backend
// -- this is the CLAUDE.md-documented v1 mechanism for entering a roster by
// hand (pulling it live from ESPN's private league API is explicitly out of
// scope). All localStorage reads/writes for team config MUST go through this
// module -- no component should touch `window.localStorage` directly
// (spec section 10, acceptance criteria).

import { useCallback, useEffect, useState } from 'react'
import { getDefaultTeamConfig } from './api.js'

// Versioned per-league storage key. Bumping the suffix on a shape change
// means the old key is treated as absent (spec section 3.5).
//
// v1 -> v2 on 2026-09-05. slotAssignments maps to slots BY INDEX, and the
// slot array changed shape twice without this suffix moving: a second FLEX
// went in at index 7, then two IR slots on the end. Every saved config
// therefore had its tail shifted one place right and rendered the wrong
// label against the right player -- a D/ST sitting in a slot headed FLEX, a
// kicker under DST, a bench QB under K. The data was never wrong; the join
// was. Bumping discards those configs and re-seeds from
// default-team-config.json, which now holds the real roster.
//
// The pre-dual-league key `nfl-fantasy-assistant:team-config:v1` is
// deliberately NOT migrated, and nothing in this module reads it. It used to
// be copied into `storageKey('league-1')` on mount -- written when that
// expression still ended in `:v1`, and never revisited when the suffix moved
// to `:v2`. The copy therefore smuggled exactly the configs the bump exists
// to discard back in under the new key, unstamped, and an unstamped record
// is joined by index (see loadTeamConfig). Against league-1's current shape
// that put a WR in the DST slot and a D/ST in the K slot. Leave it unread.
function storageKey(leagueId) {
  return `nfl-fantasy-assistant:team-config:${leagueId}:v2`
}

// Position eligibility per slot name (spec section 2). Exact-position slots
// map to a single-element list; FLEX allows the three flex-eligible
// positions; BENCH and IR (and any unrecognized future slot name, per the
// documented fallback rule) allow anything, represented as `null`.
const SLOT_ELIGIBILITY = {
  QB: ['QB'],
  RB: ['RB'],
  WR: ['WR'],
  TE: ['TE'],
  DST: ['DST'],
  K: ['K'],
  FLEX: ['RB', 'WR', 'TE'],
}

// Slots that hold a player without starting him. Listed explicitly rather
// than left to the "unknown slot name" fallback, because these two are the
// difference between a lineup slot and a roster slot and every consumer has
// to treat them differently -- see NON_STARTING_SLOTS' use in
// WeeklyReportView, which must not render them as starting groups.
//
// IR takes ANY POSITION here. A real IR slot constrains by STATUS (only an
// injured player may occupy one), not by position, and status eligibility is
// a concept this module does not have -- `eligiblePositions` answers a
// position question. Enforcing it off `newsFlag.designation` was considered
// and rejected: that feed is a live scrape, it is empty for most of the pool
// pre-season, and a wrong "not eligible" would lock a user out of a slot on
// the strength of a missing field. Left unconstrained and documented.
export const NON_STARTING_SLOTS = ['BENCH', 'IR']

/**
 * Position(s) eligible for a given slot name (spec section 2).
 * @param {string} slotName
 * @returns {string[]|null} eligible positions, or `null` meaning "any
 *   position" (BENCH, and the fallback for any slot name not in the map).
 */
export function eligiblePositions(slotName) {
  if (NON_STARTING_SLOTS.includes(slotName)) return null
  return SLOT_ELIGIBILITY[slotName] || null
}

/**
 * Whether a `slotAssignments` array considers a given position eligible for
 * a slot (helper wrapping eligiblePositions' null-means-any-position rule).
 */
export function isPositionEligible(slotName, position) {
  const eligible = eligiblePositions(slotName)
  return eligible === null || eligible.includes(position)
}

function readRawConfig(leagueId) {
  let raw
  try {
    raw = window.localStorage.getItem(storageKey(leagueId))
  } catch {
    return null
  }
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed?.slotAssignments)) return null
    // `slots` is the slot array this config was SAVED against, and it is
    // MANDATORY: every writer in this module stamps it, so a record without
    // one did not come from a writer this module still has. Treating it as
    // absent (rather than index-joining it against whatever the slot array
    // looks like today) is what the v2 bump was for -- the caller re-seeds
    // from default-team-config.json instead, which is the real roster.
    if (!Array.isArray(parsed.slots)) return null
    return { slotAssignments: parsed.slotAssignments, slots: parsed.slots }
  } catch {
    return null
  }
}

function readRawAssignments(leagueId) {
  return readRawConfig(leagueId)?.slotAssignments || null
}

/**
 * Re-seats saved assignments when the slot array changes shape, matching on
 * SLOT NAME rather than index. Players whose slot no longer exists (or whose
 * slot lost a copy) fall to a free BENCH, then IR, rather than vanishing.
 * This is what makes a future slot change safe without another version bump.
 */
function remapBySlotName(oldSlots, oldAssignments, newSlots) {
  const out = Array(newSlots.length).fill(null)
  const taken = new Set()
  const displaced = []
  for (let i = 0; i < oldSlots.length; i++) {
    const playerId = oldAssignments[i]
    if (!playerId) continue
    const target = newSlots.findIndex((name, j) => name === oldSlots[i] && !taken.has(j))
    if (target === -1) {
      displaced.push(playerId)
      continue
    }
    out[target] = playerId
    taken.add(target)
  }
  for (const playerId of displaced) {
    const target = newSlots.findIndex((name, j) => NON_STARTING_SLOTS.includes(name) && !taken.has(j))
    if (target === -1) continue // genuinely no room left; dropping is the only option
    out[target] = playerId
    taken.add(target)
  }
  return out
}

/** Whether a config has ever been saved to localStorage for the given league
 * (used to decide whether to seed from `getDefaultTeamConfig()` on first
 * run). */
export function hasSavedTeamConfig(leagueId = 'league-1') {
  return readRawAssignments(leagueId) !== null
}

/**
 * Moves anyone sitting in a slot his position is not eligible for to a free
 * BENCH (then IR), dropping him only when the roster has no room at all.
 *
 * Belt to remapBySlotName's braces. The remap preserves eligibility on its
 * own -- it re-seats by slot NAME, and a displaced player only ever lands on
 * a non-starting slot -- so with a well-formed record this is a no-op. It
 * exists for records that were ALREADY corrupt when they were read: the
 * legacy-key migration described above wrote league-1 configs that put a WR
 * in the DST slot, and those are sitting in real browsers today. Healing on
 * read means the user sees a legal roster without being told to press
 * "Reset to default".
 *
 * A player MISSING from the pool is left exactly where he is. His position
 * is unknown, not wrong, and the view already renders that state as
 * "Assigned player not found in pool" -- silently dropping him on a failed
 * lookup would turn a lookup miss into data loss.
 *
 * @param {string[]} slots
 * @param {(string|null)[]} assignments
 * @param {Map<string, {position: string}>} poolById
 * @returns {(string|null)[]} a new array; the input is not mutated.
 */
export function sanitizeAssignments(slots, assignments, poolById) {
  if (!poolById || poolById.size === 0) return assignments
  const out = [...assignments]
  const evicted = []
  for (let i = 0; i < slots.length; i++) {
    const playerId = out[i]
    if (!playerId) continue
    const entry = poolById.get(playerId)
    if (!entry) continue // unknown position -- see docblock
    if (isPositionEligible(slots[i], entry.position)) continue
    out[i] = null
    evicted.push(playerId)
  }
  for (const playerId of evicted) {
    const target = slots.findIndex((name, j) => NON_STARTING_SLOTS.includes(name) && !out[j])
    if (target === -1) continue // no room; dropping is the only option left
    out[target] = playerId
  }
  return out
}

/**
 * Sync localStorage read. Returns a null-filled array of the same length as
 * `slots` if nothing saved yet (caller -- in practice `useTeamConfig` below
 * -- is responsible for triggering the one-time seed from
 * getDefaultTeamConfig() on first run).
 *
 * The array this returns is indexed by the CURRENT `slots`, which is the one
 * coordinate system the rest of the app uses. Every writer in this module
 * goes through here first for exactly that reason -- see assignPlayerToSlot.
 *
 * @param {string[]} slots - the CURRENT slot names, not just a count: a
 *   shape change is re-mapped by name, which needs the names.
 * @param {string} [leagueId]
 * @param {Map<string, {position: string}>} [poolById] - when supplied, the
 *   result is also run through sanitizeAssignments.
 * @returns {{slotAssignments: (string|null)[]}}
 */
export function loadTeamConfig(slots, leagueId = 'league-1', poolById = null) {
  const slotNames = Array.isArray(slots) ? slots : []
  const stored = readRawConfig(leagueId)
  if (!stored) return { slotAssignments: Array(slotNames.length).fill(null) }

  const sameShape =
    stored.slots.length === slotNames.length &&
    stored.slots.every((name, i) => name === slotNames[i])

  // Unchanged shape -> straight index copy. CHANGED -> re-map by slot name,
  // because an index copy is exactly what shifted a D/ST into a FLEX slot
  // when the array grew. readRawConfig guarantees the stamp is present, so
  // there is no third "we don't know what this was saved against" case.
  let assignments
  if (sameShape) {
    assignments = Array(slotNames.length).fill(null)
    for (let i = 0; i < Math.min(slotNames.length, stored.slotAssignments.length); i++) {
      assignments[i] = stored.slotAssignments[i]
    }
  } else {
    assignments = remapBySlotName(stored.slots, stored.slotAssignments, slotNames)
  }
  return { slotAssignments: sanitizeAssignments(slotNames, assignments, poolById) }
}

/** Persists a config to localStorage for the given league. Fails silently if
 * localStorage is unavailable (private browsing, quota, etc.) -- the app
 * keeps working for the current session, it just won't persist across
 * reloads.
 *
 * `slots` is the slot array `config.slotAssignments` is indexed by, and it
 * is written as the record's stamp. It is REQUIRED: readRawConfig discards
 * an unstamped record, so writing one is the same as deleting the roster.
 * It used to be optional, carried forward from whatever was already stored
 * -- which is how a record kept an obsolete stamp across every later edit
 * and never healed. */
export function saveTeamConfig(config, leagueId = 'league-1', slots = null) {
  const stamp = Array.isArray(slots) ? slots : readRawConfig(leagueId)?.slots
  if (!Array.isArray(stamp)) return
  try {
    window.localStorage.setItem(
      storageKey(leagueId),
      JSON.stringify({ slotAssignments: config.slotAssignments || [], slots: stamp }),
    )
  } catch {
    // see comment above
  }
}

/**
 * The assignments a mutation should operate on: the stored record re-seated
 * onto the CURRENT slot array, which is the same array the UI renders and
 * therefore the same one its slot indices count against.
 *
 * Every writer starts here. The three of them used to start from
 * `readRawAssignments()` instead -- the record exactly as stored -- and
 * index into it with an index the caller had counted against the slots on
 * SCREEN. While the stored stamp matched the current slots those are the
 * same number; once the slot array changed shape they are two different
 * coordinate systems, and the read path silently reconciled them while the
 * write path did not. Assigning to league-1's FLEX then wrote into the
 * stored TE slot: the new player surfaced under the TE heading and the tight
 * end who had been there was overwritten without a word.
 *
 * `poolById` matters for the same reason: loadTeamConfig heals a corrupt
 * record when it can see positions, so reading WITHOUT the pool here would
 * mutate a different array than the one on screen -- clicking Remove on the
 * bench slot the sanitizer parked someone in would clear whoever the
 * unhealed array has at that index, and write the corruption straight back.
 */
function currentAssignments(slots, leagueId, poolById) {
  return loadTeamConfig(slots, leagueId, poolById).slotAssignments
}

/** Whether `slotIndex` addresses a real slot. An out-of-range index used to
 * grow the stored array past the slot array, which is how the two drifted
 * apart in the first place; it is now simply refused. */
function inRange(slots, slotIndex) {
  return Number.isInteger(slotIndex) && slotIndex >= 0 && slotIndex < slots.length
}

/**
 * Assigns a player to a slot, persisting immediately.
 *
 * Refuses a player whose position the slot does not take, when `poolById`
 * makes the position knowable. The picker in TeamConfigView already offers
 * only eligible players, so this should never fire from the UI -- it is here
 * because "a WR cannot sit in the DST slot" is a property of the roster, and
 * a rule enforced only by the list you happen to render is not enforced.
 *
 * @param {string[]} slots - the slot array `slotIndex` counts against.
 * @param {number} slotIndex
 * @param {string} playerId
 * @param {string} [leagueId]
 * @param {Map<string, {position: string}>} [poolById]
 * @returns {{slotAssignments: (string|null)[]}} the updated config.
 */
export function assignPlayerToSlot(slots, slotIndex, playerId, leagueId = 'league-1', poolById = null) {
  const assignments = currentAssignments(slots, leagueId, poolById)
  if (!inRange(slots, slotIndex)) return { slotAssignments: assignments }
  const position = poolById?.get(playerId)?.position
  if (position && !isPositionEligible(slots[slotIndex], position)) {
    return { slotAssignments: assignments }
  }
  assignments[slotIndex] = playerId
  const config = { slotAssignments: assignments }
  saveTeamConfig(config, leagueId, slots)
  return config
}

/**
 * Clears a slot, persisting immediately.
 * @param {string[]} slots - the slot array `slotIndex` counts against.
 * @param {number} slotIndex
 * @param {string} [leagueId]
 * @param {Map<string, {position: string}>} [poolById]
 * @returns {{slotAssignments: (string|null)[]}} the updated config.
 */
export function clearSlot(slots, slotIndex, leagueId = 'league-1', poolById = null) {
  const assignments = currentAssignments(slots, leagueId, poolById)
  if (!inRange(slots, slotIndex)) return { slotAssignments: assignments }
  assignments[slotIndex] = null
  const config = { slotAssignments: assignments }
  saveTeamConfig(config, leagueId, slots)
  return config
}

/**
 * Whether the players in two slots can trade places. Checks eligibility in
 * BOTH directions, which is the whole point: moving a WR into a FLEX is
 * fine, but the RB coming back the other way has to be legal for the WR
 * slot he lands in. A one-way check would make swap a hole through which
 * any player reaches any slot.
 *
 * An empty target slot is a move rather than a swap, so only the moving
 * player is checked. Same index twice is a no-op, allowed.
 *
 * @param {string[]} slots - slot names, index-aligned with `assignments`.
 * @param {(string|null)[]} assignments
 * @param {Map<string, {position: string}>} poolById - player lookup.
 * @returns {boolean}
 */
export function canSwapSlots(slots, assignments, poolById, indexA, indexB) {
  if (indexA === indexB) return true
  if (!slots[indexA] || !slots[indexB]) return false
  const a = assignments[indexA] ? poolById.get(assignments[indexA]) : null
  const b = assignments[indexB] ? poolById.get(assignments[indexB]) : null
  // A player assigned but missing from the pool has no known position, so
  // eligibility is unknowable -- refuse rather than guess. The view already
  // renders that state as "Assigned player not found in pool".
  if (assignments[indexA] && !a) return false
  if (assignments[indexB] && !b) return false
  if (a && !isPositionEligible(slots[indexB], a.position)) return false
  if (b && !isPositionEligible(slots[indexA], b.position)) return false
  return true
}

/**
 * Swaps the contents of two slots, persisting once. Either slot may be
 * empty, which makes this a move.
 *
 * Gated on canSwapSlots when `poolById` is supplied, and otherwise on the
 * caller having done so. The check is cheap and the cost of missing it is a
 * player parked in a slot his position cannot hold, which is the whole class
 * of bug this module has just been through.
 *
 * @param {string[]} slots - the slot array both indices count against.
 * @returns {{slotAssignments: (string|null)[]}} the updated config.
 */
export function swapSlots(slots, indexA, indexB, leagueId = 'league-1', poolById = null) {
  const assignments = currentAssignments(slots, leagueId, poolById)
  if (!inRange(slots, indexA) || !inRange(slots, indexB)) return { slotAssignments: assignments }
  if (poolById && !canSwapSlots(slots, assignments, poolById, indexA, indexB)) {
    return { slotAssignments: assignments }
  }
  const tmp = assignments[indexA]
  assignments[indexA] = assignments[indexB]
  assignments[indexB] = tmp
  const config = { slotAssignments: assignments }
  saveTeamConfig(config, leagueId, slots)
  return config
}

/** Clears the saved configuration entirely for the given league. The next
 * load re-seeds from `getDefaultTeamConfig()` (see `useTeamConfig` below). */
export function resetTeamConfig(leagueId = 'league-1') {
  try {
    window.localStorage.removeItem(storageKey(leagueId))
  } catch {
    // see saveTeamConfig comment
  }
}

/** Whether `playerId` is assigned to any slot (starting or bench) in the
 * given config -- used by the Waiver Targets exclusion filter (spec section
 * 5) and by the picker's "already rostered elsewhere" filter (spec section
 * 6). */
export function isPlayerRostered(playerId, config) {
  return Boolean(config?.slotAssignments?.includes(playerId))
}

/**
 * React hook wrapping the load/seed lifecycle so both WeeklyReportView and
 * TeamConfigView share one implementation: on first run (no saved config),
 * fetch `getDefaultTeamConfig()` and save it before reading, so the seeded
 * default team is visible without an extra reload; on every run after that,
 * just read what's saved. Returns the current config (`undefined` while
 * loading) plus mutator helpers that persist and refresh local state.
 *
 * There is no migration from the pre-dual-league key -- see the comment on
 * storageKey for why copying it forward was the bug rather than the feature.
 *
 * @param {string[]} slots - the league's slot names; null/empty defers
 *   loading until known. Names rather than a count because a slot-array
 *   shape change is re-mapped by name (see loadTeamConfig). This is also the
 *   coordinate system the returned mutators' slot indices count against.
 * @param {string} [leagueId] - which league's config to read/write.
 * @param {Map<string, {position: string}>} [poolById] - when supplied, the
 *   config is healed of position-illegal placements on read and the mutators
 *   refuse to create new ones. Callers that have the pool should pass it;
 *   the hook re-reads once it arrives.
 */
export function useTeamConfig(slots, leagueId = 'league-1', poolById = null) {
  const [config, setConfig] = useState(undefined)
  const [version, setVersion] = useState(0)

  // Join on the slot names themselves, not the array identity: a parent that
  // rebuilds `slots` each render would otherwise re-run this effect forever.
  const slotKey = Array.isArray(slots) ? slots.join('|') : ''
  // Same reasoning for the pool: re-read when it ARRIVES (null -> loaded),
  // not on every render of a parent that rebuilt the Map.
  const poolSize = poolById?.size ?? 0

  useEffect(() => {
    if (!slotKey) return
    let cancelled = false
    const slotNames = slotKey.split('|')

    async function init() {
      if (!hasSavedTeamConfig(leagueId)) {
        const defaultConfig = await getDefaultTeamConfig(leagueId)
        if (!cancelled && defaultConfig && Array.isArray(defaultConfig.slotAssignments)) {
          // Stamp the seed with the slots it was built for, so the very first
          // saved config is already shape-aware.
          //
          // `slotNames` has to actually BE this league's slots, and on a
          // league switch it briefly is not: leagueId flips the moment the
          // user picks from the dropdown, while the new roster-slots.json is
          // still in flight, so this effect runs once with the new league and
          // the OLD league's slot array. Seeding there stamped league-2's
          // 17-entry roster with league-1's 13 slots, and every later read
          // re-seated it by name against a stamp it never had -- a WR under
          // DST, a RB under K. default-team-config.json is one entry per slot
          // by contract, so a length disagreement means these two do not
          // belong together; skip, and let the run with the right slots do it.
          if (defaultConfig.slotAssignments.length === slotNames.length) {
            saveTeamConfig(defaultConfig, leagueId, slotNames)
          } else {
            // Loud rather than silent: if the fixtures themselves disagree
            // this never resolves, and an empty roster with no explanation is
            // the kind of miss this repo keeps paying for.
            console.warn(
              `[teamConfig] skipped seeding ${leagueId}: default-team-config.json has ` +
                `${defaultConfig.slotAssignments.length} entries, roster-slots.json has ` +
                `${slotNames.length}.`,
            )
          }
        }
      }
      if (!cancelled) {
        setConfig(loadTeamConfig(slotNames, leagueId, poolById))
      }
    }

    init()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slotKey, leagueId, version, poolSize])

  const refresh = useCallback(() => setVersion((v) => v + 1), [])

  const assign = useCallback(
    (slotIndex, playerId) => {
      assignPlayerToSlot(slots, slotIndex, playerId, leagueId, poolById)
      refresh()
    },
    [slots, leagueId, poolById, refresh],
  )

  const clear = useCallback(
    (slotIndex) => {
      clearSlot(slots, slotIndex, leagueId, poolById)
      refresh()
    },
    [slots, leagueId, poolById, refresh],
  )

  const swap = useCallback(
    (indexA, indexB) => {
      swapSlots(slots, indexA, indexB, leagueId, poolById)
      refresh()
    },
    [slots, leagueId, poolById, refresh],
  )

  const reset = useCallback(() => {
    resetTeamConfig(leagueId)
    refresh()
  }, [leagueId, refresh])

  return { config, assign, clear, swap, reset }
}
