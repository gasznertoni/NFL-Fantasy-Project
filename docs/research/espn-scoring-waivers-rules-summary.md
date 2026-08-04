# ESPN Fantasy Football: Scoring & Waivers — General Rules Reference

*Compiled from ESPN Fan Support documentation, 2026-08-04. This is a generic reference covering the full range of options ESPN's platform supports — not one specific league's configuration. Every league's commissioner (League Manager) picks a subset of these settings when building the league, so use this to identify which variant applies once real settings are confirmed.*

## 1. Scoring Formats — the big picture

ESPN fantasy football leagues score games in one of a few formats, chosen by the League Manager at setup:

- **Head-to-Head: Points** — the standard fantasy football format. Each team's roster earns points weekly based on real-player statistical performance, and the team with more points wins that week's matchup.
- **Head-to-Head: Each Category** / **Head-to-Head: Most Categories** and **Rotisserie** / **Season Points** formats also exist on ESPN's platform, but these are primarily used in fantasy baseball leagues rather than football.

For standard head-to-head points leagues, the two scoring-type questions that matter most are (1) Standard vs. PPR, and (2) the exact point values assigned to each statistical category — both covered below.

## 2. Standard vs. PPR (and Half-PPR)

ESPN offers a reception-based toggle that applies league-wide:

- **PPR (Points Per Reception):** every player in the starting lineup earns points for each catch they make. In ESPN's standard PPR setting, **each reception is worth 1 point**. This is an all-or-nothing league setting — it cannot be limited to specific positions (e.g., RB-only or TE-only PPR).
- **Non-PPR / Standard:** no additional points are awarded for receptions. Scoring is based purely on yardage and touchdowns.
- **Half-PPR:** a common middle-ground variant (0.5 points per reception) exists across the fantasy industry and is selectable in ESPN's scoring settings, though ESPN's own support documentation for this comparison focuses primarily on the PPR/Non-PPR distinction.

## 3. Offensive scoring categories (general structure)

Within Head-to-Head Points leagues, ESPN lets the League Manager assign a point value to each offensive statistical category. The categories that are typically configurable include:

- Passing yards (usually points per 25 or per 10 yards)
- Passing touchdowns (commonly 4 or 6 points, League Manager's choice)
- Interceptions thrown (typically a negative point value)
- Rushing yards (usually points per 10 yards)
- Rushing touchdowns
- Receiving yards (usually points per 10 yards)
- Receiving touchdowns
- Receptions (the PPR/Non-PPR/Half-PPR toggle described above)
- Fumbles lost (typically a negative point value)
- Two-point conversions
- Kicker scoring: points awarded generally scale with field goal distance (longer field goals worth more), plus points for extra points/PATs made

Because every one of these values is independently configurable by the League Manager, two ESPN leagues using "standard" settings can still score very differently — the specific point values always need to be confirmed from the real league's own settings page rather than assumed from a generic default.

## 4. Defense/Special Teams (D/ST) scoring

D/ST is scored as a single roster slot representing a team's whole defense/special-teams unit, built from categories including:

- **Points allowed** and **yards allowed**, each scored in tiers/bands (fewer points or yards allowed earns more points; ESPN's default standard scoring starts a D/ST at 10 points — 5 for allowing zero points and 5 for allowing zero offensive yards — before tier adjustments apply).
- **Sacks, interceptions, and fumble recoveries** by the defense.
- **Defensive/special-teams touchdowns** (e.g., a pick-six, a blocked-kick return, or a punt/kickoff return TD).
- **Safeties** and **blocked kicks**.
- **Return yardage** (punt and kickoff returns).

A few notable rule details from ESPN's documentation:

- Since the 2019 season, points from a defensive touchdown (e.g., a pick-six or a fumble-recovery TD) do **not** count toward the D/ST score — those are scored as part of the drive/offense stat lines instead. Special-teams touchdowns (like a punt-return TD) still do count toward D/ST.
- On a point-after-touchdown (PAT) play: a **safety recorded during the PAT** is worth 1 point; the ball being **returned to the defense's own end zone during a PAT attempt** is worth 2 points.
- A "stuff" (as clarified in a November 2021 update) is credited only on rush plays that are stopped for no gain or a loss — distinct from a tackle-for-loss, which can happen on either a run or a pass play.

As with offensive scoring, the exact point values per category (e.g., points per sack, per interception, per points-allowed tier) are League Manager–configurable and can vary significantly between leagues, including highly granular custom tiers.

## 5. Bonus Wins and Losses (League Manager leagues only)

An optional setting, available only in League Manager (self-managed/private) leagues, that adds extra win/loss credit on top of normal head-to-head results:

- **Bonus win:** if a team's weekly score ranks in the **top half** of the league for that week, it earns an additional win, regardless of its head-to-head matchup outcome.
- **Bonus loss:** if a team's weekly score ranks in the **bottom half** of the league for that week, it earns an additional loss.
- **Purpose:** rewards teams that consistently score well but happen to face other high-scoring opponents, so strong scoring performance isn't only rewarded/punished by the luck of the weekly matchup schedule.
- **How to enable:** LM Tools → League Settings → Regular Season Setup → Edit → check "Bonus Wins and Losses" → Save. Off by default; must be explicitly turned on by the commissioner.

## 6. Waivers — how the system works

When a player is dropped or goes unrostered (e.g., immediately following the draft), they don't become instantly available — they first pass through a **waiver period**, a holding window during which any team can submit a claim rather than just grabbing the player first-come-first-served.

**Waiver period / timing:**

- League Managers configure the waiver period length: **No Waivers** (player is a free agent immediately), **1 Day** (the default — most players clear by Wednesday morning if dropped after Monday/Tuesday games), or **2 Days**.
- Claims are processed in a daily batch, generally **between 3:00–5:00 AM ET**.
- Processing requires at least a full 24 hours to have passed since the player was dropped/became available.
- A player added and dropped again on the **same day** stays a free agent rather than re-entering the waiver queue — this prevents gaming the system.

**Waiver acquisition types (League Manager's choice of one per league):**

- **Standard (priority-order) waivers:** teams are ranked in a **Waiver Order** (a ranked list). When multiple teams claim the same available player, the team highest in the order wins the claim, then moves to the **bottom** of the order (a rolling/continual list). Any players left unclaimed after the waiver period become ordinary first-come, first-served free agents.
- **FAAB / Free Agent Budget (bidding):** each team has a set virtual budget for the season and places blind bids on available players; the highest bidder wins the claim, and the winning bid is deducted from that team's remaining budget. Comes in two flavors: standard FAB (unclaimed players fall through to first-come free agency after the waiver period, same as standard waivers) and Continuous FAB (there is no free-agency period at all — every unrostered player must always go through a waiver bid, with no first-come pickups).
- **No Waivers:** every dropped or unrostered player is immediately available as a first-come, first-served free agent, with no holding period at all.

**Waiver order mechanics:**

- The order (for both standard and FAAB tiebreakers) initially seeds as the **inverse of the draft order** — the manager who picked first in the draft starts last in waiver priority, and vice versa.
- Two reset styles exist: a **rolling list** (a team that wins a claim drops to the bottom of the order, everyone else moves up) or a **weekly reset to inverse standings** (the order resets every Monday around 12:00 AM PT / 3:00 AM ET, giving worse-record teams higher priority for that week).
- In standard (priority-order) leagues, the League Manager can manually edit the waiver order at any time. In salary-cap/FAAB leagues, the **tiebreaker order** (used when two teams submit identical bids) cannot be manually edited by the commissioner once waivers begin processing.

**Submitting a claim:**

- Managers submit waiver claims through the ESPN app or website, selecting the player to add and (in most cases) which rostered player to drop in exchange, since claims typically require a corresponding roster move to stay within roster limits.
- Claims can generally be edited or canceled any time before the waiver period processes.
- Multiple claims can be queued in priority order by a single team in case an earlier-priority claim fails (e.g., the targeted player was already claimed by a higher-priority team).

## 7. Notes on using this reference

Every point value, tier boundary, and process type above is a *setting*, not a fixed rule — ESPN's platform exposes many independently configurable options in both scoring and waivers, and different leagues legitimately land on very different combinations. To determine which options actually apply to a specific league, check that league's own **LM Tools → League Settings** pages (Scoring Settings and Transactions/Waivers sections) rather than assuming any of the defaults described here.

---

### Sources

- [Scoring Formats](https://support.espn.com/hc/en-us/articles/360003914032-Scoring-Formats) — general navigation page; football-specific scoring category detail not present in the article body at time of review.
- [Defense and Special Teams (D/ST) Scoring](https://support.espn.com/hc/en-us/articles/115003847231-Defense-and-Special-Teams-D-ST-Scoring)
- [League Scoring Types: Standard and Non-PPR](https://support.espn.com/hc/en-us/articles/360031085331-League-Scoring-Types-Standard-and-Non-PPR)
- [Bonus Wins and Losses (LM Leagues Only)](https://support.espn.com/hc/en-us/articles/7669711607828-Bonus-Wins-and-Losses-LM-Leagues-Only)
- [Scoring Formats (fantasy baseball)](https://support.espn.com/hc/en-us/articles/360003913972-Scoring-Formats) — this specific article ID currently documents fantasy *baseball* scoring formats (Rotisserie, Head-to-Head Points/Category, Season Points), not football; included per the source list but noted as off-topic for football scoring.
- [Claim a Player Off Waivers](https://support.espn.com/hc/en-us/articles/360000036711-Claim-a-Player-Off-Waivers) — this article returned an access error during research; waiver-claim process details above are sourced from the related articles below instead.
- [Waivers Overview](https://support.espn.com/hc/en-us/articles/360000041152-Waivers-Overview)
- [Waiver Order Overview and Salary Cap Tiebreakers](https://support.espn.com/hc/en-us/articles/360000093771-Waiver-Order-Overview-and-Salary-Cap-Tiebreakers)
- [Waiver Period](https://support.espn.com/hc/en-us/articles/360012531592-Waiver-Period)
- [Change Acquisition and Waiver Settings (LM Only)](https://support.espn.com/hc/en-us/articles/360000097492-Change-Acquisition-and-Waiver-Settings-LM-Only)
