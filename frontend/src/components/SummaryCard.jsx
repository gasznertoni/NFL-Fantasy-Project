/**
 * Headline summary card for one projection tier (spec section 5.1). This is
 * the "10-second" artifact for a recruiter: large numbers, minimal text,
 * tier clearly labeled from the fixture's own tierLabel field.
 */
export default function SummaryCard({ tier, stats, highlight }) {
  const hitRatePct = Math.round(stats.startSitHitRate * 100)

  return (
    <div className={`summary-card ${highlight ? 'summary-card-highlight' : ''}`}>
      <div className="summary-card-header">
        <span className={`tier-badge ${tier === 'in_house_estimate' ? 'tier-badge-in-house' : 'tier-badge-consensus'}`}>
          {stats.tierLabel}
        </span>
        {highlight && <span className="summary-card-note">our own work</span>}
      </div>
      <div className="summary-card-metrics">
        <div className="summary-metric">
          <span className="summary-metric-value">{hitRatePct}%</span>
          <span className="summary-metric-label">start/sit hit rate</span>
        </div>
        <div className="summary-metric">
          <span className="summary-metric-value">{stats.meanAbsoluteError.toFixed(1)}</span>
          <span className="summary-metric-label">mean abs. error (pts)</span>
        </div>
        <div className="summary-metric">
          <span className="summary-metric-value">{stats.predictionsScored}</span>
          <span className="summary-metric-label">predictions scored</span>
        </div>
      </div>
    </div>
  )
}
