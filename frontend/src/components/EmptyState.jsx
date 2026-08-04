/**
 * Plain "no data" state (spec section 2) shown when a fixture has no entry
 * for the requested week/season -- e.g. selecting a week that hasn't been
 * generated yet.
 */
export default function EmptyState({ message = 'No data for this week.' }) {
  return (
    <div className="empty-state">
      <p>{message}</p>
    </div>
  )
}
