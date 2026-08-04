/**
 * Renders the visible tier badge that must sit next to every projected
 * points number in the app (spec section 4, hard constraint from CLAUDE.md).
 *
 * The displayed text always comes from `projection.tierLabel` in the
 * fixture -- never inferred or hardcoded per component. `projection.tier`
 * (the enum) is only used to pick a color class, not to derive the text.
 */
export default function TierBadge({ projection }) {
  if (!projection || !projection.tierLabel) {
    return null
  }
  const tierClass = projection.tier === 'in_house_estimate' ? 'tier-badge-in-house' : 'tier-badge-consensus'
  return <span className={`tier-badge ${tierClass}`}>{projection.tierLabel}</span>
}
