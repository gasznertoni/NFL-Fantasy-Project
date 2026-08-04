# Data Provider Comparison: Fantasy Nerds vs. SportsDataIO

Research date: 2026-08-04. Purpose: pick the v1 data provider per the CLAUDE.md roadmap (step 1). Findings below come from public marketing/docs pages reachable via search — several pages returned HTTP 403 to direct fetch (likely bot-blocked), so some figures are triangulated from search snippets and third-party listings rather than the primary docs page. These are flagged in "Open Questions" and should be re-verified hands-on before committing.

## Summary Table

| | Fantasy Nerds | SportsDataIO (Discovery Lab) |
|---|---|---|
| **Pricing** | ~$199.95/year for the NFL package (annual, not monthly) | ~$99–$149/month self-serve tier; free tier with last-season-only data |
| **Rate limits** | Not published; docs note data "does not change more than once or twice a day" and warn against excessive polling (risk of suspension) rather than a hard published cap | Reported 100–1,000 calls/day depending on sub-tier (unverified exact figures); "next-day data," not real-time |
| **Stats / performance history** | Yes — seasonal & weekly player stats (gated to paid/"commercial" access per third-party wrapper) | Yes — "Scores, Stats & Plays" under Sports Data feeds |
| **Projections** | Yes — weekly + rest-of-season projections, DFS salary/value scores (FanDuel/DraftKings/Yahoo) | Yes — "Projections & Points" under Fantasy Data feeds |
| **Defensive/matchup rankings** | Not explicitly confirmed in docs found; draft rankings & position tiers exist, no dedicated "defense vs. position" endpoint surfaced | Not explicitly confirmed either; likely derivable from team Stats & Standings feeds, but no dedicated matchup-difficulty endpoint surfaced |
| **News** | Yes — player/team news + "fantasy analysis from around the industry" | Yes — dedicated "News & Images" category (player news, previews, recaps) |
| **Injuries** | Yes — dedicated injuries endpoint, "injured player status by week" | Yes — injuries listed under core Sports Data feeds |
| **Auth** | API key (query param); "TEST" key available for sample data without a purchase | API key via HTTP GET (standard across SportsDataIO products) |
| **Redistribution / public display terms** | Not found in public docs; commercial licensing mentioned as a separate tier, implying the standard tier may restrict redistribution — unverified | Explicit: **may not resell, redistribute, sub-license, or share your license key or the API output as a competing data product**; Discovery Lab tier is explicitly described as "not licensed for commercial redistribution" |

## Fantasy Nerds — Findings

