# Free Data Source Comparison (v2 — cost-driven revisit)

Research date: 2026-08-04. Purpose: the paid options evaluated in `api-comparison.md` (Fantasy Nerds ~$199.95/yr, SportsDataIO Discovery Lab ~$99–149/mo) are too expensive for a portfolio project. This doc surveys free alternatives against the same four v1 needs — stats, projections, news, injuries — plus the one thing we assumed would need to be paid: fantasy point projections / rankings.

**Headline finding: a fully free stack covers all four v1 needs, including projections/rankings.** The nflverse ecosystem (community-maintained, used widely in the fantasy-analytics space) already redistributes FantasyPros' consensus rankings and projections for free, on a weekly cadence. That means the "build my own model or type numbers in by hand" fallback probably isn't necessary for v1 — it can be a v2 upgrade (a custom model that beats the free consensus) rather than a v1 requirement.

## Summary Table

| | nflverse (`nflreadpy` / `nflreadr`) | DynastyProcess / `ffpros` (FantasyPros scrape) | Sleeper API | ESPN hidden API | API-Sports (American Football) |
|---|---|---|---|---|---|
| **Cost** | Free, unlimited | Free, unlimited | Free, unlimited | Free, unlimited | Free tier: 100 req/day |
| **Auth** | None | None | None | None | API key, free signup |
| **Stats (historical)** | Yes — play-by-play, weekly/seasonal, snap counts, Next Gen Stats, combine | No | Partial (season/week stats via league context) | Yes | Yes |
| **Projections** | No (only "expected fantasy points" from opportunity/usage models, not a real projection) | **Yes** — `fp_projections()` pulls FantasyPros' weekly/ROS projections by position/scoring format | No | Yes, via `kona_player_info` view (undocumented) | Not confirmed free-tier |
| **Rankings** | Indirectly (loads DynastyProcess's ranking file) | **Yes** — `fp_rankings()` / `load_ff_rankings()`: FantasyPros Expert Consensus Rankings (ECR), updated weekly, draft + in-season + full archive | No | Yes (draft rankings within `kona_player_info`) | No |
| **Injuries** | Yes — structured injury report (status: Questionable/Doubtful/Out), depth charts | No | Partial — player status flags | Yes, incl. free-text notes | Yes |
| **News (free text)** | No | No | No | Yes — `/apis/site/v2/sports/football/nfl/news` | Limited |
| **Official / supported** | Community project, not Anthropic/vendor-official but actively maintained, used broadly in fantasy-analytics circles | Community project (DynastyProcess), scrapes FantasyPros' public pages | Official but the API itself is undocumented outside community write-ups; intended for Sleeper's own app | **Unofficial** — reverse-engineered from ESPN's own web/app traffic, no ToS coverage, can change or break without notice | Official, documented, small free quota |
| **Redistribution risk** | Low — nflverse data is CC-BY / CC-BY-SA 4.0, explicitly open | Medium — underlying rankings/projections are FantasyPros' IP scraped via a public page, not a licensed feed; fine for personal/internal use, riskier for a public-facing redistribution of raw numbers | Low (Sleeper's own public API, read-only) | Medium-high — ESPN doesn't publish or license this; treat as "works today, no guarantee" | Low — official free tier explicitly for this purpose |

## Details by source

### nflverse (`nflreadpy` for Python, `nflreadr` for R)
The direct free replacement for the "stats + injuries" portion of both paid vendors. No API key, no rate limit, actively maintained (successor to the now-archived `nfl_data_py`). Pulls from `nflfastR`, `nfldata`, and partner projects. Gives play-by-play, weekly/seasonal player and team stats, rosters, schedules, snap counts, Next Gen Stats, QBR, combine data, depth charts, and a structured injury report (designation by week, not narrative text). This alone replaces most of what SportsDataIO's "Sports Data" and "Injuries" feeds would have provided.

Gap: no forward-looking fantasy point projections of its own — its "fantasy" tables are usage/opportunity-based (e.g., expected fantasy points from target share and red-zone touches), which is a good *feature* for a value engine but not the same as a projection to display to a user.

### DynastyProcess data / `ffpros` — fills the projections gap
This is the finding that changes the plan. DynastyProcess (part of the same open-source `ffverse`/`nflverse` circle) publishes a weekly-updated, free dataset that includes FantasyPros' Expert Consensus Rankings and projections:

