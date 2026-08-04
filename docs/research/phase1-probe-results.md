# Phase 1 — Hands-On Shape Probe: Results

Date: 2026-08-04. Executed from the Cowork cloud sandbox against `docs/research/data-source-test-plan.md`'s Phase 1.

## Important caveat: sandbox network limits hit mid-probe

This session's cloud sandbox has a restricted network egress allowlist. `pip install` failed for every package (pypi.org / files.pythonhosted.org returned `host_not_allowed`), and GitHub's own `robots.txt` blocks the `/releases/download/` path that `nflreadpy` actually pulls its data files from — confirmed by fetching `github.com/robots.txt` directly. A jsDelivr mirror attempt also 404'd. This is an infrastructure constraint of *this sandbox*, not a problem with `nflreadpy` itself or your local machine, which has normal internet access.

Net effect: 2 of 5 sources got a real, hands-on, live-data check today. The other 3 (nflreadpy fully, DynastyProcess's actual rankings/projections file, FantasyPros/API-Sports pending your keys) need to be run from your machine — see `scripts/probe_sources.py` and the instructions below. This doc records what's confirmed vs. still pending.

---

## 1. Sleeper API — ✅ confirmed live, hands-on

**`GET /v1/state/nfl`** — real response, unauthenticated, no key:
```json
{"week":0,"leg":0,"season":"2026","season_type":"pre","league_season":"2026",
 "previous_season":"2025","season_start_date":"2026-08-06","display_week":1,
 "league_create_season":"2026","season_has_scores":true}
```
Confirms the API is live and correctly reflects preseason state consistent with the plan's "today is preseason" framing.

**`GET /v1/players/nfl/trending/add?lookback_hours=48&limit=10`** — real response:
```json
[{"count":36996,"player_id":"13413"},{"count":22878,"player_id":"13392"}, ...]
```
**Finding:** trending returns bare `player_id` + add-count only — **no name, position, or team**. To make this endpoint useful you must cross-reference against the full player dump (`GET /v1/players/nfl`, ~5MB, single blob, no delta/pagination). Sleeper's own docs recommend caching that dump and refreshing it ~once/day rather than calling it per-report. This is a real operational note for Phase 6 (cache strategy), not just a field-shape note.

**Not yet pulled:** the full `/v1/players/nfl` dump itself — 5MB is too large to reliably extract exact/complete field names through this session's fetch tool without risking summarization errors. Included in the local script below; safe/fast to run from your machine (`curl` it directly).

## 2. DynastyProcess — ⚠️ partially confirmed hands-on

**`db_playerids.csv`** (`raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv`) — ✅ confirmed live, real header + rows:
```
mfl_id,sportradar_id,fantasypros_id,gsis_id,pff_id,sleeper_id,nfl_id,espn_id,yahoo_id,
fleaflicker_id,cbs_id,pfr_id,cfbref_id,rotowire_id,rotoworld_id,ktc_id,stats_id,
stats_global_id,fantasy_data_id,swish_id,name,merge_name,position,team,birthdate,age,
draft_year,draft_round,draft_pick,draft_ovr,twitter_username,height,weight,college,db_season
```

**Rookie edge case check (Phase 1's suggested minimum):** Fernando Mendoza (2026 draft class QB, Indiana → LVR) **is present** in the crosswalk, with `sleeper_id`, `espn_id`, `pfr_id`, `cfbref_id` populated — but **`fantasypros_id` = `NA`**.

**Finding — this matters for the spec:** rookie coverage in the ID crosswalk exists day-one (good), but the `fantasypros_id` column specifically lags for newly-drafted players. If the value engine joins FantasyPros projections to players by `fantasypros_id`, rookies will silently drop out of that join until FantasyPros assigns them an ID. Worth an explicit fallback join key (e.g. `sleeper_id` or `gsis_id`) for rookies in the engine, not just a documented gap.

**`db_fpecr.csv.gz`** (the actual rankings/projections file — the one that matters most for Phase 0's "confirmed available" projections field) — ❌ **not retrieved this session.** The repo's own README states the file is gzip-compressed specifically "due to GitHub size restrictions," and it exceeded this session's 20MB fetch cap; the `.parquet` alternative is binary and unreadable by the tool available here. **This is the single open item that matters most** — need to confirm hands-on whether real weekly/ROS *projections* (not just ECR rank) are actually in this file, per the plan's own flagged unknown. Script included below — this will take seconds on your machine.

## 3. nflreadpy — ❌ blocked entirely in this sandbox

- `pip install nflreadpy` fails: pypi.org unreachable from this session (`host_not_allowed`).
- Its underlying data (what `load_player_stats()` / `load_injuries()` / `load_rosters()` actually fetch) lives in `nflverse/nflverse-data` GitHub Release assets, e.g. confirmed via GitHub's API to exist and be current: `player_stats_2025.csv`, `roster_weekly_2025.csv`, and an `injuries` release with 75 assets. But the download path (`github.com/.../releases/download/...`) is blocked by GitHub's own `robots.txt`, which this session's fetch tool respects — confirmed by reading `robots.txt` directly.
- **Result: zero hands-on data for nflreadpy from this session.** Everything about it in `free-data-sources.md` remains "per docs," not "per hands-on check," until you run the script below.

## 4. FantasyPros API / 5. API-Sports — not probed (need your keys)

Per your call: you're getting these keys yourself. See `docs/research/api-key-setup-todo.md` for exact steps, and `scripts/probe_sources.py` has the calls ready to go once you have keys — just drop them in a `.env` file.

---

## What to run on your machine

```bash
cd ~/Desktop/NFL-Fantasy-Project
pip install nflreadpy python-dotenv requests pandas pyarrow
python3 scripts/probe_sources.py
```

(`pyarrow` wasn't in the original instructions — nflreadpy needs it to read the parquet files it downloads. Add it if you hit `No module named 'pyarrow'`.)

This prints field names, row counts, the rookie/practice-squad/recently-traded checks, and a last-updated marker for each source, and writes `docs/research/phase1-local-results.json`. Once you've run it (with or without the FantasyPros/API-Sports keys — the script skips those cleanly if the keys aren't set), share the JSON back and I'll fold it into the coverage scorecard, cross-validation, and scope note, which right now are drafted with placeholders for exactly this data.

---

## Update — 2026-08-04, first local run

Ran from the user's machine. 2 of the 3 no-auth sources came back clean; nflreadpy failed on a missing dependency (`pyarrow` — now added to the install command above) and is still pending a re-run.

### DynastyProcess `db_fpecr.csv.gz` — ✅ now confirmed, and it changes the plan

Real pull: **1,528,918 rows** (this is the full historical archive across seasons/weeks/positions, not just current data — needs filtering by `scrape_date`/season before use). Real columns:
```
fp_page, page_type, player, id, pos, team, ecr, sd, best, worst, mergename, tm,
sportsdata_id, player_filename, yahoo_id, cbs_id, player_owned_avg, player_owned_espn,
player_owned_yahoo, player_image_url, player_square_image_url, rank_delta, ecr_type, scrape_date
```

**Finding — resolves the plan's biggest open question, in the negative:** there are **no projection/points columns** (`ecr`, `sd`, `best`, `worst`, `rank_delta` are all rank statistics, not fantasy points). `free-data-sources.md`'s claim that DynastyProcess gives "rankings + projections" conflated two different things: this static file is ECR/rank only. The actual `fp_projections()` projections data that doc referenced comes from the `ffpros` **R package's live scrape** of FantasyPros' site, not from a file in this repo — meaning it's not reachable from a Python-only stack without either replicating that scrape or using FantasyPros' own API.

**Practical effect:** the "$0, fully free stack covers projections" recommendation in `free-data-sources.md` needs revising. For real weekly point projections (not just consensus rank), FantasyPros' own free-tier API is no longer just "one step cleaner" — it looks like the only free, Python-reachable source of actual projections among these 5. This makes getting that key (see `api-key-setup-todo.md`) higher-priority than it looked initially, specifically to confirm the free tier includes `projections`, not just `rankings`.

### Sleeper full player dump — ✅ confirmed, richer than expected

Real pull: **12,207 players**, with a much fuller injury-relevant schema than `free-data-sources.md` assumed ("partial — player status flags"). Actual fields include: `injury_status`, `injury_body_part`, `injury_start_date`, `injury_notes`, `practice_participation`, `practice_description`. That's designation *and* practice-participation *and* free-text notes — all three things the plan was hoping to find in nflreadpy alone.

**Finding:** Sleeper should move from "confirmation/cross-check" to a genuine **primary or co-primary** candidate for the injury/practice-status data point, not just a backup — pending confirming nflreadpy's actual injury table once the pyarrow fix lets it run, for the Phase 5 side-by-side comparison the plan calls for.

**Rookie edge case:** found (437 players with `years_exp: 0`). Sample: Kinkead Dent, an inactive/unsigned rookie QB — most cross-reference IDs (`espn_id`, `gsis_id`, `yahoo_id`) are null, only `sportradar_id` is populated. Confirms the "IDs are sparse for edge-case players" pattern already seen in DynastyProcess's crosswalk.

**Practice-squad edge case:** only 1 player found with `status == "Practice Squad"` out of 12,207. This is very likely a **timing artifact, not a data quality issue** — it's preseason, rosters are still at the bloated ~90-man offseason size, and practice squads don't really form until after the 53-man cuts in late August (per the plan's own Phase 4 framing). Worth re-checking this specific field after roster cuts rather than reading anything into it now.

### nflreadpy — ✅ confirmed after the pyarrow fix, second local run

`pip install pyarrow` fixed it. Real pull, 2025 season:

- **`load_player_stats()`: 19,421 rows, weeks 1–22** (regular season + playoffs). ~140 columns — full passing/rushing/receiving lines, advanced metrics (`passing_epa`, `target_share`, `air_yards_share`, `wopr`, `racr`), defense, special teams, kicking, punting. **Big bonus finding: it includes `fantasy_points` and `fantasy_points_ppr` directly** — real historical fantasy output already computed, by scoring format, per game. Sample row checked: Bijan Robinson, week 17 2025, 22 carries/195 rush yds/1 TD, 5 rec/34 yds/1 TD → 34.9 standard / 39.9 PPR points. This is a strong free source of ground-truth outcomes for the eval/backtest layer, not just an input to the value engine.
- **`load_injuries()`: 6,068 rows.** Columns: `report_status`, `report_primary_injury`, `report_secondary_injury`, `practice_status`, `practice_primary_injury`, `practice_secondary_injury`. **`has_practice_participation_field: true`** — confirmed, nflreadpy has practice-participation detail too, not just Sleeper. This resolves the plan's originally-flagged unknown from *both* sides.
- **`load_rosters()`: 3,137 rows**, with `status` values: `ACT` 1537, `DEV` 484, `CUT` 444, `RES` 435, `INA` 210, `RET` 23, `TRD` 3, `TRC` 1. **This directly answers the earlier practice-squad question**: nflreadpy's `DEV` (developmental squad) status has 484 real players across the 2025 season — the "only 1 practice-squad player" result from Sleeper wasn't a data-quality problem, it was confirmed to be a timing artifact of pulling Sleeper's *live 2026 preseason* snapshot before squads had formed, compared against nflreadpy's *completed 2025 season* data. Not a fully apples-to-apples comparison yet — see the updated cross-validation notes.
- **Also notable:** the rosters table already carries `espn_id`, `sportradar_id`, `yahoo_id`, `rotowire_id`, `pff_id`, `pfr_id`, `fantasy_data_id`, `sleeper_id` directly — meaning for many joins you may not even need DynastyProcess's separate crosswalk; DynastyProcess remains useful for the IDs nflreadpy doesn't carry (`mfl_id`, `cfbref_id`, `ktc_id`) and for pre-season/rookie coverage before nflreadpy's own season data exists.

This closes out the last major open item from the original 5-source Phase 1 list except the two key-gated sources.

## 4. FantasyPros API — ✅ confirmed, resolves the plan's central question — with a real catch

Once keyed, the `projections` endpoint returned real per-player projected stats, not just rank. Sample (Jalen Hurts, week 1 2025 request):
```json
"stats": {
  "points": 23.1, "points_ppr": 23.1, "points_half": 23.1,
  "pass_att": 27.61, "pass_cmp": 18.97, "pass_yds": 218.44, "pass_tds": 1.57,
  "pass_ints": 0.45, "rush_att": 8.87, "rush_yds": 40.93, "rush_tds": 0.83, "fumbles": 0.28
}
```
**This is the finding that settles the plan's biggest open question, in the positive:** the free tier does include real weekly fantasy point projections (`points`/`points_ppr`/`points_half`), plus the underlying stat-line projections that produce them. My original automated check (`HAS_PROJECTIONS_IN_FREE_TIER`) reported `false` — that was a bug in the script, not a real finding; it only checked top-level player field names and never looked inside the nested `stats` object where the actual numbers live. Fixed in `scripts/probe_sources.py`.

**The catch — a real operational constraint, not a bug:** the response's top-level metadata is
```json
{"season": "2025", "week": "1", "count": "915", "positions": "QB,RB,WR,TE,K,DST",
 "scoring": "STD", "limit": 10, "public_api_limited": true, "tier": "free"}
```
`count: 915` is how many players exist for that query; `limit: 10` is how many the free tier actually returns. **The free tier caps every response at 10 players.** That's fine for "who are the top 10 overall projected players" but likely not enough to cover a full ~16-player roster plus waiver-wire candidates in one call — and it's not yet confirmed whether you can target *specific* players (e.g., your own bench guy who isn't top-10 overall) rather than just getting whatever the API's default sort returns. Worth checking directly in your FantasyPros dashboard whether there's a pagination/offset parameter or a way to query by player ID, before assuming this fully covers the value engine's needs.

**One more thing to verify later, not blocking:** the response's `scoring` field came back `"STD"` even though the request asked for `scoring=PPR`. For Jalen Hurts (a QB with no receptions) `points` and `points_ppr` happened to match, which doesn't prove the scoring parameter works — worth re-checking against a pass-catcher (a WR/RB) where standard vs. PPR scoring should clearly diverge, to confirm the free tier actually honors the scoring format you request.

## Update — 2026-08-04, follow-up: scoring param + per-player querying, both settled

Two follow-up probes against a real pass-catcher (Travis Kelce, TE) to close the two open items above.

### Scoring parameter — confirmed ignored, but the data itself is fine

Requested `scoring=STD` and `scoring=PPR` separately for the TE position/week 1: both calls echoed `"scoring": "STD"` in the response metadata regardless of what was requested, and returned the identical top-10 TE list in the identical order. **The `scoring` query parameter has no effect on the free tier.** That's fine in practice, though, because every response already includes all three formats per player — Kelce's real `stats` object: `points: 7.3` (STD), `points_ppr: 12.72`, `points_half: 10.01`. The 5.42-point gap between STD and PPR matches his `rec_rec: 5.42` (projected receptions) exactly — internally consistent, real data. **Practical takeaway: don't bother sending `scoring` at all; just read whichever field (`points`/`points_ppr`/`points_half`) matches your league's format from the response you already have.**

### Per-player querying — confirmed NOT supported on the free tier

Tried six different ways to get past the top-10-per-position cap, all against the same TE/week-1 query with Kelce's real `fpid` (11594): `player_id=11594`, `id=11594`, `fpid=11594`, `limit=50`, `offset=10`, `page=2`. **Every single one returned the exact same 10 names in the exact same order**: Brock Bowers, George Kittle, Trey McBride, David Njoku, Travis Kelce, Mark Andrews, Sam LaPorta, T.J. Hockenson, Tyler Warren, Dallas Goedert — with `count: 177` (real total TEs available) and `limit: 10` unchanged every time. None of the parameters tried had any effect.

**This resolves the open design question from the scope note, in the negative:** the free tier's top-10-per-position cap has no documented or discoverable override. It's a hard ceiling, not a pagination limit you can work around with request parameters. (Also worth noting operationally: rapid successive calls tripped a `429 Too Many Requests` once during this testing — the free tier has request-rate limiting on top of the response-size cap, so build in a small delay between calls in the real pipeline.)

**What this means for the build:** FantasyPros' free tier is reliable for the consensus top-10-per-position players — which will often cover your starting lineup — but will silently return nothing for any rostered or waiver-wire player who falls outside their position's top 10. For a bench player, a committee-backfield RB2, or most waiver-wire targets, this source has no answer. See the scope note for how this changes the recommended data architecture.
