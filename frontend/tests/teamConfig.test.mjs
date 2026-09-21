// Regression tests for lib/teamConfig.js' slot <-> player join.
//
// Every test here encodes a symptom that was live in the app on 2026-09-20,
// reported as "in the AH football league my team is bugged, I could assign a
// WR to DST". Two independent defects produced it, and both are covered:
//
//   A. The pre-dual-league key `nfl-fantasy-assistant:team-config:v1` was
//      copied verbatim into `storageKey('league-1')` on mount. That helper
//      ended in `:v1` when the migration was written and in `:v2` by the time
//      this shipped, so the copy smuggled exactly the configs the v2 bump
//      exists to discard back in under the new key -- and unstamped, which
//      sent them down the index-join branch. Against league-1's current slot
//      array that seats a WR under DST and a D/ST under K.
//
//   B. The read path re-seated a stale record onto the current slots while
//      the write path did not. `assignPlayerToSlot` indexed the array AS
//      STORED with an index counted against the slots ON SCREEN; once the
//      slot array changed shape those are two different coordinate systems.
//
// No test runner dependency: `node --test` (Node >= 18) runs this directly.
// The module reads `window.localStorage` lazily inside its functions, so the
// stub below only has to exist before the first CALL, not before the import
// -- but it is installed pre-import anyway so the order is never load-bearing.

import test from 'node:test'
import assert from 'node:assert/strict'

const store = new Map()
globalThis.window = {
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  },
}

const {
  loadTeamConfig,
  saveTeamConfig,
  hasSavedTeamConfig,
  assignPlayerToSlot,
  clearSlot,
  swapSlots,
  sanitizeAssignments,
  isPositionEligible,
} = await import('../src/lib/teamConfig.js')

const KEY = 'nfl-fantasy-assistant:team-config:league-1:v2'
const LEGACY_KEY = 'nfl-fantasy-assistant:team-config:v1'

// league-1 as of 2026-09-06: 1 QB, 1 RB, 1 WR, 1 TE, 2 FLEX, 1 DST, 1 K,
// 4 BN, 1 IR.
const SLOTS = ['QB', 'RB', 'WR', 'TE', 'FLEX', 'FLEX', 'DST', 'K', 'BENCH', 'BENCH', 'BENCH', 'BENCH', 'IR']
// The shape league-1 carried before that -- league-2's, by mistake.
const OLD_SLOTS = ['QB', 'RB', 'RB', 'WR', 'WR', 'TE', 'FLEX', 'DST', 'K', 'BENCH', 'BENCH', 'BENCH', 'BENCH']

const POOL = new Map(
  Object.entries({
    qb1: 'QB', qb2: 'QB',
    rb1: 'RB', rb2: 'RB', rb3: 'RB',
    wr1: 'WR', wr2: 'WR', wr3: 'WR', wr4: 'WR',
    te1: 'TE', te2: 'TE',
    dst1: 'DST',
    k1: 'K',
  }).map(([playerId, position]) => [playerId, { playerId, position }]),
)

// Legal under OLD_SLOTS, index for index.
const OLD_ROSTER = ['qb1', 'rb1', 'rb2', 'wr1', 'wr2', 'te1', 'wr3', 'dst1', 'k1', 'rb3', 'wr4', 'te2', 'qb2']

function reset() {
  store.clear()
}

/** Every occupied slot holds a player its position is eligible for. */
function assertAllSlotsLegal(slots, assignments, label) {
  slots.forEach((slotName, i) => {
    const playerId = assignments[i]
    if (!playerId) return
    const position = POOL.get(playerId)?.position
    if (!position) return
    assert.ok(
      isPositionEligible(slotName, position),
      `${label}: ${playerId} (${position}) is not eligible for slot ${i} (${slotName})`,
    )
  })
}

// ---------------------------------------------------------------------------
// Defect A -- a record with no slot stamp is not trustworthy
// ---------------------------------------------------------------------------

test('an unstamped record is discarded rather than index-joined', () => {
  reset()
  // Exactly what the legacy migration used to write: a v1 payload, no stamp.
  store.set(KEY, JSON.stringify({ slotAssignments: OLD_ROSTER }))

  assert.equal(hasSavedTeamConfig('league-1'), false, 'must re-seed from the default instead')
  const { slotAssignments } = loadTeamConfig(SLOTS, 'league-1')
  assert.deepEqual(slotAssignments, Array(SLOTS.length).fill(null))
})

