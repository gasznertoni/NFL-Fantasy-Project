# To-Do: Get FantasyPros + API-Sports Keys

These two sources need a signup that only you can complete (tied to your email). Once you have both keys, `scripts/probe_sources.py` will pick them up automatically.

## 1. FantasyPros API key

1. Go to `secure.fantasypros.com/api-keys/request` and submit the free-key request form (personal/non-commercial/prototype use — this project qualifies).
2. You should receive a key by email, or it may show directly after submitting — FantasyPros' own docs page (`fantasypros.com/api-data/`) 404'd when I tried to fetch it directly, so check the current form/docs location live rather than trusting this exact URL blindly.
3. **The one thing to verify the moment you have the key** (this is the open question flagged in `free-data-sources.md`): hit the `projections` endpoint, not just `rankings` — confirm the free tier actually returns projected fantasy points, not just Expert Consensus Rank. The probe script's `probe_fantasypros()` checks this automatically (`HAS_PROJECTIONS_IN_FREE_TIER` in the output JSON).
4. Add to a `.env` file in the repo root (create it, don't commit it — add `.env` to `.gitignore` if it isn't already):
   ```
   FANTASYPROS_API_KEY=your_key_here
   ```

## 2. API-Sports (American Football) key

1. Go to `api-sports.io`, sign up for a free account (no card required per their published free tier).
2. In your API-Sports dashboard, find the American Football API and grab your key — API-Sports uses one key across all their sport APIs, gated per-sport by subscription; make sure American Football is enabled on the free plan.
3. Confirm the daily cap live in your dashboard — `free-data-sources.md` cites 100 req/day, but that came from search snippets, not a verified account, so treat it as provisional until you see your own dashboard.
4. Add to the same `.env` file:
   ```
   API_SPORTS_KEY=your_key_here
   ```

## After you have both

```bash
cd ~/Desktop/NFL-Fantasy-Project
python3 scripts/probe_sources.py
```

The script skips any source cleanly if its key isn't set yet — so you can run it now for the other 3 sources and re-run once you've got both keys, no need to wait for both before running once.

## Quota math, once you have real numbers

The test plan flags this explicitly: don't assume "100 requests/day" is obviously enough without doing the multiplication for your actual use case. Once you know your real roster size and how many waiver candidates you check per week, revisit `docs/research/scope-note-draft.md`'s quota section — it has the formula and a worked placeholder example ready to update with your real numbers.
