// Data-fetching layer.
//
// Per spec section 6: these functions read static mock JSON fixtures bundled
// with the build today. Later they will call a real backend endpoint that
// returns the identical shape (WeeklyReport / TrackRecord) -- no view-layer
// changes required at that swap. Components must always go through these
// functions rather than importing fixture JSON directly.

// The latest week for which a mock fixture exists. This drives the default
// week shown on load (spec section 2, "entry point"). Not a hardcoded roster
// or scoring value -- just which mock files ship with this build.
export const LATEST_AVAILABLE_WEEK = 3;

// Week range offered in the UI's week selector. MAX_SELECTABLE_WEEK is
// intentionally one past the latest fixture so the "no data for this week"
// empty state (spec section 2) is reachable without waiting for real weeks
// to run out of data.
export const MIN_SELECTABLE_WEEK = 1;
export const MAX_SELECTABLE_WEEK = 4;

const DEFAULT_SEASON = 2026;

/**
 * Fetches and parses a JSON fixture, treating anything that isn't a real
 * JSON response as "no data" rather than throwing.
 *
 * This matters beyond just handling real 404s: local dev/preview servers
 * (Vite's included) commonly fall back to serving index.html (200 OK,
 * text/html) for any unmatched path, including a missing mock fixture --
 * a naive `!response.ok` check alone would miss that case and crash on
 * `response.json()`. Guarding on content-type keeps the empty state
 * reliable in dev, preview, and a real static host alike.
 */
async function fetchJsonFixture(path) {
  let response;
  try {
    response = await fetch(path);
  } catch {
    return null;
  }
  if (!response.ok) {
    return null;
  }
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('json')) {
    return null;
  }
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/**
 * Fetch the weekly report for a given week and league.
 * @param {number} [week] - defaults to the latest available week.
 * @param {string} [leagueId] - defaults to 'league-1'.
 * @returns {Promise<object|null>} the WeeklyReport object, or null if no
 *   fixture exists for that week (renders the "no data" empty state).
 */
export async function getWeeklyReport(week = LATEST_AVAILABLE_WEEK, leagueId = 'league-1') {
  return fetchJsonFixture(`${import.meta.env.BASE_URL}mock/${leagueId}/weekly-report-week-${week}.json`);
}

/**
 * Fetch the season-to-date track record for a given league.
 * @param {number} [season] - defaults to the current mock season (2026).
 * @param {string} [leagueId] - defaults to 'league-1'.
 * @returns {Promise<object|null>} the TrackRecord object, or null if no
 *   fixture exists for that season.
 */
export async function getTrackRecord(season = DEFAULT_SEASON, leagueId = 'league-1') {
  const data = await fetchJsonFixture(`${import.meta.env.BASE_URL}mock/${leagueId}/track-record.json`);
  if (!data || data.season !== season) {
    return null;
  }
  return data;
}

/**
 * Fetch the generic roster slot config (spec section 3.3). Stored as its own
 * small fixture so it stays trivially swappable once the real league roster
 * is confirmed -- no component should hardcode slot names/counts.
 * Shared across all leagues -- do NOT add a leagueId parameter here.
 * @returns {Promise<{slots: string[]}>}
 */
export async function getRosterSlots() {
  const data = await fetchJsonFixture(`${import.meta.env.BASE_URL}mock/roster-slots.json`);
  return data || { slots: [] };
}

/**
 * Fetch the broader, week-independent player pool (team-config spec section
 * 3.2) -- the picker source for team configuration, and the fallback
 * identity/status source for Weekly Report when an assigned player has no
 * projection entry for the selected week.
 * @param {string} [leagueId] - defaults to 'league-1'.
 * @returns {Promise<{players: object[]}>}
 */
export async function getPlayerPool(leagueId = 'league-1') {
  const data = await fetchJsonFixture(`${import.meta.env.BASE_URL}mock/${leagueId}/player-pool.json`);
  return data || { players: [] };
}

/**
 * Fetch the one-time default team config seed (team-config spec section
 * 3.4). Only ever read when localStorage has no saved config yet -- callers
 * go through lib/teamConfig.js, never fetch this directly.
 * @param {string} [leagueId] - defaults to 'league-1'.
 * @returns {Promise<{slotAssignments: (string|null)[]}|null>} null if the
 *   fixture is missing/unreadable -- caller must handle gracefully.
 */
export async function getDefaultTeamConfig(leagueId = 'league-1') {
  return fetchJsonFixture(`${import.meta.env.BASE_URL}mock/${leagueId}/default-team-config.json`);
}