test('the reported symptom: no WR ends up in the DST slot', () => {
  reset()
  store.set(KEY, JSON.stringify({ slotAssignments: OLD_ROSTER }))

  const { slotAssignments } = loadTeamConfig(SLOTS, 'league-1', POOL)
  const dstIndex = SLOTS.indexOf('DST')
  // Pre-fix this index-joined OLD_ROSTER[6] -- 'wr3' -- into the DST slot,
  // and OLD_ROSTER[7] -- the D/ST itself -- into K.
  assert.equal(slotAssignments[dstIndex], null)
  assertAllSlotsLegal(SLOTS, slotAssignments, 'unstamped legacy record')
})

test('the legacy key is never read', () => {
  reset()
  store.set(LEGACY_KEY, JSON.stringify({ slotAssignments: OLD_ROSTER }))
  const read = []
  const realGetItem = globalThis.window.localStorage.getItem
  globalThis.window.localStorage.getItem = (k) => {
    read.push(k)
    return realGetItem(k)
  }
  try {
    loadTeamConfig(SLOTS, 'league-1')
    hasSavedTeamConfig('league-1')
    assignPlayerToSlot(SLOTS, 0, 'qb1', 'league-1', POOL)
  } finally {
    globalThis.window.localStorage.getItem = realGetItem
  }
  assert.ok(!read.includes(LEGACY_KEY), `read the legacy key: ${read.join(', ')}`)
  // The assign above legitimately writes a fresh, correctly-stamped record.
  // What must never happen is the legacy ROSTER turning up inside it.
  const stored = JSON.parse(store.get(KEY))
  assert.deepEqual(stored.slots, SLOTS)
  for (const playerId of OLD_ROSTER.slice(1)) {
    assert.ok(!stored.slotAssignments.includes(playerId), `${playerId} came from the legacy key`)
  }
  assert.equal(store.get(LEGACY_KEY), JSON.stringify({ slotAssignments: OLD_ROSTER }), 'left untouched')
})

// ---------------------------------------------------------------------------
// Defect B -- the write path must use the same coordinates as the read path
// ---------------------------------------------------------------------------

test('assigning against a stale stamp lands in the slot the caller named', () => {
  reset()
  // Saved under OLD_SLOTS, read against SLOTS: the two disagree from index 2 on.
  saveTeamConfig({ slotAssignments: OLD_ROSTER }, 'league-1', OLD_SLOTS)

  const before = loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments
  const flexIndex = 5
  assert.equal(SLOTS[flexIndex], 'FLEX')
  const teIndex = SLOTS.indexOf('TE')
  const teBefore = before[teIndex]

  assignPlayerToSlot(SLOTS, flexIndex, 'rb3', 'league-1', POOL)
  const after = loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments

  // Pre-fix: raw index 5 was OLD_SLOTS' TE slot, so the new player surfaced
  // under TE and the tight end already there was overwritten in silence.
  assert.equal(after[flexIndex], 'rb3', 'player must land in the slot that was clicked')
  assert.equal(after[teIndex], teBefore, 'an unrelated slot must not change')
})

test('a mutation re-stamps the record with the current slots', () => {
  reset()
  saveTeamConfig({ slotAssignments: OLD_ROSTER }, 'league-1', OLD_SLOTS)

  assignPlayerToSlot(SLOTS, 0, 'qb2', 'league-1', POOL)
  const stored = JSON.parse(store.get(KEY))
  // Pre-fix the obsolete stamp was carried forward on every save, so the
  // record never healed and the drift was permanent.
  assert.deepEqual(stored.slots, SLOTS)
  assert.equal(stored.slotAssignments.length, SLOTS.length)
})

test('mutations round-trip: what a mutator returns is what reloads', () => {
  reset()
  saveTeamConfig({ slotAssignments: OLD_ROSTER }, 'league-1', OLD_SLOTS)

  const returned = assignPlayerToSlot(SLOTS, 4, 'wr4', 'league-1', POOL).slotAssignments
  assert.deepEqual(loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments, returned)

  const cleared = clearSlot(SLOTS, 4, 'league-1').slotAssignments
  assert.deepEqual(loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments, cleared)
})

test('no rostered player is lost to a shape change plus an edit', () => {
  reset()
  saveTeamConfig({ slotAssignments: OLD_ROSTER }, 'league-1', OLD_SLOTS)

  const seated = loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments.filter(Boolean)
  // OLD_SLOTS holds 13 players and SLOTS has 13 slots, but the shapes differ:
  // the remap seats what it can and drops only what has nowhere to go.
  clearSlot(SLOTS, SLOTS.length - 1, 'league-1')
  const after = loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments.filter(Boolean)
  assert.deepEqual(new Set(after), new Set(seated.filter((p) => p !== seated[seated.length - 1])))
})

// ---------------------------------------------------------------------------
// The invariant itself
// ---------------------------------------------------------------------------

