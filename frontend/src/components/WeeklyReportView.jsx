import { useEffect, useState } from 'react'
import {
  getWeeklyReport,
  getRosterSlots,
  LATEST_AVAILABLE_WEEK,
  MIN_SELECTABLE_WEEK,
  MAX_SELECTABLE_WEEK,
} from '../lib/api.js'
import WeekSelector from './WeekSelector.jsx'
import PlayerCard from './PlayerCard.jsx'
import LoadingSkeleton from './LoadingSkeleton.jsx'
import EmptyState from './EmptyState.jsx'

const FORMAT_LABEL = {
  half_ppr: 'half-PPR',
  ppr: 'PPR',
  standard: 'standard',
}

// Slot value used for the de-emphasized "sit" / bench group. Read from the
// slot config at render time (see startingSlotOrder below) rather than
// assumed -- this constant only names which value from that config we treat
// as the bench pile.
const BENCH_SLOT = 'BENCH'

/**
 * Derives the ordered list of *starting* slot names from the generic slot
 * config (spec section 3.3), deduplicated so e.g. two "RB" entries become
 * one "RB" group heading. Excludes the bench slot. This is the only place
 * slot grouping is computed -- no slot names/counts are hardcoded inline
 * anywhere else in this view.
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

export default function WeeklyReportView() {
  const [week, setWeek] = useState(LATEST_AVAILABLE_WEEK)
  const [report, setReport] = useState(undefined) // undefined = loading, null = no data
  const [slots, setSlots] = useState([])

  useEffect(() => {
    let cancelled = false
    setReport(undefined)
    Promise.all([getWeeklyReport(week), getRosterSlots()]).then(([reportData, slotData]) => {
      if (cancelled) return
      setReport(reportData)
      setSlots(slotData.slots || [])
    })
    return () => {
      cancelled = true
    }
  }, [week])

  const slotOrder = startingSlotOrder(slots)
  const benchCount = slots.filter((s) => s === BENCH_SLOT).length

  return (
    <section aria-label="Weekly report">
      <div className="view-header">
        <h1>Weekly Report</h1>
        <WeekSelector
          week={week}
          minWeek={MIN_SELECTABLE_WEEK}
          maxWeek={MAX_SELECTABLE_WEEK}
          onChange={setWeek}
        />
      </div>

      {report === undefined && <LoadingSkeleton rows={6} label="Loading weekly report..." />}

      {report === null && <EmptyState message={`No data for week ${week} yet.`} />}

      {report && (
        <>
          <p className="format-footnote">
            Projections shown assume <strong>{FORMAT_LABEL[report.leagueFormatAssumption] || report.leagueFormatAssumption}</strong> scoring, pending
            the real league's confirmed settings.
          </p>

          <div className="report-section">
            <h2>Start</h2>
            {slotOrder.map((slot) => {
              const players = report.startSit.start.filter((p) => p.rosterSlot === slot)
              if (players.length === 0) return null
              return (
                <div className="slot-group" key={slot}>
                  <h3 className="slot-group-heading">{slot}</h3>
                  <div className="player-card-grid">
                    {players.map((p) => (
                      <PlayerCard key={p.playerId} player={p} />
                    ))}
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
              {report.startSit.sit.map((p) => (
                <PlayerCard key={p.playerId} player={p} muted />
              ))}
            </div>
          </div>

          <div className="report-section">
            <h2>Waiver Targets</h2>
            <div className="player-card-grid">
              {[...report.waiverTargets]
                .sort((a, b) => b.projection.points - a.projection.points)
                .map((p) => (
                  <PlayerCard key={p.playerId} player={p} rationale={p.rationale} />
                ))}
            </div>
          </div>
        </>
      )}
    </section>
  )
}
