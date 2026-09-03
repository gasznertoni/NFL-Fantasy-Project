import { useEffect, useState } from 'react'
import {
  getWeeklyReport,
  getRosterSlots,
  getPlayerPool,
  getManifest,
  LATEST_AVAILABLE_WEEK,
  MIN_SELECTABLE_WEEK,
  MAX_SELECTABLE_WEEK,
} from '../lib/api.js'
import { useTeamConfig, isPlayerRostered, isPositionEligible } from '../lib/teamConfig.js'
import WeekSelector from './WeekSelector.jsx'
import PlayerCard from './PlayerCard.jsx'
import PlayerPickerRow from './PlayerPickerRow.jsx'
import LoadingSkeleton from './LoadingSkeleton.jsx'
import EmptyState from './EmptyState.jsx'

const FORMAT_LABEL = {
  half_ppr: 'half-PPR',
  ppr: 'PPR',
  standard: 'standard',
}

// Slot value used for the de-emphasized "sit" / bench group.
const BENCH_SLOT = 'BENCH'

/**
 * Derives the ordered list of *starting* slot names from the generic slot
 * config, deduplicated so e.g. two "RB" entries become one "RB" group
 * heading. Excludes the bench slot. This is the only place slot grouping
 * is computed -- no slot names/counts are hardcoded inline anywhere else
 * in this view.
 */
function startingSlotOrder(slots) {
  const seen = new Set()
  const order = []
  for (const slot of slots) {
    if (slot === BENCH_SLOT) continue
    if (!seen.has(slot)) {
      seen.add(slot)
      order.push(slot)
    }
  }
  return order
}