test('sanitizeAssignments evicts an illegal occupant to the bench', () => {
  const illegal = ['qb1', 'rb1', 'wr1', 'te1', 'wr2', null, 'wr3', 'k1', null, null, null, null, null]
  //                                                          ^ WR in DST
  const healed = sanitizeAssignments(SLOTS, illegal, POOL)
  assert.equal(healed[SLOTS.indexOf('DST')], null)
  assert.ok(healed.includes('wr3'), 'the evicted player is benched, not deleted')
  assertAllSlotsLegal(SLOTS, healed, 'sanitized')
})

test('sanitizeAssignments leaves a player missing from the pool alone', () => {
  const withUnknown = Array(SLOTS.length).fill(null)
  withUnknown[SLOTS.indexOf('DST')] = 'not-in-pool'
  const healed = sanitizeAssignments(SLOTS, withUnknown, POOL)
  // An unknown position is unknown, not wrong -- dropping him would turn a
  // lookup miss into data loss, and the view already renders this state.
  assert.equal(healed[SLOTS.indexOf('DST')], 'not-in-pool')
})

test('sanitizeAssignments is a no-op without a pool', () => {
  const illegal = Array(SLOTS.length).fill(null)
  illegal[SLOTS.indexOf('DST')] = 'wr1'
  assert.deepEqual(sanitizeAssignments(SLOTS, illegal, null), illegal)
  assert.deepEqual(sanitizeAssignments(SLOTS, illegal, new Map()), illegal)
})

test('assignPlayerToSlot refuses a position the slot cannot hold', () => {
  reset()
  saveTeamConfig({ slotAssignments: Array(SLOTS.length).fill(null) }, 'league-1', SLOTS)
  const dstIndex = SLOTS.indexOf('DST')

  const { slotAssignments } = assignPlayerToSlot(SLOTS, dstIndex, 'wr1', 'league-1', POOL)
  assert.equal(slotAssignments[dstIndex], null, 'a WR must not reach the DST slot')

  assignPlayerToSlot(SLOTS, dstIndex, 'dst1', 'league-1', POOL)
  assert.equal(loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments[dstIndex], 'dst1')
})

test('assignPlayerToSlot ignores an out-of-range index instead of growing the array', () => {
  reset()
  saveTeamConfig({ slotAssignments: Array(SLOTS.length).fill(null) }, 'league-1', SLOTS)

  assignPlayerToSlot(SLOTS, SLOTS.length + 3, 'qb1', 'league-1', POOL)
  const stored = JSON.parse(store.get(KEY))
  // Growing past the slot array is how the stored and displayed coordinate
  // systems drifted apart in the first place.
  assert.equal(stored.slotAssignments.length, SLOTS.length)
  assert.ok(!stored.slotAssignments.includes('qb1'))
})

test('swapSlots refuses a swap canSwapSlots would reject', () => {
  reset()
  const start = Array(SLOTS.length).fill(null)
  start[SLOTS.indexOf('DST')] = 'dst1'
  start[SLOTS.indexOf('WR')] = 'wr1'
  saveTeamConfig({ slotAssignments: start }, 'league-1', SLOTS)

  const { slotAssignments } = swapSlots(SLOTS, SLOTS.indexOf('DST'), SLOTS.indexOf('WR'), 'league-1', POOL)
  assert.equal(slotAssignments[SLOTS.indexOf('DST')], 'dst1')
  assert.equal(slotAssignments[SLOTS.indexOf('WR')], 'wr1')
})

test('swapSlots performs a legal swap', () => {
  reset()
  const start = Array(SLOTS.length).fill(null)
  start[SLOTS.indexOf('WR')] = 'wr1'
  start[4] = 'wr2' // FLEX
  saveTeamConfig({ slotAssignments: start }, 'league-1', SLOTS)

  swapSlots(SLOTS, SLOTS.indexOf('WR'), 4, 'league-1', POOL)
  const after = loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments
  assert.equal(after[SLOTS.indexOf('WR')], 'wr2')
  assert.equal(after[4], 'wr1')
})

// ---------------------------------------------------------------------------
// Existing behaviour that must survive the change
// ---------------------------------------------------------------------------

test('a stamped record of the current shape is read index for index', () => {
  reset()
  const roster = ['qb1', 'rb1', 'wr1', 'te1', 'wr2', 'te2', 'dst1', 'k1', 'rb2', 'wr3', 'wr4', 'qb2', null]
  saveTeamConfig({ slotAssignments: roster }, 'league-1', SLOTS)
  assert.deepEqual(loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments, roster)
})

