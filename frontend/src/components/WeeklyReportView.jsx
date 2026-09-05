import { useEffect, useState } from 'react'
import {
  getWeeklyReport,
  getRosterSlots,
  getWeeklyReports,
  getPlayerPool,
  getManifest,
  LATEST_AVAILABLE_WEEK,
  MIN_SELECTABLE_WEEK,
  MAX_SELECTABLE_WEEK,
} from '../lib/api.js'
import { useTeamConfig, isPlayerRostered, isPositionEligible } from '../lib/teamConfig.js'
import WeekSelector from './WeekSelector.jsx'
import PlayerCard from './PlayerCard.jsx'
import PlayerPickerRow from './PlayerPickerRow.jsx'
import LoadingSkeleton from './LoadingSkeleton.jsx'
import { lineupProjection, betterLineup, waiverReplacement, replacementLevels } from '../lib/lineup.js'
import EmptyState from './EmptyState.jsx'

const FORMAT_LABEL = {
  half_ppr: 'half-PPR',
  ppr: 'PPR',
  standard: 'standard',
}

// Slot value used for the de-emphasized "sit" / bench group.
const BENCH_SLOT = 'BENCH'
// Injured reserve. Deliberately NOT folded into the bench group: a bench
// player is startable and simply is not started this week, while an IR
// player cannot be started at all. Showing them together would put a
// start/sit decision in front of the user that does not exist.
const IR_SLOT = 'IR'

// How many weeks the waiver advice judges over, counting the week on screen.
// Four is a compromise: long enough that a single hard matchup or a bye does
// not by itself make a starter look droppable, short enough that the later
// weeks -- which are baseline-plus-context until real games are played -- do
// not drown out the week actually being decided.
const WAIVER_HORIZON_WEEKS = 4

/**
 * Derives the ordered list of *starting* slot names from the generic slot
 * config, deduplicated so e.g. two "RB" entries become one "RB" group
 * heading. Excludes the bench and IR slots. This is the only place slot grouping
 * is computed -- no slot names/counts are hardcoded inline anywhere else
 * in this view.
 */
function startingSlotOrder(slots) {
  const seen = new Set()
  const order = []
  for (const slot of slots) {
    if (slot === BENCH_SLOT || slot === IR_SLOT) continue
    if (!seen.has(slot)) {
      seen.add(slot)
      order.push(slot)
    }
  }
  return order
}

