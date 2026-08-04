const LEVEL_LABEL = {
  low: 'Low risk',
  medium: 'Medium risk',
  high: 'High risk',
}

/**
 * Renders an injury/news risk flag for a player card. Mock stand-in for the
 * future LLM summarization/risk-flagging layer (CLAUDE.md Next Steps item
 * 6, not built yet).
 *
 * Renders nothing when there's no relevant news (`riskLevel` missing or
 * "none"). Otherwise renders a colored tag with the designation, and makes
 * the summary text accessible via a native <details> disclosure so it's
 * reachable without JS and without cluttering the card by default.
 */
export default function RiskFlag({ newsFlag }) {
  if (!newsFlag || !newsFlag.riskLevel || newsFlag.riskLevel === 'none') {
    return null
  }

  const { riskLevel, designation, summary } = newsFlag
  const label = LEVEL_LABEL[riskLevel] || riskLevel

  return (
    <details className={`risk-flag risk-flag-${riskLevel}`}>
      <summary className="risk-flag-summary">
        <span className="risk-flag-dot" aria-hidden="true" />
        {designation ? `${designation} · ${label}` : label}
      </summary>
      {summary && <p className="risk-flag-detail">{summary}</p>}
    </details>
  )
}