test('a stamped record of a different shape is re-seated by slot name', () => {
  reset()
  saveTeamConfig({ slotAssignments: OLD_ROSTER }, 'league-1', OLD_SLOTS)
  const { slotAssignments } = loadTeamConfig(SLOTS, 'league-1', POOL)

  assert.equal(slotAssignments[0], 'qb1')
  assert.equal(slotAssignments[SLOTS.indexOf('DST')], 'dst1')
  assert.equal(slotAssignments[SLOTS.indexOf('K')], 'k1')
  assertAllSlotsLegal(SLOTS, slotAssignments, 're-seated by name')
})

test('nothing saved yet reads as an empty roster of the right length', () => {
  reset()
  assert.equal(hasSavedTeamConfig('league-1'), false)
  assert.deepEqual(loadTeamConfig(SLOTS, 'league-1').slotAssignments, Array(SLOTS.length).fill(null))
})

test('each league keeps its own record', () => {
  reset()
  const l2Slots = ['QB', 'RB', 'RB', 'WR', 'WR', 'TE', 'FLEX', 'FLEX', 'DST', 'K']
  saveTeamConfig({ slotAssignments: Array(SLOTS.length).fill(null).fill('qb1', 0, 1) }, 'league-1', SLOTS)
  saveTeamConfig({ slotAssignments: Array(l2Slots.length).fill(null).fill('qb2', 0, 1) }, 'league-2', l2Slots)

  assert.equal(loadTeamConfig(SLOTS, 'league-1').slotAssignments[0], 'qb1')
  assert.equal(loadTeamConfig(l2Slots, 'league-2').slotAssignments[0], 'qb2')
})

// ---------------------------------------------------------------------------
// Defect C -- a seed must not be stamped with another league's slot array
// ---------------------------------------------------------------------------
//
// `leagueId` flips the instant the user picks from the league dropdown, while
// the new roster-slots.json is still in flight, so useTeamConfig's effect runs
// once with the NEW league and the OLD league's slots. It seeded there, which
// stamped league-2's 17-entry roster with league-1's 13 slots. The guard lives
// in the hook (a length disagreement means the two do not belong together);
// these cover the storage-level invariants it depends on.

test('saveTeamConfig writes nothing rather than an unstampable record', () => {
  reset()
  saveTeamConfig({ slotAssignments: OLD_ROSTER }, 'league-1', null)
  // An unstamped record is discarded on read, so writing one is the same as
  // deleting the roster -- and worse, it looks saved.
  assert.equal(store.get(KEY), undefined)
  assert.equal(hasSavedTeamConfig('league-1'), false)
})

test('a cross-league stamp cannot put a player in an illegal slot', () => {
  reset()
  // league-2's roster, stamped with league-1's slots: what the unguarded seed
  // used to write. Pre-fix this surfaced a WR under DST and a RB under K.
  const L2_SLOTS = ['QB','RB','RB','WR','WR','TE','FLEX','FLEX','DST','K','BENCH','BENCH','BENCH','BENCH','BENCH','IR','IR']
  const L2_ROSTER = ['qb1','rb1','rb2','wr1','wr2','te1','wr3','rb3','dst1','k1','qb2','wr4','te2',null,null,null,null]
  saveTeamConfig({ slotAssignments: L2_ROSTER }, 'league-2', SLOTS) // wrong stamp, on purpose

  const { slotAssignments } = loadTeamConfig(L2_SLOTS, 'league-2', POOL)
  assertAllSlotsLegal(L2_SLOTS, slotAssignments, 'cross-league stamp')
})

test('a mutation persists the healed roster, not the corrupt one it read', () => {
  reset()
  // Correctly stamped and current-shaped, but illegal: a WR parked in DST.
  // This is what a browser that ran the buggy code and then saved looks like.
  const corrupt = Array(SLOTS.length).fill(null)
  corrupt[0] = 'qb1'
  corrupt[SLOTS.indexOf('DST')] = 'wr1'
  saveTeamConfig({ slotAssignments: corrupt }, 'league-1', SLOTS)

  // What the view renders -- wr1 evicted to the first free bench slot.
  const shown = loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments
  const benchIndex = shown.indexOf('wr1')
  assert.equal(SLOTS[benchIndex], 'BENCH')

  // Clearing the slot the user can SEE him in must clear him. Reading without
  // the pool here would mutate the unhealed array instead, clearing whatever
  // sits at that index there and writing the WR back into DST.
  clearSlot(SLOTS, benchIndex, 'league-1', POOL)
  const after = loadTeamConfig(SLOTS, 'league-1', POOL).slotAssignments
  assert.ok(!after.includes('wr1'), 'the player the user removed is gone')
  assert.equal(after[0], 'qb1', 'and nobody else moved')

  const stored = JSON.parse(store.get(KEY))
  assert.equal(stored.slotAssignments[SLOTS.indexOf('DST')], null, 'corruption not written back')
})
