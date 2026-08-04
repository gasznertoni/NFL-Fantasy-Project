// Client-side persistence for the user's manually-configured fantasy roster
// (spec: docs/specs/team-config-and-roster-status.md). There is no backend
// -- this is the CLAUDE.md-documented v1 mechanism for entering a roster by
// hand (pulling it live from ESPN's private league API is explicitly out of
// scope). All localStorage reads/writes for team config MUST go through this
// module -- no component should touch `window.localStorage` directly
// (spec section 10, acceptance criteria).

import { useCallback, useEffect, useState } from 'react'
import { getDefaultTeamConfig } from './api.js'

// Versioned so a future shape change doesn't need a migration path -- just
// bump the suffix and treat the old key as absent (spec section 3.5).
const STORAGE_KEY = 'nfl-fantasy-assistant:team-config:v1'

// Position eligibility per slot name (spec section 2). Exact-position slots
// map to a single-element list; FLEX allows the three flex-eligible
// positions; BENCH (and any unrecognized future slot name, per the
// documented fallback rule) allows anything, represented as `null`.
const SLOT_ELIGIBILITY = {
  QB: ['QB'],
  RB: ['RB'],
  WR: ['WR'],
  TE: ['TE'],
  DST: ['DST'],
  K: ['K'],
  FLEX: ['RB', 'WR', 'TE'],
}

/**
 * Position(s) eligible for a given slot name (spec section 2).
 * @param {string} slotName
 * @returns {string[]|null} eligible positions, or `null` meaning "any
 *   position" (BENCH, and the fallback for any slot name not in the map).
 */
export function eligiblePositions(slotName) {
  if (slotName === 'BENCH') return null
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

function readRawAssignments() {
  let raw
  try {
    raw = window.localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed?.slotAssignments) ? parsed.slotAssignments : null
  } catch {
    return null
  }
}

/** Whether a config has ever been saved to localStorage (used to decide
 * whether to seed from `getDefaultTeamConfig()` on first run). */
export function hasSavedTeamConfig() {
  return readRawAssignments() !== null
}

/**
 * Sync localStorage read. Returns a null-filled array of length `slotCount`
 * if nothing saved yet (caller -- in practice `useTeamConfig` below -- is
 * responsible for triggering the one-time seed from getDefaultTeamConfig()
 * on first run). Normalizes the stored array to `slotCount` entries so a
 * future change to the number of roster slots doesn't crash on a
 * length mismatch.
 * @param {number} slotCount
 * @returns {{slotAssignments: (string|null)[]}}
 */
export function loadTeamConfig(slotCount) {
  const raw = readRawAssignments()
  const assignments = Array(slotCount).fill(null)
  if (raw) {
    for (let i = 0; i < Math.min(slotCount, raw.length); i++) {
      assignments[i] = raw[i]
    }
  }
  return { slotAssignments: assignments }
}

/** Persists a config to localStorage. Fails silently if localStorage is
 * unavailable (private browsing, quota, etc.) -- the app keeps working for
 * the current session, it just won't persist across reloads. */
export function saveTeamConfig(config) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ slotAssignments: config.slotAssignments || [] }))
  } catch {
    // see comment above
  }
}

/**
 * Assigns a player to a slot, persisting immediately. Reads whatever is
 * currently stored (extending it if the slot index is past its current
 * length), rather than requiring the caller to pass the full config.
 * @returns {{slotAssignments: (string|null)[]}} the updated config.
 */
export function assignPlayerToSlot(slotIndex, playerId) {
  const assignments = readRawAssignments() || []
  while (assignments.length <= slotIndex) assignments.push(null)
  assignments[slotIndex] = playerId
  const config = { slotAssignments: assignments }
  saveTeamConfig(config)
  return config
}

/**
 * Clears a slot, persisting immediately.
 * @returns {{slotAssignments: (string|null)[]}} the updated config.
 */
export function clearSlot(slotIndex) {
  const assignments = readRawAssignments() || []
  while (assignments.length <= slotIndex) assignments.push(null)
  assignments[slotIndex] = null
  const config = { slotAssignments: assignments }
  saveTeamConfig(config)
  return config
}

/** Clears the saved configuration entirely. The next load re-seeds from
 * `getDefaultTeamConfig()` (see `useTeamConfig` below). */
export function resetTeamConfig() {
  try {
    window.localStorage.removeItem(STORAGE_KEY)
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
 * @param {number} slotCount - 0/undefined defers loading until known.
 */
export function useTeamConfig(slotCount) {
  const [config, setConfig] = useState(undefined)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    if (!slotCount) return
    let cancelled = false

    async function init() {
      if (!hasSavedTeamConfig()) {
        const defaultConfig = await getDefaultTeamConfig()
        if (!cancelled && defaultConfig && Array.isArray(defaultConfig.slotAssignments)) {
          saveTeamConfig(defaultConfig)
        }
      }
      if (!cancelled) {
        setConfig(loadTeamConfig(slotCount))
      }
    }

    init()
    return () => {
      cancelled = true
    }
  }, [slotCount, version])

  const refresh = useCallback(() => setVersion((v) => v + 1), [])

  const assign = useCallback(
    (slotIndex, playerId) => {
      assignPlayerToSlot(slotIndex, playerId)
      refresh()
    },
    [refresh],
  )

  const clear = useCallback(
    (slotIndex) => {
      clearSlot(slotIndex)
      refresh()
    },
    [refresh],
  )

  const reset = useCallback(() => {
    resetTeamConfig()
    refresh()
  }, [refresh])

  return { config, assign, clear, reset }
}
