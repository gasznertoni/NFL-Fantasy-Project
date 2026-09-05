// Client-side persistence for the user's manually-configured fantasy roster
// (spec: docs/specs/team-config-and-roster-status.md). There is no backend
// -- this is the CLAUDE.md-documented v1 mechanism for entering a roster by
// hand (pulling it live from ESPN's private league API is explicitly out of
// scope). All localStorage reads/writes for team config MUST go through this
// module -- no component should touch `window.localStorage` directly
// (spec section 10, acceptance criteria).

import { useCallback, useEffect, useState } from 'react'
import { getDefaultTeamConfig } from './api.js'

// Legacy key used before dual-league support. Kept only for the one-time
// migration in useTeamConfig -- do not use it for new reads or writes.
const LEGACY_STORAGE_KEY = 'nfl-fantasy-assistant:team-config:v1'

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
    // `slots` is the slot array this config was SAVED against. Stamped from
    // v2 onward so a later shape change can re-map by name instead of
    // silently misaligning by index -- see loadTeamConfig. Absent on a
    // config written before the stamp existed.
    return {
      slotAssignments: parsed.slotAssignments,
      slots: Array.isArray(parsed.slots) ? parsed.slots : null,
    }
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
 * Sync localStorage read. Returns a null-filled array of the same length as
 * `slots` if nothing saved yet (caller -- in practice `useTeamConfig` below
 * -- is responsible for triggering the one-time seed from
 * getDefaultTeamConfig() on first run).
 * @param {string[]} slots - the CURRENT slot names, not just a count: a
 *   shape change is re-mapped by name, which needs the names.
 * @param {string} [leagueId]
 * @returns {{slotAssignments: (string|null)[]}}
 */
export function loadTeamConfig(slots, leagueId = 'league-1') {
  const slotNames = Array.isArray(slots) ? slots : []
  const stored = readRawConfig(leagueId)
  const assignments = Array(slotNames.length).fill(null)
  if (!stored) return { slotAssignments: assignments }

  const sameShape =
    stored.slots &&
    stored.slots.length === slotNames.length &&
    stored.slots.every((name, i) => name === slotNames[i])

  // Unstamped or unchanged -> straight index copy, the original behaviour.
  // Stamped and CHANGED -> re-map by slot name, because an index copy is
  // exactly what shifted a D/ST into a FLEX slot when the array grew.
  if (!stored.slots || sameShape) {
    for (let i = 0; i < Math.min(slotNames.length, stored.slotAssignments.length); i++) {
      assignments[i] = stored.slotAssignments[i]
    }
    return { slotAssignments: assignments }
  }
  return { slotAssignments: remapBySlotName(stored.slots, stored.slotAssignments, slotNames) }
}

/** Persists a config to localStorage for the given league. Fails silently if
 * localStorage is unavailable (private browsing, quota, etc.) -- the app
 * keeps working for the current session, it just won't persist across
 * reloads. */
export function saveTeamConfig(config, leagueId = 'league-1', slots = null) {
  try {
    const existing = slots ? null : readRawConfig(leagueId)
    const stamp = slots || existing?.slots || null
    window.localStorage.setItem(
      storageKey(leagueId),
      JSON.stringify({
        slotAssignments: config.slotAssignments || [],
        // Stamped so a later shape change re-maps by name. Carried forward
        // from the existing record when a caller does not supply it, so a
        // plain assign/clear never strips the stamp off a stamped config.
        ...(stamp ? { slots: stamp } : {}),
      }),
    )
  } catch {
    // see comment above
  }
}

/**
 * Assigns a player to a slot, persisting immediately. Reads whatever is
 * currently stored (extending it if the slot index is past its current
 * length), rather than requiring the caller to pass the full config.
 * @param {number} slotIndex
 * @param {string} playerId
 * @param {string} [leagueId]
 * @returns {{slotAssignments: (string|null)[]}} the updated config.
 */
