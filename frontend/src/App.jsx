import { useEffect, useState } from 'react'
import WeeklyReportView from './components/WeeklyReportView.jsx'
import TeamConfigView from './components/TeamConfigView.jsx'
import TrackRecordView from './components/TrackRecordView.jsx'
import ExploreView from './components/ExploreView.jsx'
import BriefingsView from './components/BriefingsView.jsx'

const TABS = [
  { id: 'report', label: 'Weekly Report' },
  { id: 'briefings', label: 'Briefings' },
  { id: 'explore', label: 'Explore' },
  { id: 'my-team', label: 'My Team' },
  { id: 'track-record', label: 'Track Record' },
]

const LEAGUES = [
  { id: 'league-1', label: 'AH Football League' },
  { id: 'league-2', label: 'Intuitech Fantasy' },
]

function tabFromHash() {
  const hash = window.location.hash.replace('#', '')
  return TABS.some((t) => t.id === hash) ? hash : 'report'
}

export default function App() {
  const [activeTab, setActiveTab] = useState(tabFromHash)

  const [activeLeagueId, setActiveLeagueId] = useState(() => {
    try {
      return localStorage.getItem('nfl-fantasy-assistant:active-league') || 'league-1'
    } catch {
      return 'league-1'
    }
  })

  useEffect(() => {
    const onHashChange = () => setActiveTab(tabFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  useEffect(() => {
    try {
      localStorage.setItem('nfl-fantasy-assistant:active-league', activeLeagueId)
    } catch {
      // localStorage unavailable — ignore
    }
  }, [activeLeagueId])

  function selectTab(id) {
    setActiveTab(id)
    window.history.replaceState(null, '', `#${id}`)
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-inner">
          <div className="app-brand">
            <span className="app-brand-name">NFL Value Assistant</span>
            <span className="app-brand-tag">start/sit &amp; waiver recommendations</span>
          </div>
          <div className="app-header-controls">
            <label className="league-selector-label" htmlFor="league-selector">
              League:
            </label>
            <select
              id="league-selector"
              className="league-selector"
              value={activeLeagueId}
              onChange={(e) => setActiveLeagueId(e.target.value)}
            >
              {LEAGUES.map((league) => (
                <option key={league.id} value={league.id}>
                  {league.label}
                </option>
              ))}
            </select>
          </div>
          <nav className="tabs" role="tablist" aria-label="Main view">
            {TABS.map((tab) => (
              <button
                key={tab.id}
                role="tab"
                aria-selected={activeTab === tab.id}
                className={`tab-button ${activeTab === tab.id ? 'tab-button-active' : ''}`}
                onClick={() => selectTab(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      <main className="app-main">
        {activeTab === 'report' && <WeeklyReportView leagueId={activeLeagueId} />}
        {activeTab === 'briefings' && <BriefingsView leagueId={activeLeagueId} />}
        {activeTab === 'explore' && <ExploreView leagueId={activeLeagueId} />}
        {activeTab === 'my-team' && <TeamConfigView leagueId={activeLeagueId} />}
        {activeTab === 'track-record' && <TrackRecordView leagueId={activeLeagueId} />}
      </main>

      {/* Mobile bottom navigation — hidden on desktop via CSS */}
      <nav className="mobile-bottom-nav" aria-label="Main navigation">
        <button
          className={`mobile-nav-btn ${activeTab === 'report' ? 'mobile-nav-btn-active' : ''}`}
          onClick={() => selectTab('report')}
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <rect x="4" y="4" width="7" height="7" rx="1.2" stroke="currentColor" strokeWidth="1.5" />
            <rect x="13" y="4" width="7" height="7" rx="1.2" stroke="currentColor" strokeWidth="1.5" />
            <rect x="4" y="13" width="7" height="7" rx="1.2" stroke="currentColor" strokeWidth="1.5" />
            <rect x="13" y="13" width="7" height="7" rx="1.2" stroke="currentColor" strokeWidth="1.5" />
          </svg>
          <span>Report</span>
        </button>
        <button
          className={`mobile-nav-btn ${activeTab === 'explore' ? 'mobile-nav-btn-active' : ''}`}
          onClick={() => selectTab('explore')}
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.5" />
            <path d="M15.5 8.5L13.2 13.2L8.5 15.5L10.8 10.8L15.5 8.5Z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
          </svg>
          <span>Explore</span>
        </button>
        <button
          className={`mobile-nav-btn ${activeTab === 'my-team' ? 'mobile-nav-btn-active' : ''}`}
          onClick={() => selectTab('my-team')}
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle cx="9" cy="8" r="3" stroke="currentColor" strokeWidth="1.5" />
            <circle cx="17" cy="9" r="2.4" stroke="currentColor" strokeWidth="1.5" />
            <path d="M3.5 19c0-3 2.5-5 5.5-5s5.5 2 5.5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            <path d="M14.5 14.5c2.4.2 4 1.9 4 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          <span>My Team</span>
        </button>
        <button
          className={`mobile-nav-btn ${activeTab === 'track-record' ? 'mobile-nav-btn-active' : ''}`}
          onClick={() => selectTab('track-record')}
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M4 20V10M11 20V4M18 20V13" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
          </svg>
          <span>Track</span>
        </button>
      </nav>

      <footer className="app-footer">
        Portfolio project &middot; mock data, no live backend yet &middot; see project case study for build details
      </footer>
    </div>
  )
}
