# Implementation plans

Active plans and the order to implement them in. A plan that is fully implemented and merged is
deleted (anything spec-worthy moves into `specs/` first) and its row here goes with it.

## Order

| # | Plan | Blocked by | Notes |
|---|---|---|---|
| 1 | `pwa-analytics-refresh.md` | — | The actual defect: the stats page shows pre-receipt numbers. Unblocks #2 |
| 2 | `pwa-analytics-event-detail.md` | #1 | Shares five files with #1; back-to-back is one context load |
| 3 | `llm-view-status-details.md` | — | Shares no files with #1 or #2 — can be pulled forward or run in parallel |
| — | `analytics-llm-secrets.md` | — | Problem statement only, no solution chosen. Not scheduled |

## Why this order

**#1 before #2 is a hard constraint.** They overlap in `src/dinary/api/analytics.py`,
`stores/analytics.js`, `views/AnalyticsView.vue`, `webapp/tests/store-analytics.test.js` and
`specs/reference/pwa-analytics.md`. #1 rewrites the analytics store onto `useStaleCache` and
renames `fetchAll` → `loadIfNeeded`, so building #2's detail map against the current store shape
means writing it twice. #1 also supplies the dirty flag that #2's cache-clearing rule leans on —
shipped alone, the drill-down follows the 24 h TTL and can display exactly the stale numbers #1
exists to fix.

**#3 floats.** It touches `src/dinary/api/controllers/llm.py`, `views/LLMView.vue`,
`components/ProviderCard.vue` and one new composable — no overlap with #1 or #2 in code, tests or
specs (they change `src/dinary/api/analytics.py`, it changes the LLM controller). It is the
smallest of the three and the natural candidate to hand to a second person or to land first as a
warm-up.

One shared file to watch: `specs/reference/pwa-analytics.md` is edited by both #1 (the
"Client cache" section) and #2 (the event-detail endpoint). Different sections, but separate
branches will conflict there.
