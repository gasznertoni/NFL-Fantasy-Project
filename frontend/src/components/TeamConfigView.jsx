import { useEffect, useMemo, useState } from 'react'
import { getRosterSlots, getPlayerPool } from '../lib/api.js'
import { useTeamConfig, eligiblePositions, canSwapSlots } from '../lib/teamConfig.js'
import PlayerPickerRow from './PlayerPickerRow.jsx'
import LoadingSkeleton from './LoadingSkeleton.jsx'

/**
 * "My Team" tab (spec section 6): lets the user manually assign players
 * from the broader player pool to the roster slots defined by
 * roster-slots.json, persisted via lib/teamConfig.js. Not week-scoped --
 * no week selector, no projection numbers, just identity + status + slot
 * assignment (spec section 2).
 */
export default function TeamConfigView({ leagueId = 'league-1' }) {
  const [slots, setSlots] = useState(null) // null = loading
  const [pool, setPool] = useState(null) // null = loading
  const [openIndex, setOpenIndex] = useState(null)
  const [moveIndex, setMoveIndex] = useState(null)
  const [search, setSearch] = useState('')

  useEffect(() => {
    let cancelled = false
    // Back to "loading" FIRST. Without this, a league switch leaves the
    // previous league's slots on screen while the new league's fixtures load,
    // and useTeamConfig seeds the new league's roster against them -- which
    // is how league-2's 17 players got stamped with league-1's 13 slots.
    setSlots(null)
    setPool(null)
    Promise.all([getRosterSlots(leagueId), getPlayerPool(leagueId)]).then(([slotData, poolData]) => {
      if (cancelled) return
      setSlots(slotData.slots || [])
      setPool(poolData.players || [])
    })
    return () => {
      cancelled = true
    }
  }, [leagueId])

  const poolById = useMemo(() => {
    const map = new Map()
    for (const p of pool || []) map.set(p.playerId, p)
    return map
  }, [pool])

  // The pool goes in so the config is healed of position-illegal placements
  // on read and the mutators refuse to create new ones.
  const { config, assign, clear, swap, reset } = useTeamConfig(slots, leagueId, poolById)

  const loading = slots === null || pool === null || config === undefined

  function togglePicker(index) {
    setSearch('')
    setMoveIndex(null)
    setOpenIndex((prev) => (prev === index ? null : index))
  }

  function toggleMove(index) {
    setOpenIndex(null)
    setMoveIndex((prev) => (prev === index ? null : index))
  }

  function handleAssign(index, playerId) {
    assign(index, playerId)
    setOpenIndex(null)
  }

  function handleSwap(from, to) {
    swap(from, to)
    setMoveIndex(null)
  }

  function handleReset() {
    reset()
    setOpenIndex(null)
    setMoveIndex(null)
  }

  /**
   * Slots the player in `index` may legally move to, each paired with
   * whoever currently sits there so the row can say what the trade is.
   * Eligibility is delegated to canSwapSlots, which checks BOTH directions
   * -- rendering a target this view thinks is fine but the model refuses
   * would be a dead button.
   */
  function moveTargets(index) {
    if (!slots || !config) return []
    return slots
      .map((slotName, target) => ({ slotName, target }))
      .filter(({ target }) => target !== index)
      .filter(({ target }) => canSwapSlots(slots, config.slotAssignments, poolById, index, target))
      .map((t) => ({ ...t, occupant: poolById.get(config.slotAssignments[t.target]) || null }))
  }

  return (
    <section aria-label="My team">
      <div className="view-header">
        <h1>My Team</h1>
        <button type="button" className="secondary-button" onClick={handleReset}>
          Reset to default
        </button>
      </div>

      <p className="format-footnote">
        Assign players to your roster slots. This drives Start/Sit grouping in Weekly Report. Changes
        save automatically to this browser only -- there's no server or account sync (v1 scope).
      </p>

      {loading && <LoadingSkeleton rows={12} label="Loading team..." />}

      {!loading && (
        <div className="team-slot-list">
          {slots.map((slotName, index) => {
            const playerId = config.slotAssignments[index]
            const assignedPlayer = playerId ? poolById.get(playerId) : null
            const eligible = eligiblePositions(slotName)

            const candidates = pool
              .filter((p) => eligible === null || eligible.includes(p.position))
              .filter((p) => {
                const idx = config.slotAssignments.indexOf(p.playerId)
                return idx === -1 || idx === index
              })
              .filter((p) => !search || p.name.toLowerCase().includes(search.toLowerCase()))

            return (
              <div className="team-slot-row" key={index}>
                <div className="team-slot-label">{slotName}</div>

                <div className="team-slot-body">
                  {playerId && assignedPlayer && (
                    <>
                      <PlayerPickerRow player={assignedPlayer} />
                      <div className="team-slot-actions">
                        <button type="button" className="link-button" onClick={() => togglePicker(index)}>
                          Change
                        </button>
                        <button type="button" className="link-button" onClick={() => toggleMove(index)}>
                          Move
                        </button>
                        <button type="button" className="link-button" onClick={() => clear(index)}>
                          Remove
                        </button>
                      </div>
                    </>
                  )}

                  {playerId && !assignedPlayer && (
                    <>
                      <span className="team-slot-unknown">Assigned player not found in pool ({playerId})</span>
                      <div className="team-slot-actions">
                        <button type="button" className="link-button" onClick={() => togglePicker(index)}>
                          Change
                        </button>
                        <button type="button" className="link-button" onClick={() => clear(index)}>
                          Remove
                        </button>
                      </div>
                    </>
                  )}

                  {!playerId && (
                    <>
                      <span className="team-slot-empty-label">Empty &mdash; assign a player</span>
                      <div className="team-slot-actions">
                        <button type="button" className="link-button" onClick={() => togglePicker(index)}>
                          Assign
                        </button>
                      </div>
                    </>
                  )}
                </div>

                {moveIndex === index && (
                  <div className="player-picker">
                    <div className="player-picker-list">
                      {moveTargets(index).length === 0 && (
                        <p className="player-picker-empty">
                          No other slot can take {assignedPlayer ? assignedPlayer.name : 'this player'}.
                        </p>
                      )}
                      {moveTargets(index).map(({ slotName: target, target: t, occupant }) => (
                        <button
                          type="button"
                          key={t}
                          className="player-picker-option"
                          onClick={() => handleSwap(index, t)}
                        >
                          <span className="team-slot-label">{target}</span>
                          {occupant ? (
                            <span>
                              Swap with {occupant.name} ({occupant.position})
                            </span>
                          ) : (
                            <span>Move here &mdash; slot is empty</span>
                          )}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {openIndex === index && (
                  <div className="player-picker">
                    <input
                      type="text"
                      className="player-picker-search"
                      placeholder="Search by name..."
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      autoFocus
                    />
                    <div className="player-picker-list">
                      {candidates.length === 0 && <p className="player-picker-empty">No eligible players match.</p>}
                      {candidates.map((p) => (
                        <button
                          type="button"
                          key={p.playerId}
                          className="player-picker-option"
                          onClick={() => handleAssign(index, p.playerId)}
                        >
                          <PlayerPickerRow player={p} statusExpandable={false} />
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