- `fp_rankings()` / `nflreadr::load_ff_rankings()` — FantasyPros ECR (rank, standard deviation, best/worst rank, ownership %), for draft, weekly in-season, or the full historical archive.
- `fp_projections()` — FantasyPros' own weekly/rest-of-season projections by position and scoring format (standard/PPR/half-PPR).
- Raw files are also downloadable directly as CSV/parquet from the `dynastyprocess/data` GitHub repo (`db_fpecr.csv.gz`, updated via GitHub Actions weekly) — this can be pulled with a plain HTTP GET from any language/backend, not just R.

This is effectively "the same numbers the NFL Fantasy App and most fantasy sites show you," sourced for free because FantasyPros' own public rankings pages are the underlying source (the DynastyProcess project scrapes and republishes them under an open license, GPL-3.0 for the repo itself). Caveat: this is a step removed from an official licensed feed — treat it as solid for a personal-use/portfolio tool, but don't present raw FantasyPros numbers as a commercial redistribution without checking FantasyPros' own terms.

### FantasyPros API (official, direct)
Worth naming separately from the DynastyProcess scrape: FantasyPros does offer its own API with a **free tier explicitly for personal, non-commercial, prototype use** (request a key at `secure.fantasypros.com/api-keys/request`). It's the "straight from the source" option if the DynastyProcess redistribution feels too indirect, with a paid "Hall of Fame" tier for higher rate limits if the project ever needs more.

### Sleeper API
Completely free, no key, effectively no published rate limit, read-only. Its main value here isn't projections (it doesn't have them) but as a clean, zero-friction source of player IDs, trending adds/drops, and basic status — useful as a supplementary layer or for cross-referencing player IDs across sources. Good backup, not a primary projections/rankings source.

### ESPN hidden/unofficial API — fills the news gap
Not an official product — this is the same API ESPN's own web and app clients call, reverse-engineered and documented by the community (e.g., the `kona_player_info` view for player/ranking data, and `/apis/site/v2/sports/football/nfl/news` for free-text news articles). No key, no official rate limit, but no guarantees either — it can change shape or get blocked without notice, and there's no ToS covering third-party use. This is the best free candidate for the "News & injury layer" (LLM-summarized news) piece of v1, since none of the other free sources here provide narrative news text. Use it, but build the news layer so it degrades gracefully if the endpoint changes.

### API-Sports (American Football) — small official free tier
A legitimate, documented, official API with a permanent free tier (100 requests/day, no card required), paid tiers from ~$10–19/month. The free quota is too small for a full weekly-report pipeline pulling many players, but usable for narrow tasks (e.g., a daily batch job) or as a fallback/cross-check source.

## Recommended free stack for v1

1. **Stats, schedules, rosters, snap counts, structured injury status** → `nflreadpy` (Python, matches the Node/Python backend in the brief).
2. **Fantasy rankings and projections** → DynastyProcess/`ffpros` weekly feed (or the official FantasyPros free-tier API if you want the licensing story to be one step cleaner). This removes the need to build a custom projection model or hand-enter numbers from the NFL Fantasy App for v1 — that becomes a legitimate v2 differentiator ("our model vs. consensus ECR") rather than a v1 blocker.
3. **News (free-text, for the LLM summarization layer)** → ESPN's unofficial news endpoint, with a fallback plan (e.g., cache last-known-good, or fall back to injury-status-only summaries) if the endpoint breaks, since it's unofficial.
4. **Optional cross-check / player-ID mapping** → Sleeper API, free and frictionless.

Total cost: **$0**, all four v1 data needs covered, with the FantasyPros official free tier as a one-step-cleaner backup for rankings/projections if the DynastyProcess redistribution's licensing feels too indirect for a portfolio piece you'll show publicly.

## Open questions / needs hands-on verification

- Confirm the DynastyProcess/FantasyPros redistribution is comfortable to depend on for a *public, shareable* tool (not just personal use) — read FantasyPros' terms on their own rankings/projections pages, since the "medium redistribution risk" flagged above is about public display, not personal use.
- ESPN's hidden API has no SLA — confirm the specific news endpoint still returns expected data at build time, since these endpoints are known to shift periodically.
- `nflreadpy` is young relative to the archived `nfl_data_py` (which was the more battle-tested tool) — sanity-check a few calls against known stat lines before building on it.
- If narrative injury detail (not just status) matters for the news/risk-flagging layer, ESPN's feed and/or Sleeper's status fields should be compared side by side for depth before picking one as primary.
