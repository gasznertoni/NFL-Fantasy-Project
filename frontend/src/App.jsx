import { useEffect, useState } from 'react'
import WeeklyReportView from './components/WeeklyReportView.jsx'
import TeamConfigView from './components/TeamConfigView.jsx'
import TrackRecordView from './components/TrackRecordView.jsx'

const TABS = [
  { id: 'report', label: 'Weekly Report' },
  { id: 'my-team', label: 'My Team' },
  { id: 'track-record', label: 'Track Record' },
]

function tabFromHash() {
  const hash = window.location.hash.replace('#', '')
  return TABS.some((t) => t.id === hash) ? hash : 'report'
}

export default function App() {
  // Tab state is optionally synced to the URL hash (#report / #track-record)
  // so the current view is shareable via link -- nice-to-have per spec
  // section 2, not required for acceptance. Switching tabs never triggers a
  // full page reload; this is client-side state, the hash update is just
  // for shareability.
  const [activeTab, setActiveTab] = useState(tabFromHash)

  useEffect(() => {
    const onHashChange = () => setActiveTab(tabFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  function selectTab(id) {
    setActiveTab(id)
    window.history.replaceState(null, '', `#${id}`)
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-inner">
          <div className="app-brand">
            <span className="app-brand-name">NFL Fantasy Value Assistant</span>
            <span className="app-brand-tag">start/sit &amp; waiver recommendations, tracked against real outcomes</span>
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
        {activeTab === 'report' && <WeeklyReportView />}
        {activeTab === 'my-team' && <TeamConfigView />}
        {activeTab === 'track-record' && <TrackRecordView />}
      </main>

      <footer className="app-footer">
        Portfolio project &middot; mock data, no live backend yet &middot; see project case study for build details
      </footer>
    </div>
  )
}
