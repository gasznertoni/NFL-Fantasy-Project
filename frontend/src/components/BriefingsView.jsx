import { useEffect, useState } from 'react'
import { getBriefing, listBriefings } from '../lib/api.js'
import LoadingSkeleton from './LoadingSkeleton.jsx'
import EmptyState from './EmptyState.jsx'

// Spec: docs/specs/briefings-view.md.
//
// A briefing is a different artefact from the Weekly Report. That view answers
// "what does the model project for all ~990 players"; this one answers "what
// should I do with my ten starters right now, and what changed since the last
// briefing". Short, dated, opinionated, with a lineup in it.

const KIND_LABEL = { monday: 'Mon', wednesday: 'Wed', saturday: 'Sat' }

// Slot render order. Matches the league's roster spec rather than being
// alphabetical, so the lineup reads the way the app's own roster does.
const SLOT_ORDER = ['QB', 'RB', 'WR', 'TE', 'FLEX', 'K', 'DEF']

function slotRank(slot) {
  const i = SLOT_ORDER.indexOf(String(slot || '').toUpperCase())
  return i === -1 ? SLOT_ORDER.length : i
}

/** "2 days ago" / "today", from an ISO timestamp. Returns null if unparseable —
 *  callers render the raw string in that case rather than a wrong age. */
function ageInDays(iso) {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return null
  return Math.floor((Date.now() - then) / 86400000)
}

function freshnessPhrase(iso) {
  const days = ageInDays(iso)
  if (days === null) return null
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  return `${days} days ago`
}

/**
 * The source banner. Always rendered, and the most prominent element on the
 * page when the data is not live.
 *
 * This is the spec's one render-blocking requirement, and it comes from
 * watching the routines run: the most valuable thing the 2026-09-05 briefing
 * produced was its opening line saying the data was two days old. A briefing
 * whose provenance is invisible is worse than none, because the reader assumes
 * it is live.
 */
function SourceBanner({ source }) {
  if (!source || !source.label) {
    return (
      <div className="briefing-source briefing-source-error" role="alert">
        <strong>Source unknown.</strong> This briefing did not record where its
        injury data came from, so its numbers cannot be trusted. Treat it as
        unverified.
      </div>
    )
  }
  const phrase = freshnessPhrase(source.asOf)
  return (
    <div
      className={`briefing-source ${source.isLive ? 'briefing-source-live' : 'briefing-source-stale'}`}
      role={source.isLive ? undefined : 'alert'}
    >
      <span className="briefing-source-label">
        {source.isLive ? 'Live data' : 'Not live'}
      </span>
      <span>
        {source.label}
        {phrase ? <> &middot; as of <strong>{phrase}</strong></> : null}
        {!phrase && source.asOf ? <> &middot; as of {source.asOf}</> : null}
      </span>
      {source.note ? <span className="briefing-source-note">{source.note}</span> : null}
    </div>
  )
}

/** One lineup row. Expected points is the headline; the conditional number and
 *  P(play) sit beside it, visually subordinate. Reuses PlayerCard's phrasing so
 *  the same idea does not get two visual languages. */
function LineupRow({ row }) {
  const pct =
    typeof row.playProbability === 'number' ? Math.round(row.playProbability * 100) : null
  const risky = typeof row.playProbability === 'number' && row.playProbability < 0.85
  return (
    <tr className={risky ? 'briefing-row-risky' : undefined}>
      <td className="briefing-slot">{row.slot}</td>
      <td>
        <span className="briefing-player">{row.name}</span>
        <span className="briefing-meta">
          {row.position}
          {row.opponent ? ` · ${row.opponent}` : ''}
          {row.designation && row.designation !== 'Healthy' ? (
            <span className="briefing-flag">{row.designation}</span>
          ) : null}
          {row.practice ? <span className="briefing-practice">{row.practice}</span> : null}
        </span>
        {row.reason ? <span className="briefing-reason">{row.reason}</span> : null}
      </td>
      <td className="briefing-num">
        <span className="briefing-expected">
          {typeof row.expectedPoints === 'number' ? row.expectedPoints.toFixed(1) : '—'}
        </span>
        <span className="briefing-sub">
          {pct !== null ? `${pct}% likely to play` : null}
          {pct !== null && typeof row.conditionalPoints === 'number' ? ' · ' : null}
          {typeof row.conditionalPoints === 'number'
            ? `${row.conditionalPoints.toFixed(1)} if he does`
            : null}
        </span>
      </td>
    </tr>
  )
}

