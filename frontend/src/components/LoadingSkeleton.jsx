/**
 * Generic loading skeleton shown while getWeeklyReport()/getTrackRecord()
 * resolve (spec section 2). `rows` controls how many placeholder blocks to
 * render so each view can size the skeleton to roughly match its content.
 */
export default function LoadingSkeleton({ rows = 4, label = 'Loading...' }) {
  return (
    <div className="skeleton" role="status" aria-live="polite">
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }).map((_, i) => (
        <div className="skeleton-row" key={i} />
      ))}
    </div>
  )
}
