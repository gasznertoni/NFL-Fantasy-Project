import { useEffect, useMemo, useState } from 'react'
import { getRosterSlots, getPlayerPool } from '../lib/api.js'
import { useTeamConfig, eligiblePositions } from '../lib/teamConfig.js'
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
  const [search, setSearch] = useState('')

  useEffect(() => {
    let cancelled = false
    Promise.all([getRosterSlots(), getPlayerPool(leagueId)]).then(([slotData, poolData]) => {
      if (cancelled) return
      setSlots(slotData.slots || [])
      setPool(poolData.players || [])
    })
    return () => {
      cancelled = true
    }
  }, [leagueId])

  const { config, assign, clear, reset } = useTeamConfig(slots ? slots.length : 0, leagueId)

  const poolById = useMemo(() => {
    const map = new Map()
    for (const p of pool || []) map.set(p.playerId, p)
    return map
  }, [pool])

  const loading = slots === null || pool === null || config === undefined

  function togglePicker(index) {
    setSearch('')
    setOpenIndex((prev) => (prev === index ? null : index))
  }

  function handleAssign(index, playerId) {
    assign(index, playerId)
    setOpenIndex(null)
  }

  function handleReset() {
    reset()
    setOpenIndex(null)
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
