import { useEffect, useState } from 'react'
import { getTrackRecord } from '../lib/api.js'
import SummaryCard from './SummaryCard.jsx'
import HistoryTable from './HistoryTable.jsx'
import LoadingSkeleton from './LoadingSkeleton.jsx'
import EmptyState from './EmptyState.jsx'

// Display order for the headline summary cards. in_house_estimate is shown
// first: CLAUDE.md is explicit that it's "the one actually worth measuring
// closely," since consensus numbers are FantasyPros' work, not this
// project's own. Any tier present in the fixture but not listed here still
// renders, just after these.
const TIER_DISPLAY_PRIORITY = ['in_house_estimate', 'consensus']

function orderedTiers(summary) {
  const known = TIER_DISPLAY_PRIORITY.filter((t) => t in summary)
  const rest = Object.keys(summary).filter((t) => !TIER_DISPLAY_PRIORITY.includes(t))
  return [...known, ...rest]
}

export default function TrackRecordView() {
  const [track, setTrack] = useState(undefined) // undefined = loading, null = no data

  useEffect(() => {
    let cancelled = false
    getTrackRecord().then((data) => {
      if (!cancelled) setTrack(data)
    })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <section aria-label="Track record">
      <div className="view-header">
        <h1>Track Record</h1>
      </div>

      {track === undefined && <LoadingSkeleton rows={5} label="Loading track record..." />}

      {track === null && <EmptyState message="No track record data available yet." />}

      {track && (
        <>
          <p className="format-footnote">
            Season {track.season}, through week {track.asOfWeek}. Accuracy is tracked separately per projection tier so
            the in-house model's real performance is never blended with FantasyPros' consensus numbers.
          </p>

          <div className="summary-cards">
            {orderedTiers(track.summary).map((tier) => (
              <SummaryCard
                key={tier}
                tier={tier}
                stats={track.summary[tier]}
                highlight={tier === 'in_house_estimate'}
              />
            ))}
          </div>

          <div className="report-section">
            <h2>Prediction History</h2>
            <HistoryTable
              history={track.history}
              tierLabels={Object.fromEntries(Object.keys(track.summary).map((t) => [t, track.summary[t].tierLabel]))}
            />
          </div>
        </>
      )}
    </section>
  )
}
