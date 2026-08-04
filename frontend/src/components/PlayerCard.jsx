import TierBadge from './TierBadge.jsx'
import StatusTag from './StatusTag.jsx'

/**
 * Renders a single player projection (spec section 3.1) as a card. Used
 * identically across Start, Sit, and Waiver Targets sections -- the tier
 * badge requirement (section 4) applies the same way in all three.
 *
 * @param {object} player - a Player Projection object.
 * @param {boolean} [muted] - visually de-emphasize the card (used for Sit).
 * @param {string} [rationale] - waiver-target-only LLM-summarization stand-in.
 * @param {string} [replacement] - waiver-target-only: which of the user's
 *   current roster spots this player would fill (an open slot) or replace
 *   (the lowest-projected eligible rostered player this week).
 */
export default function PlayerCard({ player, muted = false, rationale, replacement }) {
  const { name, position, team, opponent, projection, newsFlag } = player

  return (
    <article className={`player-card ${muted ? 'player-card-muted' : ''}`}>
      <div className="player-card-main">
        <div className="player-card-identity">
          <span className="player-card-name">{name}</span>
          <span className="player-card-meta">
            {position} &middot; {team}
            {opponent ? ` vs ${opponent}` : ''}
          </span>
        </div>
        <div className="player-card-projection">
          <span className="player-card-points">{projection.points.toFixed(1)}</span>
          <TierBadge projection={projection} />
        </div>
      </div>
      <StatusTag newsFlag={newsFlag} />
      {rationale && <p className="player-card-rationale">{rationale}</p>}
      {replacement && <p className="player-card-replacement">{replacement}</p>}
    </article>
  )
}
