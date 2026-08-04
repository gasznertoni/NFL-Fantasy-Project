# NFL Fantasy Value Assistant — Frontend

React + Vite single-page app implementing the user journey/IA in
[`docs/specs/user-journey-frontend.md`](../docs/specs/user-journey-frontend.md):
a **Weekly Report** view (start/sit + waiver targets) and a **Track Record**
view (recommendation accuracy vs. real outcomes).

This is a frontend-only build against mock data. There is no backend yet —
the real value engine, real league scoring rules, and real roster are still
pending (see the root `CLAUDE.md` Next Steps). All data comes from static
JSON fixtures in `public/mock/`, read through `src/lib/api.js`
(`getWeeklyReport`, `getTrackRecord`, `getRosterSlots`), so swapping in a
real API later is a one-file change with no component rework.

## Run locally

```bash
npm install
npm run dev       # dev server
npm run build     # static bundle -> dist/
npm run preview   # serve the built bundle
```

## Notes

- Roster slots are config-driven (`public/mock/roster-slots.json`), not
  hardcoded — the real league roster isn't confirmed yet.
- Every projected-points number is paired with a tier badge
  ("Consensus projection" vs. "Our estimate") — the two projection tiers are
  never presented as the same kind of number, per the project's scoring
  philosophy.
- The league scoring format shown (`half_ppr`) is a labeled placeholder
  assumption, not a confirmed league setting.
