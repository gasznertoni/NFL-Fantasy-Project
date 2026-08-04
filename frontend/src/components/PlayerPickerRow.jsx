import StatusTag from './StatusTag.jsx'

/**
 * Compact player identity + status row -- name/position/team plus the same
 * StatusTag used everywhere else. Deliberately distinct from PlayerCard,
 * which assumes week-scoped `projection` data that player-pool entries
 * don't have (spec section 8). Shared by:
 *  - My Team's per-slot assigned-player row and the player picker list.
 *  - Weekly Report's degraded card (assigned player, no projection this
 *    week) -- falls back to the pool entry's identity/status.
 *
 * @param {object} player - identity object with at least
 *   name/position/team/newsFlag (a pool entry, or any Player Projection
 *   object -- both shapes carry these fields).
 * @param {boolean} [statusExpandable=true] - pass false when this row is
 *   itself nested inside another clickable element (e.g. a picker option
 *   <button>), so StatusTag doesn't render its own nested <details>.
 */
export default function PlayerPickerRow({ player, statusExpandable = true }) {
  const { name, position, team, newsFlag } = player

  return (
    <div className="player-picker-row">
      <div className="player-picker-identity">
        <span className="player-picker-name">{name}</span>
        <span className="player-picker-meta">
          {position} &middot; {team}
        </span>
      </div>
      <StatusTag newsFlag={newsFlag} expandable={statusExpandable} />
    </div>
  )
}
