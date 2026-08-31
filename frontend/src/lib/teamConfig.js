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

// Versioned per-league storage key. Bumping the suffix in a future shape
// change means the old key is treated as absent (spec section 3.5).
function storageKey(leagueId) {
  return `nfl-fantasy-assistant:team-config:${leagueId}:v1`
}

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

function readRawAssignments(leagueId) {
  let raw
  try {
    raw = window.localStorage.getItem(storageKey(leagueId))
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

/** Whether a config has ever been saved to localStorage for the given league
 * (used to decide whether to seed from `getDefaultTeamConfig()` on first
 * run). */
export function hasSavedTeamConfig(leagueId = 'league-1') {
  return readRawAssignments(leagueId) !== null
}

/**
 * Sync localStorage read. Returns a null-filled array of length `slotCount`
 * if nothing saved yet (caller -- in practice `useTeamConfig` below -- is
 * responsible for triggering the one-time seed from getDefaultTeamConfig()
 * on first run). Normalizes the stored array to `slotCount` entries so a
 * future change to the number of roster slots doesn't crash on a
 * length mismatch.
 * @param {number} slotCount
 * @param {string} [leagueId]
 * @returns {{slotAssignments: (string|null)[]}}
 */
export function loadTeamConfig(slotCount, leagueId = 'league-1') {
  const raw = readRawAssignments(leagueId)
  const assignments = Array(slotCount).fill(null)
  if (raw) {
    for (let i = 0; i < Math.min(slotCount, raw.length); i++) {
      assignments[i] = raw[i]
    }
  }
  return { slotAssignments: assignments }
}

/** Persists a config to localStorage for the given league. Fails silently if
 * localStorage is unavailable (private browsing, quota, etc.) -- the app
 * keeps working for the current session, it just won't persist across
 * reloads. */
export function saveTeamConfig(config, leagueId = 'league-1') {
  try {
    window.localStorage.setItem(
      storageKey(leagueId),
      JSON.stringify({ slotAssignments: config.slotAssignments || [] }),
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
 * @param {number} slotCount - 0/undefined defers loading until known.
 * @param {string} [leagueId] - which league's config to read/write.
 */
export function useTeamConfig(slotCount, leagueId = 'league-1') {
  const [config, setConfig] = useState(undefined)
  const [version, setVersion] = useState(0)

  // One-time migration: if the old single-league key exists and the new
  // league-1 key does not yet exist, copy and remove. Runs once on mount,
  // independent of slotCount/leagueId so it executes before any read.
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

  useEffect(() => {
    if (!slotCount) return
    let cancelled = false

    async function init() {
      if (!hasSavedTeamConfig(leagueId)) {
        const defaultConfig = await getDefaultTeamConfig(leagueId)
        if (!cancelled && defaultConfig && Array.isArray(defaultConfig.slotAssignments)) {
          saveTeamConfig(defaultConfig, leagueId)
        }
      }
      if (!cancelled) {
        setConfig(loadTeamConfig(slotCount, leagueId))
      }
    }

    init()
    return () => {
      cancelled = true
    }
  }, [slotCount, leagueId, version])

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

  const reset = useCallback(() => {
    resetTeamConfig(leagueId)
    refresh()
  }, [leagueId, refresh])

  return { config, assign, clear, reset }
}