function Changes({ changes }) {
  if (!changes || changes.length === 0) {
    // An explicit statement, not an absence: "nothing moved" is information a
    // reader wants, and a blank space does not convey it.
    return (
      <p className="briefing-nochange">No changes since the last briefing.</p>
    )
  }
  return (
    <ul className="briefing-changes">
      {changes.map((c, i) => (
        <li key={i} className={`briefing-change briefing-change-${c.action}`}>
          <span className="briefing-change-action">{c.action}</span>
          <strong>{c.name}</strong>
          {c.replacing ? <> over {c.replacing}</> : null}
          {typeof c.deltaExpectedPoints === 'number' ? (
            <span className="briefing-delta">
              {c.deltaExpectedPoints >= 0 ? '+' : ''}
              {c.deltaExpectedPoints.toFixed(2)}
            </span>
          ) : null}
          {c.reason ? <span className="briefing-reason">{c.reason}</span> : null}
        </li>
      ))}
    </ul>
  )
}

function Collapsible({ title, count, children }) {
  if (!count) return null
  return (
    <details className="briefing-details">
      <summary>
        {title} <span className="section-subcount">{count}</span>
      </summary>
      {children}
    </details>
  )
}

function BriefingDetail({ briefing, onBack }) {
  const lineup = [...(briefing.lineup || [])].sort(
    (a, b) => slotRank(a.slot) - slotRank(b.slot)
  )
  const flagged = lineup.filter((r) => r.practice)

  return (
    <article className="briefing-detail">
      <button type="button" className="briefing-back" onClick={onBack}>
        &larr; All briefings
      </button>

      <header className="briefing-detail-head">
        <h2>
          Week {briefing.week} &middot; {KIND_LABEL[briefing.kind] || briefing.kind}
        </h2>
        <span className="briefing-meta">{briefing.slug}</span>
      </header>

      <SourceBanner source={briefing.source} />

      <h3>Changes</h3>
      <Changes changes={briefing.changes} />

      <h3>
        Start these {lineup.length || 'ten'}
        {typeof briefing.lineupTotal === 'number' ? (
          <span className="briefing-total">{briefing.lineupTotal.toFixed(1)} expected</span>
        ) : null}
      </h3>
      <table className="briefing-lineup">
        <thead>
          <tr>
            <th scope="col">Slot</th>
            <th scope="col">Player</th>
            <th scope="col" className="briefing-num">Expected</th>
          </tr>
        </thead>
        <tbody>
          {lineup.map((row) => (
            <LineupRow key={`${row.slot}-${row.playerId || row.name}`} row={row} />
          ))}
        </tbody>
      </table>

      {/* Wednesday only: omitted entirely rather than shown empty, because the
          practice column only exists on that run and an empty section would
          read as "nobody is flagged". */}
      {briefing.kind === 'wednesday' && flagged.length > 0 ? (
        <>
          <h3>Practice report</h3>
          <ul className="briefing-practice-list">
            {flagged.map((r) => (
              <li key={r.playerId || r.name}>
                <strong>{r.name}</strong> — {r.designation || 'flagged'}, practised{' '}
                <em>{r.practice}</em>
                {typeof r.playProbability === 'number'
                  ? ` → ${Math.round(r.playProbability * 100)}% to play`
                  : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <Collapsible title="Watch list" count={(briefing.watchList || []).length}>
        <ul>
          {(briefing.watchList || []).map((w, i) => (
            <li key={i}>
              <strong>{w.name}</strong> — {w.why}
              {w.newsDue ? <span className="briefing-meta"> news due {w.newsDue}</span> : null}
            </li>
          ))}
        </ul>
      </Collapsible>

      <Collapsible title="Opponent notes" count={(briefing.opponentNotes || []).length}>
        <ul>
          {(briefing.opponentNotes || []).map((n, i) => (
            <li key={i}>{n.text}</li>
          ))}
        </ul>
      </Collapsible>

      <Collapsible title="Byes ahead" count={(briefing.byesAhead || []).length}>
        <ul>
          {(briefing.byesAhead || []).map((b, i) => (
            <li key={i}>
              <strong>Week {b.week}</strong> — {(b.players || []).join(', ')}
            </li>
          ))}
        </ul>
      </Collapsible>

      {(briefing.caveats || []).length > 0 ? (
        <div className="briefing-caveats">
          {briefing.caveats.map((c, i) => (
            <p key={i}>{c}</p>
          ))}
        </div>
      ) : null}

      {briefing.markdown ? (
        <details className="briefing-details">
          <summary>Read the full briefing</summary>
          <pre className="briefing-markdown">{briefing.markdown}</pre>
        </details>
      ) : null}

      {briefing.runUrl ? (
        <p className="briefing-meta">
          <a href={briefing.runUrl} target="_blank" rel="noreferrer">
            Open the run that produced this
          </a>
        </p>
      ) : null}
    </article>
  )
}

function BriefingCard({ summary, onOpen }) {
  const phrase = freshnessPhrase(summary.generatedAt)
  return (
    <button type="button" className="briefing-card" onClick={() => onOpen(summary.slug)}>
      <span className="briefing-card-head">
        <span className="briefing-kind">{KIND_LABEL[summary.kind] || summary.kind}</span>
        <span className="briefing-card-date">{summary.slug}</span>
        {summary.sourceIsLive === false ? (
          <span className="briefing-stale-chip">not live</span>
        ) : null}
      </span>
      <span className="briefing-card-summary">
        {summary.changeSummary || 'No changes'}
      </span>
      <span className="briefing-meta">
        Week {summary.week}
        {typeof summary.lineupTotal === 'number'
          ? ` · ${summary.lineupTotal.toFixed(1)} expected`
          : ''}
        {phrase ? ` · ${phrase}` : ''}
      </span>
    </button>
  )
}

export default function BriefingsView({ leagueId = 'league-2' }) {
  const [summaries, setSummaries] = useState(undefined) // undefined = loading
  const [openSlug, setOpenSlug] = useState(null)
  const [briefing, setBriefing] = useState(undefined)

  useEffect(() => {
    let cancelled = false
    setSummaries(undefined)
    setOpenSlug(null)
    listBriefings(leagueId).then((rows) => {
      if (!cancelled) setSummaries(rows)
    })
    return () => {
      cancelled = true
    }
  }, [leagueId])

  useEffect(() => {
    if (!openSlug) return undefined
    let cancelled = false
    setBriefing(undefined)
    getBriefing(leagueId, openSlug).then((data) => {
      if (!cancelled) setBriefing(data)
    })
    return () => {
      cancelled = true
    }
  }, [leagueId, openSlug])

  if (summaries === undefined) {
    return <LoadingSkeleton />
  }

  if (openSlug) {
    if (briefing === undefined) return <LoadingSkeleton />
    if (briefing === null) {
      return (
        <section aria-label="Briefing">
          <button type="button" className="briefing-back" onClick={() => setOpenSlug(null)}>
            &larr; All briefings
          </button>
          <EmptyState message={`No briefing found for ${openSlug}.`} />
        </section>
      )
    }
    return (
      <section aria-label="Briefing">
        <BriefingDetail briefing={briefing} onBack={() => setOpenSlug(null)} />
      </section>
    )
  }

  if (summaries.length === 0) {
    return (
      <section aria-label="Briefings">
        <EmptyState
          message={
            'No briefings yet. The scheduled routines write one every Monday, ' +
            'Wednesday and Saturday.'
          }
        />
      </section>
    )
  }

  // Grouped by week, most recent first. Weeks are a natural chunk here: three
  // briefings describe one week's decision as it firms up.
  const byWeek = summaries.reduce((acc, s) => {
    const key = s.week ?? 'unknown'
    ;(acc[key] = acc[key] || []).push(s)
    return acc
  }, {})
  const weeks = Object.keys(byWeek).sort((a, b) => Number(b) - Number(a))

  return (
    <section aria-label="Briefings">
      {weeks.map((week) => (
        <div key={week} className="briefing-week">
          <h2>Week {week}</h2>
          <div className="briefing-list">
            {byWeek[week].map((s) => (
              <BriefingCard key={s.slug} summary={s} onOpen={setOpenSlug} />
            ))}
          </div>
        </div>
      ))}
    </section>
  )
}
