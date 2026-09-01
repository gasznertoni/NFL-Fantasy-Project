import TierBadge from './TierBadge.jsx'
import StatusTag from './StatusTag.jsx'

/**
 * Renders a single player projection as a card (desktop) or expandable list
 * row (mobile). The layout switch is handled by CSS — this component always
 * renders the same HTML; the breakpoint at 900 px hides/shows the
 * `.player-card-expandable` section and shifts the main row to a compact
 * horizontal layout.
 *
 * @param {object}   player      - Player Projection object.
 * @param {boolean}  [muted]     - Visually de-emphasize (bench/sit section).
 * @param {string}   [rationale] - Waiver-target pickup rationale text.
 * @param {string}   [replacement] - Which roster slot this player would fill.
 * @param {string}   [slotLabel] - Roster slot name shown as a colored badge.
 * @param {boolean}  [expanded]  - Whether the expandable detail is open.
 * @param {function} [onToggle]  - Toggle handler; makes the card clickable.
 */
export default function PlayerCard({
  player,
  muted = false,
  rationale,
  replacement,
  slotLabel,
  expanded = false,
  onToggle,
}) {
  const { name, position, team, opponent, projection, newsFlag } = player
  const isHealthy = newsFlag?.designation === 'Healthy'

  // `points` is an EXPECTED value once availability is modelled: P(play) x the
  // if-he-plays projection. Surfacing the two factors matters because they are
  // different reasons to sit someone -- "he is a 4-point player" and "he is a
  // 14-point player who probably won't suit up" look identical otherwise.
  const hasRange =
    typeof projection.floor === 'number' && typeof projection.ceiling === 'number'
  const playProbability = projection.playProbability
  const isDoubtful = typeof playProbability === 'number' && playProbability < 0.85

  // Slot badge color is driven by the player's position, not the slot name —
  // so a RB in the FLEX slot still gets a green badge.
  const posClass = position?.toLowerCase() || 'bench'

  const clickProps = onToggle
    ? { onClick: onToggle, role: 'button', tabIndex: 0, onKeyDown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle() } } }
    : {}

  return (
    <article
      className={`player-card ${muted ? 'player-card-muted' : ''} ${expanded ? 'is-expanded' : ''} ${onToggle ? 'player-card-clickable' : ''}`}
      {...clickProps}
    >
      <div className="player-card-main">
        {slotLabel && (
          <span className={`slot-tag slot-tag-${posClass}`} aria-label={`Slot: ${slotLabel}`}>
            {slotLabel}
          </span>
        )}
        <div className="player-card-identity">
          <span className="player-card-name">{name}</span>
          <span className="player-card-meta">
            {position} &middot; {team}
            {opponent ? ` vs ${opponent}` : ''}
          </span>
          {/* Compact risk tag visible in list rows on mobile when not expanded */}
          {!isHealthy && (
            <span className="player-card-risk-inline">
              <StatusTag newsFlag={newsFlag} expandable={false} />
            </span>
          )}
        </div>
        <div className="player-card-projection">
          <span className="player-card-points">{projection.points.toFixed(1)}</span>
          {/* Range comes from empirical residual quantiles (10th-90th), not a
              normal interval -- weekly outcomes are strongly right-skewed, so a
              symmetric band would be wrong on both ends. Absent for tiers or
              runs without a fitted interval model, in which case the point
              estimate stands alone exactly as before. */}
          {hasRange && (
            <span className="player-card-range" title="Likely range (10th-90th percentile)">
              {projection.floor.toFixed(1)}&ndash;{projection.ceiling.toFixed(1)}
            </span>
          )}
          <TierBadge projection={projection} />
        </div>
      </div>

      {/* Hidden on mobile until expanded; always visible on desktop */}
      <div className="player-card-expandable">
        {isDoubtful && (
          <p className="player-card-availability">
            <span className="player-card-availability-pct">
              {Math.round(playProbability * 100)}% likely to play
            </span>
            {typeof projection.conditionalPoints === 'number' && (
              <span className="player-card-availability-detail">
                {' '}&middot; {projection.conditionalPoints.toFixed(1)} if he does
              </span>
            )}
          </p>
        )}
        <StatusTag newsFlag={newsFlag} expandable={!onToggle} />
        {rationale && <p className="player-card-rationale">{rationale}</p>}
        {replacement && <p className="player-card-replacement">{replacement}</p>}
      </div>
    </article>
  )
}