export default function WeeklyReportView({ leagueId = 'league-1' }) {
  const [week, setWeek] = useState(LATEST_AVAILABLE_WEEK)
  const [maxWeek, setMaxWeek] = useState(MAX_SELECTABLE_WEEK)
  const [report, setReport] = useState(undefined) // undefined = loading, null = no data
  const [slots, setSlots] = useState(null) // null = loading
  const [pool, setPool] = useState(null) // null = loading, Map<playerId, poolEntry> once loaded
  const [expandedRows, setExpandedRows] = useState(new Set())
  // playerId -> expected points for each horizon week, index 0 = `week`.
  const [pointsByWeek, setPointsByWeek] = useState(null)
  const [horizonWeeks, setHorizonWeeks] = useState(1)
  const [teamCount, setTeamCount] = useState(null)

  function toggleRow(id) {
    setExpandedRows((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // On mount (and whenever leagueId changes), load the manifest to find the
  // real latest week so the UI doesn't need a hardcoded constant updated by hand.
  useEffect(() => {
    let cancelled = false
    getManifest(leagueId).then((manifest) => {
      if (cancelled || !manifest) return
      const latest = manifest.latestWeek
      // Open on the week the SEASON is on, not the newest fixture on disk.
      // Regenerating the whole season makes latestWeek 18 the day before
      // week 1, which is how this view came to default to week 18 all
      // preseason. Fall back to latestWeek when currentWeek is absent --
      // generate_report omits it rather than guessing if the schedule cannot
      // be read, and older manifests predate the field.
      if (manifest.teamCount > 0) setTeamCount(manifest.teamCount)
      const current = manifest.currentWeek
      if (current && current > 0) setWeek(current)
      else if (latest && latest > 0) setWeek(latest)
      if (latest && latest > 0) {
        setMaxWeek(latest + 1) // +1 so "no data" empty state is reachable
      }
    })
    return () => { cancelled = true }
  }, [leagueId])

  useEffect(() => {
    let cancelled = false
    setReport(undefined)
    Promise.all([getWeeklyReport(week, leagueId), getRosterSlots(leagueId), getPlayerPool(leagueId)]).then(
      ([reportData, slotData, poolData]) => {
        if (cancelled) return
        setReport(reportData)
        setSlots(slotData.slots || [])
        const map = new Map()
        for (const p of poolData.players || []) map.set(p.playerId, p)
        setPool(map)
      },
    )
    return () => {
      cancelled = true
    }
  }, [week, leagueId])

  const { config } = useTeamConfig(slots, leagueId)

  // Horizon fetch for waiver advice. Separate from the main report load so a
  // slow or missing future week never delays or breaks the week on screen --
  // pointsByWeek simply stays null and the advice falls back to one week.
  useEffect(() => {
    let cancelled = false
    setPointsByWeek(null)
    const wanted = []
    for (let w = week; w < week + WAIVER_HORIZON_WEEKS && w <= maxWeek; w += 1) wanted.push(w)
    getWeeklyReports(wanted, leagueId).then((results) => {
      if (cancelled) return
      const present = results.filter((r) => r.report)
      const map = new Map()
      present.forEach(({ report: r }, idx) => {
        for (const entry of r.projections || []) {
          if (!map.has(entry.playerId)) map.set(entry.playerId, Array(present.length).fill(0))
          const pts = entry.projection?.points
          map.get(entry.playerId)[idx] = typeof pts === 'number' ? pts : 0
        }
      })
      setPointsByWeek(map)
      setHorizonWeeks(present.length)
    })
    return () => { cancelled = true }
  }, [week, maxWeek, leagueId])

  // Replacement level per position, from this league's own slots and team
  // count. Without it the waiver advice compares raw totals across positions
  // and a D/ST outranks a WR for arithmetic reasons rather than real ones.
  // Null until both the horizon and the team count are known; waiverReplacement
  // then falls back to raw totals and the wording says so.
  const replacement =
    slots && pool && pointsByWeek && teamCount
      ? replacementLevels(slots, teamCount, pointsByWeek, pool)
      : null

  const metaReady = slots !== null && pool !== null && config !== undefined
  const slotOrder = metaReady ? startingSlotOrder(slots) : []
  const benchCount = metaReady ? slots.filter((s) => s === BENCH_SLOT).length : 0
  // Indices of IR slots that actually hold someone. An empty IR slot is not
  // worth a card in a weekly report -- unlike an empty starting slot, which
  // is a hole the user needs to fill before kickoff.
  const irIndices = metaReady
    ? slots
        .map((s, i) => (s === IR_SLOT && config.slotAssignments[i] ? i : -1))
        .filter((i) => i !== -1)
    : []

  const projectionsById = new Map((report?.projections || []).map((p) => [p.playerId, p]))

  // Header numbers. Computed here rather than inside the render tree so the
  // starting-lineup total and the "better lineup" check see exactly the same
  // assignments the cards below are rendered from.
  const lineup = metaReady
    ? lineupProjection(slots, config.slotAssignments, projectionsById)
    : null
  const upgrade = metaReady
    ? betterLineup(slots, config.slotAssignments, projectionsById, pool)
    : null

  /**
   * Renders one card for a given roster-slot index (spec section 4, the
   * core join): empty-slot placeholder, normal PlayerCard (projection
   * found), or a degraded card (player assigned but no projection entry
   * for this week -- falls back to the player-pool entry). Never renders
   * nothing and never throws, regardless of which of the three cases applies.
   */
  function renderSlotCard(index, slotName, { muted = false } = {}) {
    const playerId = config.slotAssignments[index]

    if (!playerId) {
      return (
        <article className="player-card player-card-empty-slot" key={`empty-${index}`}>
          <span className="slot-empty-slot-label">{slotName}</span>
          <p className="slot-empty-message">Empty &mdash; assign a player.</p>
          <a className="slot-empty-link" href="#my-team">
            Assign in My Team &rarr;
          </a>
        </article>
      )
    }

    const projection = projectionsById.get(playerId)
    if (projection) {
      return (
        <PlayerCard
          key={playerId}
          player={projection}
          muted={muted}
          slotLabel={slotName}
          expanded={expandedRows.has(playerId)}
          onToggle={() => toggleRow(playerId)}
        />
      )
    }

    // Assigned player has no projection entry for this week -- degrade to
    // the player-pool fallback identity/status rather than dropping the
    // card or crashing.
    const poolEntry = pool.get(playerId) || {
      name: `Player ${playerId}`,
      position: '',
      team: '',
      newsFlag: { designation: 'Healthy', riskLevel: 'none', summary: null },
    }
    return (
      <article className={`player-card player-card-degraded ${muted ? 'player-card-muted' : ''}`} key={`degraded-${playerId}`}>
        <PlayerPickerRow player={poolEntry} />
        <p className="player-card-degraded-note">No projection available for this player this week.</p>
      </article>
    )
  }

  const waiverTargets = report
    ? [...(report.waiverTargets || [])]
        .filter((p) => !metaReady || !isPlayerRostered(p.playerId, config))
        .sort((a, b) => b.projection.points - a.projection.points)
    : []

  /**
   * Read-only "who this would replace" suggestion for a waiver target: an
   * open roster slot eligible for the target's position, if one exists,
   * otherwise the eligible, currently-rostered player with the lowest
   * projection this week. Purely informational -- picking a target doesn't
   * change My Team, the user still makes the swap themselves there. Not an
   * optimizer: it only compares this week's already-computed projections,
   * it doesn't attempt to model anything beyond that.
   *
   * Only ever called from within the `metaReady` branch below, where
   * `slots`/`config`/`pool` are guaranteed loaded -- this guard exists so a
   * future call site added before that gate can't crash instead of just
   * seeing no suggestion.
   */
  /**
   * Sentence for a waiver candidate's "what would this cost me" line.
   *
   * The decision itself lives in lineup.waiverReplacement -- it skips IR
   * slots and IR-eligible players (park them, do not drop them) and judges
   * over a multi-week horizon rather than the single week on screen. This
   * function only turns that result into words.
   */
  function suggestReplacement(position, candidateId) {
    if (!slots || !config || !pool || !pointsByWeek) return null

    const result = waiverReplacement({
      position,
      slots,
      slotAssignments: config.slotAssignments,
      poolById: pool,
      projectionsById,
      pointsByWeek,
      candidateId,
      weeks: horizonWeeks,
      replacement,
    })
    if (!result) return null

    if (result.kind === 'empty-slot') return `Fills your open ${result.slotName} slot.`

    const window = result.weeks > 1 ? `next ${result.weeks} weeks` : 'this week'

    // A position at its cap is a same-position question. Saying "you already
    // have better" out loud matters: silence reads as a missing feature.
    if (result.kind === 'upgrade') {
      return (
        `Upgrade on ${result.name} at ${result.position} -- ` +
        `+${result.gain.toFixed(1)} over the ${window}.`
      )
    }
    if (result.kind === 'have-better') {
      return `You already roster a better ${result.position} (${result.name}) -- no move needed.`
    }
    if (result.kind === 'no-legal-drop') {
      return 'No legal drop -- every bench player is needed for positional cover.'
    }
    // With replacement levels the compared numbers are value ABOVE the last
    // startable player at each position, which is the only way a D/ST and a WR
    // can be weighed against each other. Say which it is rather than printing
    // two numbers whose meaning depends on state the reader cannot see.
    const basis = result.adjusted ? 'above replacement' : 'projected'
    const inV = result.adjusted ? result.inSurplus : result.inHorizon
    const outV = result.adjusted ? result.outSurplus : result.outHorizon

    if (result.kind === 'hold') {
      return (
        `Better than ${result.name} (${result.slotName}) this week only ` +
        `(${fmt(result.inWeek)} vs ${fmt(result.outWeek)}), but worth less over the ${window} ` +
        `(${inV.toFixed(1)} vs ${outV.toFixed(1)} ${basis}) -- probably not worth the drop.`
      )
    }
    return (
      `Would replace ${result.name} (${result.slotName}) -- ` +
      `${inV.toFixed(1)} vs ${outV.toFixed(1)} ${basis} over the ${window}.`
    )
  }

  function fmt(n) {
    return typeof n === 'number' ? n.toFixed(1) : '--'
  }

  return (
    <section aria-label="Weekly report">
      <div className="view-header">
        <h1>Weekly Report</h1>
        <WeekSelector
          week={week}
          minWeek={MIN_SELECTABLE_WEEK}
          maxWeek={maxWeek}
          onChange={setWeek}
        />
      </div>

      {report === undefined && <LoadingSkeleton rows={6} label="Loading weekly report..." />}

      {report === null && <EmptyState message={`No data for week ${week} yet.`} />}

      {report && !metaReady && <LoadingSkeleton rows={6} label="Loading weekly report..." />}

      {report && metaReady && (
        <>
          <div className="lineup-summary">
            <div className="lineup-summary-main">
              <span className="lineup-summary-label">Projected lineup total</span>
              <span className="lineup-summary-total">{lineup.total.toFixed(1)}</span>
              <span className="lineup-summary-range">
                {lineup.low.toFixed(1)}&ndash;{lineup.high.toFixed(1)}
              </span>
              <span className="lineup-summary-meta">
                {lineup.counted} starter{lineup.counted === 1 ? '' : 's'}
                {lineup.missing > 0 && ` · ${lineup.missing} slot${lineup.missing === 1 ? '' : 's'} empty`}
              </span>
            </div>

            {upgrade.moves.length > 0 ? (
              <div className="lineup-summary-warning" role="status">
                <strong>
                  A better lineup is available: +{upgrade.gain.toFixed(1)} pts
                </strong>
                <ul className="lineup-summary-moves">
                  {upgrade.moves.map((m) => (
                    <li key={`${m.fromIndex}-${m.slotIndex}`}>
                      Start <strong>{m.playerName}</strong>
                      {m.outPlayerName ? <> over {m.outPlayerName}</> : <> in the empty slot</>} at{' '}
                      {m.slotName} <span className="lineup-move-delta">+{m.delta.toFixed(1)}</span>
                    </li>
                  ))}
                </ul>
                <a className="slot-empty-link" href="#my-team">
                  Change in My Team &rarr;
                </a>
              </div>
            ) : (
              <div className="lineup-summary-ok">
                Best available lineup &mdash; no bench player outscores a starter.
              </div>
            )}
          </div>

          <div className="report-section">
            <h2>Start</h2>
            {slotOrder.map((slotName) => {
              const indices = slots
                .map((s, i) => (s === slotName ? i : -1))
                .filter((i) => i !== -1)
              return (
                <div className="slot-group" key={slotName}>
                  <h3 className="slot-group-heading">{slotName}</h3>
                  <div className="player-card-grid">
                    {indices.map((i) => renderSlotCard(i, slotName))}
                  </div>
                </div>
              )
            })}
          </div>

          <div className="report-section">
            <h2>
              Sit {benchCount > 0 && <span className="section-subcount">({benchCount} bench slots)</span>}
            </h2>
            <div className="player-card-grid">
              {slots
                .map((s, i) => (s === BENCH_SLOT ? i : -1))
                .filter((i) => i !== -1)
                .map((i) => renderSlotCard(i, BENCH_SLOT, { muted: true }))}
            </div>
          </div>

          {irIndices.length > 0 && (
            <div className="report-section">
              <h2>
                Injured reserve{' '}
                <span className="section-subcount">(not startable)</span>
              </h2>
              <div className="player-card-grid">
                {irIndices.map((i) => renderSlotCard(i, IR_SLOT, { muted: true }))}
              </div>
            </div>
          )}

          <div className="report-section">
            <h2>Waiver Targets</h2>
            {waiverTargets.length === 0 ? (
              <EmptyState message="No waiver targets available — this week's top candidates are already on your team." />
            ) : (
              <div className="player-list-group">
                {waiverTargets.map((p) => (
                  <PlayerCard
                    key={p.playerId}
                    player={p}
                    slotLabel={p.position}
                    expanded={expandedRows.has(p.playerId)}
                    onToggle={() => toggleRow(p.playerId)}
                    rationale={p.rationale}
                    replacement={suggestReplacement(p.position, p.playerId)}
                  />
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </section>
  )
}
