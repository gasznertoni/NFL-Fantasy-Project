import { useMemo, useState } from 'react'

const COLUMNS = [
  { key: 'week', label: 'Week', sortable: true },
  { key: 'player', label: 'Player', sortable: true },
  { key: 'tier', label: 'Tier', sortable: true },
  { key: 'recommendationType', label: 'Type', sortable: true },
  { key: 'predictedPoints', label: 'Predicted', sortable: true },
  { key: 'actualPoints', label: 'Actual', sortable: true },
  { key: 'outcomeCorrect', label: 'Result', sortable: false },
]

const TYPE_LABEL = {
  start: 'Start',
  sit: 'Sit',
  waiver_add: 'Waiver add',
}

function sortValue(row, key) {
  switch (key) {
    case 'player':
      return row.player.name
    case 'tier':
      return row.tier
    default:
      return row[key]
  }
}

/**
 * Sortable, filterable prediction history table (spec section 5.2-5.3).
 * Sortable by clicking any sortable column header (week and tier included,
 * per acceptance criteria); filterable by tier via dropdown. Pending rows
 * (actualPoints === null) render a distinct grey "pending" tag rather than
 * a blank cell or a zero.
 *
 * @param {object[]} history - TrackRecord.history entries.
 * @param {object} tierLabels - map of tier enum -> tierLabel, sourced from
 *   the fixture's own summary block so no label is hardcoded here.
 */
const POSITIONS = ['QB', 'RB', 'WR', 'TE', 'DST', 'K']

export default function HistoryTable({ history, tierLabels }) {
  const [sortKey, setSortKey] = useState('week')
  const [sortDir, setSortDir] = useState('desc')
  const [tierFilter, setTierFilter] = useState('all')
  const [weekFilter, setWeekFilter] = useState('all')
  const [posFilter, setPosFilter] = useState('all')
  const [search, setSearch] = useState('')

  const tierOptions = Object.keys(tierLabels)
  const weekOptions = useMemo(
    () => [...new Set(history.map((row) => row.week))].sort((a, b) => a - b),
    [history],
  )

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase()
    let filtered = history
    if (q) {
      filtered = filtered.filter((row) => row.player.name.toLowerCase().includes(q))
    }
    if (tierFilter !== 'all') {
      filtered = filtered.filter((row) => row.tier === tierFilter)
    }
    if (weekFilter !== 'all') {
      filtered = filtered.filter((row) => row.week === Number(weekFilter))
    }
    if (posFilter !== 'all') {
      filtered = filtered.filter((row) => row.player.position === posFilter)
    }
    const sorted = [...filtered].sort((a, b) => {
      const va = sortValue(a, sortKey)
      const vb = sortValue(b, sortKey)
      if (va === vb) return 0
      // Nulls (pending actualPoints) always sort last regardless of direction.
      if (va === null) return 1
      if (vb === null) return -1
      const cmp = va > vb ? 1 : -1
      return sortDir === 'asc' ? cmp : -cmp
    })
    return sorted
  }, [history, sortKey, sortDir, tierFilter, weekFilter, posFilter, search])

  function toggleSort(key) {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir('asc')
    }
  }

  return (
    <div className="history-table-wrap">
      <div className="history-table-controls">
        <input
          className="history-table-search"
          type="search"
          placeholder="Search player…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search by player name"
        />
        <label>
          Position
          <select value={posFilter} onChange={(e) => setPosFilter(e.target.value)}>
            <option value="all">All</option>
            {POSITIONS.map((pos) => (
              <option key={pos} value={pos}>{pos}</option>
            ))}
          </select>
        </label>
        <label>
          Week
          <select value={weekFilter} onChange={(e) => setWeekFilter(e.target.value)}>
            <option value="all">All</option>
            {weekOptions.map((week) => (
              <option key={week} value={week}>Week {week}</option>
            ))}
          </select>
        </label>
        <label>
          Tier
          <select value={tierFilter} onChange={(e) => setTierFilter(e.target.value)}>
            <option value="all">All</option>
            {tierOptions.map((tier) => (
              <option key={tier} value={tier}>{tierLabels[tier]}</option>
            ))}
          </select>
        </label>
      </div>

      <table className="history-table">
        <thead>
          <tr>
            {COLUMNS.map((col) => (
              <th key={col.key}>
                {col.sortable ? (
                  <button type="button" className="sort-button" onClick={() => toggleSort(col.key)}>
                    {col.label}
                    {sortKey === col.key && <span className="sort-indicator">{sortDir === 'asc' ? ' ↑' : ' ↓'}</span>}
                  </button>
                ) : (
                  col.label
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.predictionId} className={row.actualPoints === null ? 'history-row-pending' : ''}>
              <td>{row.week}</td>
              <td>
                {row.player.name} <span className="history-position">{row.player.position}</span>
              </td>
              <td>
                <span className={`tier-badge tier-badge-small ${row.tier === 'in_house_estimate' ? 'tier-badge-in-house' : 'tier-badge-consensus'}`}>
                  {tierLabels[row.tier] || row.tier}
                </span>
              </td>
              <td>{TYPE_LABEL[row.recommendationType] || row.recommendationType}</td>
              <td>{row.predictedPoints.toFixed(1)}</td>
              <td>{row.actualPoints === null ? <span className="pending-tag">pending</span> : row.actualPoints.toFixed(1)}</td>
              <td>
                {row.outcomeCorrect === null ? (
                  <span className="pending-tag">pending</span>
                ) : row.outcomeCorrect ? (
                  <span className="result-tag result-tag-correct">Correct</span>
                ) : (
                  <span className="result-tag result-tag-incorrect">Incorrect</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