export default function WeeklyReportView({ leagueId = 'league-1' }) {
  const [week, setWeek] = useState(LATEST_AVAILABLE_WEEK)
  const [maxWeek, setMaxWeek] = useState(MAX_SELECTABLE_WEEK)
  const [report, setReport] = useState(undefined) // undefined = loading, null = no data
  const [slots, setSlots] = useState(null) // null = loading
  const [pool, setPool] = useState(null) // null = loading, Map<playerId, poolEntry> once loaded
  const [expandedRows, setExpandedRows] = useState(new Set())

  function toggleRow(id) {
    setExpandedRows((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // On mount (and whenever leagueId changes), load the manifest to find the
  // real latest week so the UI doesn't need a hardcoded constant updated by hand.
  useEffect(() => {
    let cancelled = false
    getManifest(leagueId).then((manifest) => {
      if (cancelled || !manifest) return
      const latest = manifest.latestWeek
      if (latest && latest > 0) {
        setWeek(latest)
        setMaxWeek(latest + 1) // +1 so "no data" empty state is reachable
      }
    })
    return () => { cancelled = true }
  }, [leagueId])

  useEffect(() => {
    let cancelled = false
    setReport(undefined)
    Promise.all([getWeeklyReport(week, leagueId), getRosterSlots(leagueId), getPlayerPool(leagueId)]).then(
      ([reportData, slotData, poolData]) => {
        if (cancelled) return
        setReport(reportData)
        setSlots(slotData.slots || [])
        const map = new Map()
        for (const p of poolData.players || []) map.set(p.playerId, p)
        setPool(map)
      },
    )
    return () => {
      cancelled = true
    }
  }, [week, leagueId])

  const { config } = useTeamConfig(slots ? slots.length : 0, leagueId)

  const metaReady = slots !== null && pool !== null && config !== undefined
  const slotOrder = metaReady ? startingSlotOrder(slots) : []
  const benchCount = metaReady ? slots.filter((s) => s === BENCH_SLOT).length : 0

  const projectionsById = new Map((report?.projections || []).map((p) => [p.playerId, p]))

  /**
   * Renders one card for a given roster-slot index (spec section 4, the
   * core join): empty-slot placeholder, normal PlayerCard (projection
   * found), or a degraded card (player assigned but no projection entry
   * for this week -- falls back to the player-pool entry). Never renders
   * nothing and never throws, regardless of which of the three cases applies.
   */
  function renderSlotCard(index, slotName, { muted = false } = {}) {
    const playerId = config.slotAssignments[index]

    if (!playerId) {
      return (
        <article className="player-card player-card-empty-slot" key={`empty-${index}`}>
          <span className="slot-empty-slot-label">{slotName}</span>
          <p className="slot-empty-message">Empty &mdash; assign a player.</p>
          <a className="slot-empty-link" href="#my-team">
            Assign in My Team &rarr;
          </a>
        </article>
      )
    }

    const projection = projectionsById.get(playerId)
    if (projection) {
      return (
        <PlayerCard
          key={playerId}
          player={projection}
          muted={muted}
          slotLabel={slotName}
          expanded={expandedRows.has(playerId)}
          onToggle={() => toggleRow(playerId)}
        />
      )
    }

    // Assigned player has no projection entry for this week -- degrade to
    // the player-pool fallback identity/status rather than dropping the
    // card or crashing.
    const poolEntry = pool.get(playerId) || {
      name: `Player ${playerId}`,
      position: '',
      team: '',
      newsFlag: { designation: 'Healthy', riskLevel: 'none', summary: null },
    }
    return (
      <article className={`player-card player-card-degraded ${muted ? 'player-card-muted' : ''}`} key={`degraded-${playerId}`}>
        <PlayerPickerRow player={poolEntry} />
        <p className="player-card-degraded-note">No projection available for this player this week.</p>
      </article>
    )
  }

  const waiverTargets = report
    ? [...(report.waiverTargets || [])]
        .filter((p) => !metaReady || !isPlayerRostered(p.playerId, config))
        .sort((a, b) => b.projection.points - a.projection.points)
    : []

  /**
   * Read-only "who this would replace" suggestion for a waiver target: an
   * open roster slot eligible for the target's position, if one exists,
   * otherwise the eligible, currently-rostered player with the lowest
   * projection this week. Purely informational -- picking a target doesn't
   * change My Team, the user still makes the swap themselves there. Not an
   * optimizer: it only compares this week's already-computed projections,
   * it doesn't attempt to model anything beyond that.
   *
   * Only ever called from within the `metaReady` branch below, where
   * `slots`/`config`/`pool` are guaranteed loaded -- this guard exists so a
   * future call site added before that gate can't crash instead of just
   * seeing no suggestion.
   */
  function suggestReplacement(position) {
    if (!slots || !config || !pool) return null

    let emptySlot = null
    let worst = null

    for (let i = 0; i < slots.length; i++) {
      const slotName = slots[i]
      if (!isPositionEligible(slotName, position)) continue

      const playerId = config.slotAssignments[i]
      if (!playerId) {
        if (!emptySlot) emptySlot = slotName
        continue
      }

      const proj = projectionsById.get(playerId)
      const points = proj ? proj.projection.points : null
      const name = proj ? proj.name : pool.get(playerId)?.name || playerId
      if (!worst || (points ?? -Infinity) < (worst.points ?? -Infinity)) {
        worst = { slotName, name, points }
      }
    }

    if (emptySlot) return `Fills your open ${emptySlot} slot.`
    if (worst) {
      return worst.points === null
        ? `Would replace ${worst.name} (${worst.slotName}).`
        : `Would replace ${worst.name} (${worst.slotName}, ${worst.points.toFixed(1)} pts this week).`
    }
    return null
  }

  return (
    <section aria-label="Weekly report">
      <div className="view-header">
        <h1>Weekly Report</h1>
        <WeekSelector
          week={week}
          minWeek={MIN_SELECTABLE_WEEK}
          maxWeek={maxWeek}
          onChange={setWeek}
        />
      </div>

      {report === undefined && <LoadingSkeleton rows={6} label="Loading weekly report..." />}

      {report === null && <EmptyState message={`No data for week ${week} yet.`} />}

      {report && !metaReady && <LoadingSkeleton rows={6} label="Loading weekly report..." />}

      {report && metaReady && (
        <>
          <div className="report-section">
            <h2>Start</h2>
            {slotOrder.map((slotName) => {
              const indices = slots
                .map((s, i) => (s === slotName ? i : -1))
                .filter((i) => i !== -1)
              return (
                <div className="slot-group" key={slotName}>
                  <h3 className="slot-group-heading">{slotName}</h3>
                  <div className="player-card-grid">
                    {indices.map((i) => renderSlotCard(i, slotName))}
                  </div>
                </div>
              )
            })}
          </div>

          <div className="report-section">
            <h2>
              Sit {benchCount > 0 && <span className="section-subcount">({benchCount} bench slots)</span>}
            </h2>
            <div className="player-card-grid">
              {slots
                .map((s, i) => (s === BENCH_SLOT ? i : -1))
                .filter((i) => i !== -1)
                .map((i) => renderSlotCard(i, BENCH_SLOT, { muted: true }))}
            </div>
          </div>

          <div className="report-section">
            <h2>Waiver Targets</h2>
            {waiverTargets.length === 0 ? (
              <EmptyState message="No waiver targets available — this week's top candidates are already on your team." />
            ) : (
              <div className="player-list-group">
                {waiverTargets.map((p) => (
                  <PlayerCard
                    key={p.playerId}
                    player={p}
                    slotLabel={p.position}
                    expanded={expandedRows.has(p.playerId)}
                    onToggle={() => toggleRow(p.playerId)}
                    rationale={p.rationale}
                    replacement={suggestReplacement(p.position)}
                  />
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </section>
  )
}