export function assignPlayerToSlot(slotIndex, playerId, leagueId = 'league-1') {
  const assignments = readRawAssignments(leagueId) || []
  while (assignments.length <= slotIndex) assignments.push(null)
  assignments[slotIndex] = playerId
  const config = { slotAssignments: assignments }
  saveTeamConfig(config, leagueId)
  return config
}

/**
 * Clears a slot, persisting immediately.
 * @param {number} slotIndex
 * @param {string} [leagueId]
 * @returns {{slotAssignments: (string|null)[]}} the updated config.
 */
export function clearSlot(slotIndex, leagueId = 'league-1') {
  const assignments = readRawAssignments(leagueId) || []
  while (assignments.length <= slotIndex) assignments.push(null)
  assignments[slotIndex] = null
  const config = { slotAssignments: assignments }
  saveTeamConfig(config, leagueId)
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
 * empty, which makes this a move. Callers should gate on canSwapSlots --
 * this function does not re-check eligibility, because it has no pool to
 * check against and inventing one here would duplicate the view's.
 * @returns {{slotAssignments: (string|null)[]}} the updated config.
 */
export function swapSlots(indexA, indexB, leagueId = 'league-1') {
  const assignments = readRawAssignments(leagueId) || []
  const highest = Math.max(indexA, indexB)
  while (assignments.length <= highest) assignments.push(null)
  const tmp = assignments[indexA]
  assignments[indexA] = assignments[indexB]
  assignments[indexB] = tmp
  const config = { slotAssignments: assignments }
  saveTeamConfig(config, leagueId)
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
 * On first load, migrates any pre-dual-league config stored under the legacy
 * key `nfl-fantasy-assistant:team-config:v1` into the new
 * `nfl-fantasy-assistant:team-config:league-1:v1` key so previously saved
 * rosters are not lost after upgrading.
 *
 * @param {string[]} slots - the league's slot names; null/empty defers
 *   loading until known. Names rather than a count because a slot-array
 *   shape change is re-mapped by name (see loadTeamConfig).
 * @param {string} [leagueId] - which league's config to read/write.
 */
export function useTeamConfig(slots, leagueId = 'league-1') {
  const [config, setConfig] = useState(undefined)
  const [version, setVersion] = useState(0)

  // One-time migration: if the old single-league key exists and the new
  // league-1 key does not yet exist, copy and remove. Runs once on mount,
  // independent of slots/leagueId so it executes before any read.
  useEffect(() => {
    try {
      const oldValue = window.localStorage.getItem(LEGACY_STORAGE_KEY)
      const newKey = storageKey('league-1')
      if (oldValue && !window.localStorage.getItem(newKey)) {
        window.localStorage.setItem(newKey, oldValue)
        window.localStorage.removeItem(LEGACY_STORAGE_KEY)
      }
    } catch {
      // localStorage unavailable -- skip migration, no-op
    }
  }, [])

  // Join on the slot names themselves, not the array identity: a parent that
  // rebuilds `slots` each render would otherwise re-run this effect forever.
  const slotKey = Array.isArray(slots) ? slots.join('|') : ''

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
          saveTeamConfig(defaultConfig, leagueId, slotNames)
        }
      }
      if (!cancelled) {
        setConfig(loadTeamConfig(slotNames, leagueId))
      }
    }

    init()
    return () => {
      cancelled = true
    }
  }, [slotKey, leagueId, version])

  const refresh = useCallback(() => setVersion((v) => v + 1), [])

  const assign = useCallback(
    (slotIndex, playerId) => {
      assignPlayerToSlot(slotIndex, playerId, leagueId)
      refresh()
    },
    [leagueId, refresh],
  )

  const clear = useCallback(
    (slotIndex) => {
      clearSlot(slotIndex, leagueId)
      refresh()
    },
    [leagueId, refresh],
  )

  const swap = useCallback(
    (indexA, indexB) => {
      swapSlots(indexA, indexB, leagueId)
      refresh()
    },
    [leagueId, refresh],
  )

  const reset = useCallback(() => {
    resetTeamConfig(leagueId)
    refresh()
  }, [leagueId, refresh])

  return { config, assign, clear, swap, reset }
}
