# NFL Fantasy Value Assistant — Project Brief (v1)

This file is the standing reference for any agent (Claude Code or otherwise) working in this repo. Read it before making architectural or scope decisions.

## Context / Purpose

Portfolio project for an active job search (targeting Senior Product Owner / AI Product Manager roles). The goal is to demonstrate hands-on AI + data-driven product building — not just PM coordination. It will be built with Claude Code, deployed live, documented as a case study, and added to the builder's CV/LinkedIn afterward.

**Builder:** Ágoston Gászner, Senior Product Owner (Applied AI), Budapest. Background in mortgage/lending AI products (LLM document extraction, rule validation, evaluation frameworks) at Intuitech. Plays NFL fantasy on the official NFL Fantasy App.

## Problem

Manually assessing weekly NFL fantasy lineup decisions (start/sit, waiver pickups) requires synthesizing player stats, matchup context, and breaking injury/news — time-consuming to do well every week.

## v1 Scope (2-week build target)

**In scope:**

1. **Player value engine** — projected fantasy points using structured stats: player performance history, matchup/defensive difficulty, existing projections from a data provider.
2. **News & injury layer** — pull player news/injury feed from the same data provider's API; use an LLM to summarize what's relevant and flag risk level (e.g., "questionable, limited practice reps") rather than scraping raw news sources from scratch.
3. **Daily/weekly report output** — start/sit recommendations and top waiver-wire targets, combining the value engine + news layer.
4. **Eval layer (differentiator)** — track the tool's own recommendation accuracy against actual results over the season; a simple accuracy/track record view, ideally public. This is the "AI evaluation framework" instinct from the builder's day job, made into a visible artifact.

**Explicitly out of scope for v1** (documented as v2 roadmap):

- Trade suggestions (requires full league-state / roster modeling across teams)
- "Fan interaction" sentiment signals (hard to source cleanly, low signal-to-noise)
- Direct integration with the official NFL Fantasy App API (auth/approval process unconfirmed — treat as stretch goal, not a v1 dependency)
- Multi-league support, user accounts/auth

## Data Sources (candidates to evaluate first)

- **Fantasy Nerds API** — offers player/team news, injury data, projections, defensive rankings, tiers; appears to have a paid tier but accessible for personal projects.
- **SportsDataIO NFL API** — scores, odds, projections, stats, news; has a "Discovery Lab" tier aimed at students/hobbyists/personal projects.

Both should be compared for: news/injury data quality, projection data included, pricing at hobby scale, and rate limits.

**Note:** these were surfaced via a quick search and have not yet been evaluated hands-on — the first build step should be checking actual API docs/pricing before committing to one.

## Suggested Architecture

- **Frontend:** simple React app (single view: weekly report + eval/track-record view)
- **Backend:** lightweight Node/Python service — fetches from the data provider API, runs LLM summarization on news/injury data, computes value engine output
- **LLM:** Claude API for news summarization/risk flagging and reasoning
- **Deployment:** Vercel or similar, so it's live and shareable with a link

## Success Criteria for v1

- Tool produces a real, usable weekly report during an actual NFL fantasy week
- Recommendations are tracked against real outcomes (even informally) to seed the eval/track-record layer
- Deployed live with a shareable link
- Documented as a short case study: problem → approach → what was built → what was measured → what's next (v2 roadmap)

## Next Steps (in order)

1. Compare Fantasy Nerds vs. SportsDataIO API docs/pricing/data quality; pick one.
2. Write a one-page technical scope note once the data source is confirmed (exact endpoints, data fields available).
3. Build core value engine using structured stats first.
4. Add news/injury summarization layer.
5. Build the report output view.
6. Add the eval/track-record view.
7. Deploy, use for a real fantasy week, document as a case study.

## Notes for agents

- This is a portfolio/case-study project — code quality, clear commit history, and a clean architecture matter as much as functionality, since the build itself may be referenced in interviews.
- Prefer simplicity over premature generalization: this is a single-user, single-league tool for v1. Don't build multi-tenant or auth scaffolding unless explicitly asked.
- Keep the eval/track-record layer in mind from the start — recommendations should be logged in a way that makes later accuracy scoring straightforward (e.g., store the prediction, the context it was made with, and a way to attach the actual outcome once known).
- When picking a data provider, verify pricing and rate limits hands-on before committing — the brief flags this as unverified.
