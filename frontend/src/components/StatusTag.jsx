const LEVEL_LABEL = {
  low: 'Low risk',
  medium: 'Medium risk',
  high: 'High risk',
}

/**
 * Renders the universal player status tag shown on every player card
 * everywhere in the app (spec: team-config-and-roster-status.md section
 * 3.1). Mock stand-in for the future LLM summarization/risk-flagging layer
 * (CLAUDE.md Next Steps item 6, not built yet).
 *
 * Replaces the old RiskFlag component's "render nothing when there's no
 * risk" behavior: `newsFlag` is now mandatory on every player object, with
 * `designation: "Healthy"` as the required default, so this always renders
 * something. Healthy renders as a plain, low-emphasis tag with no
 * disclosure (there's no summary text for it). Questionable/Doubtful/Out/IR
 * keep the previous expandable <details> pattern so the summary text stays
 * reachable without cluttering the card by default.
 *
 * `designation` (not `riskLevel`) is the source of truth for whether a
 * player is healthy -- `riskLevel` is only used for the severity label/
 * styling of an already-non-healthy status.
 *
 * `expandable` must be set to false wherever this renders inside another
 * clickable element (e.g. the team-config player picker's option buttons):
 * nesting a <details>/<summary> inside a <button> is invalid HTML, and a
 * click on the summary to read the note would also bubble up and trigger
 * the outer button's own click handler.
 */
export default function StatusTag({ newsFlag, expandable = true }) {
  // Defensive fallback only -- every fixture entry is required to set these,
  // this just keeps a malformed/missing entry from crashing the card.
  const riskLevel = newsFlag?.riskLevel || 'none'
  const designation = newsFlag?.designation || 'Healthy'
  const summary = newsFlag?.summary
  const isHealthy = designation === 'Healthy'

  if (isHealthy) {
    return (
      <span className="status-tag status-tag-healthy">
        <span className="status-tag-dot" aria-hidden="true" />
        {designation}
      </span>
    )
  }

  const label = LEVEL_LABEL[riskLevel] || riskLevel

  if (!expandable) {
    return (
      <span className={`status-tag status-tag-${riskLevel}`}>
        <span className="status-tag-dot" aria-hidden="true" />
        {`${designation} · ${label}`}
      </span>
    )
  }

  return (
    <details className={`status-tag-expandable status-tag-${riskLevel}`}>
      <summary className="status-tag-summary">
        <span className="status-tag-dot" aria-hidden="true" />
        {`${designation} · ${label}`}
      </summary>
      {summary && <p className="status-tag-detail">{summary}</p>}
    </details>
  )
}