- Docs root: [api.fantasynerds.com](https://api.fantasynerds.com/) (getting-started, pricing, and NFL docs pages exist but returned 403 on direct fetch — likely blocking non-browser requests).
- Pricing, via [SportsAPI.com directory listing](https://sportsapi.com/api-directory/fantasynerds/) and search snippets: sold per-sport, per-year. NFL package specifically is **$199.95/year** (NBA/MLB packages are cheaper, ~$74.95/year each — an earlier search conflated these, worth double-checking on the actual pricing page once reachable).
- A free "TEST" API key returns sample/fake data for development without purchase — useful for prototyping the integration before paying.
- Endpoint inventory (via [GregBaugues/fantasy_football_nerd wrapper](https://github.com/GregBaugues/fantasy_football_nerd), a third-party Python client, not the vendor): teams, schedule, players, bye weeks, injuries, auction values, current week, standard/PPR draft rankings, position tiers, draft projections, weekly rankings, weekly projections. Notably, **player info and historical player stats are flagged "commercial access only"** in that wrapper's notes — i.e., may not be included in the standard $199.95/year tier. This needs hands-on confirmation.
- Injuries have their own endpoint per a vendor blog post, ["New Injuries API Available"](https://www.fantasynerds.com/news/article/31/new-injuries-api-available).
- No published hard rate limit; docs instead ask developers to cache and not poll excessively since underlying data updates once or twice daily.
- No redistribution/public-display terms were found in reachable pages.

## SportsDataIO (Discovery Lab) — Findings

- Discovery Lab landing pages: [discoverylab.sportsdata.io](https://discoverylab.sportsdata.io/) and the [NFL personal-use page](https://discoverylab.sportsdata.io/personal-use-apis/nfl) — both are explicitly aimed at students/hobbyists/personal projects, matching the CLAUDE.md brief's target tier. Both returned mostly template/placeholder content on fetch (pricing shown as unrendered template variables), so exact dollar figures could not be confirmed directly from the vendor page.
- Pricing, per third-party listings ([Vendr](https://www.vendr.com/marketplace/sportsdataio), [apis.io](https://plans.apis.io/plans/sportsdataio/sportsdataio-plans-pricing/)): Discovery Lab runs **roughly $99–$149/month**, with a free tier limited to last season's rosters/schedules/results only (no current-season data).
- Data categories confirmed from the [main developer portal](https://sportsdata.io/developers/api-documentation/nfl): Scores/Stats/Plays, Injuries, Lineups & Depth Charts, Teams/Stadiums/Standings (Sports Data); Projections & Points, Salaries & Slates, Fantasy Stats (Fantasy Data); News & Images (player news, previews, recaps). This is a broader, more explicitly organized feed set than Fantasy Nerds' public docs show.
- Discovery Lab specifically provides **next-day data**, not real-time — fine for a weekly report workflow but a real constraint if any same-day injury-news feature is wanted later.
- Terms of service (via search snippet from [sportsdata.io/terms-of-service](https://sportsdata.io/terms-of-service)): explicitly prohibits reselling/redistributing/sub-licensing output "as a competing data product." Discovery Lab is additionally described in third-party summaries as "not licensed for commercial redistribution" — likely fine for a single-user portfolio tool showing derived recommendations (not raw data resale), but worth a direct read of the full ToS before building anything public-facing.

## Recommendation

**Lean toward SportsDataIO's Discovery Lab tier**, provisionally. It's purpose-built for exactly this use case (hobby/personal project, explicitly named tier for students and early-stage ideas), has a clearer and broader documented feed set covering all four v1 needs (stats, projections, news, injuries) under one product, and offers a genuine no-cost tier to prototype against before paying. Fantasy Nerds is cheaper annually and has a nice zero-cost "TEST" key for early integration work, but the signal that historical player stats may be commercial-only, plus the lack of any visible redistribution terms, makes it a riskier pick to commit to without direct account verification. Neither provider has a clearly documented "defensive/matchup difficulty" endpoint — that may need to be derived from raw team stats regardless of provider.

This recommendation is provisional on the verification steps below — don't treat "SportsDataIO" as locked in until at least the free-tier signup and one real API call confirm current-season data access and actual rate limits.

## Open Questions / Needs Hands-On Verification

- **Exact Fantasy Nerds NFL pricing** — the $199.95/year figure came from a search snippet, not a directly fetched pricing page (page 403'd). Confirm on account signup.
- **Whether Fantasy Nerds' historical player stats are gated to a "commercial" tier** above the standard $199.95/year package — this directly affects whether it can support the value engine at all.
- **SportsDataIO Discovery Lab exact price and daily call cap** — vendor page rendered as an unpopulated template; third-party estimates ($99–$149/mo, 100–1,000 calls/day) need confirming via free-tier signup.
- **Whether either provider has a purpose-built "defense vs. position" or matchup-difficulty endpoint**, or whether that needs to be computed in-house from raw team/opponent stats.
- **Full redistribution/attribution terms for both providers** as applied to a small public-facing web app showing derived recommendations (not raw data) — read both full ToS documents directly rather than relying on snippets.
- **Actual response payload shapes** for the key endpoints (projections, injuries, news) — needed before the technical scope note (roadmap step 2) can specify exact fields.
- Neither vendor's docs pages were fully fetchable directly (403s); a real developer-dashboard login will surface the authoritative, current pricing/docs instead of marketing pages.
