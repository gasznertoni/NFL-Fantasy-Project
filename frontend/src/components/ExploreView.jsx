import { useState, useEffect } from 'react'
import { getWeeklyReport, getManifest, LATEST_AVAILABLE_WEEK } from '../lib/api.js'
import PlayerCard from './PlayerCard.jsx'
import LoadingSkeleton from './LoadingSkeleton.jsx'
import EmptyState from './EmptyState.jsx'

const POSITIONS = ['All', 'QB', 'RB', 'WR', 'TE', 'DST', 'K']

export default function ExploreView({ leagueId = 'league-1' }) {
  const [week, setWeek] = useState(LATEST_AVAILABLE_WEEK)
  const [report, setReport] = useState(undefined)
  const [posFilter, setPosFilter] = useState('All')
  const [expandedRows, setExpandedRows] = useState(new Set())

  useEffect(() => {
    let cancelled = false
    getManifest(leagueId).then((manifest) => {
      if (cancelled || !manifest) return
      const latest = manifest.latestWeek
      if (latest && latest > 0) setWeek(latest)
    })
    return () => { cancelled = true }
  }, [leagueId])

  useEffect(() => {
    let cancelled = false
    setReport(undefined)
    setExpandedRows(new Set())
    getWeeklyReport(week, leagueId).then((data) => {
      if (!cancelled) setReport(data)
    })
    return () => { cancelled = true }
  }, [week, leagueId])

  function toggleRow(id) {
    setExpandedRows((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const targets = report
    ? [...(report.waiverTargets || [])]
        .filter((p) => posFilter === 'All' || p.position === posFilter)
        .sort((a, b) => b.projection.points - a.projection.points)
    : []

  return (
    <section aria-label="Explore">
      <div className="view-header">
        <div>
          <h1>Explore</h1>
          <p className="view-subtitle">Trending pickups and under-the-radar players from this week&rsquo;s projections.</p>
        </div>
      </div>

      <div className="chip-row">
        {POSITIONS.map((pos) => (
          <button
            key={pos}
            className={`chip ${posFilter === pos ? 'chip-active' : ''}`}
            onClick={() => setPosFilter(pos)}
          >
            {pos}
          </button>
        ))}
      </div>

      {report === undefined && <LoadingSkeleton rows={4} label="Loading players..." />}
      {report === null && <EmptyState message="No data available for this week." />}
      {report && targets.length === 0 && (
        <EmptyState
          message={
            posFilter !== 'All'
              ? `No ${posFilter} waiver targets this week.`
              : "No waiver targets match — this week’s top candidates are already on your team."
          }
        />
      )}
      {report && targets.length > 0 && (
        <div className="player-list-group">
          {targets.map((p) => (
            <PlayerCard
              key={p.playerId}
              player={p}
              slotLabel={p.position}
              expanded={expandedRows.has(p.playerId)}
              onToggle={() => toggleRow(p.playerId)}
              rationale={p.rationale}
            />
          ))}
        </div>
      )}
    </section>
  )
}
