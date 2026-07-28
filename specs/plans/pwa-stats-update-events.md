# Plan: PWA stats refresh and per-event drill-down

## Task

1. The PWA stats page must refresh when expenses/incomes are added and when a receipt is
   scanned. Caching logic mirrors the review page: data counts as dirty until the server has
   fully processed all receipts.
2. Clicking an event shows that event's stats: how much was spent per category, plus the
   spend total for each of the last few days.

## Context (current state)

Stats page = `webapp/src/views/AnalyticsView.vue` + store `webapp/src/stores/analytics.js` +
endpoint `GET /api/analytics/summary` (`src/dinary/api/analytics.py`).

The analytics cache currently works differently from review's:
- `analytics.js` — 24h TTL only (`lastFetched`), no dirty flag.
- Invalidation is triggered by the income store alone (`invalidate()` on add/patch/remove).
  Adding an expense and scanning a receipt do not reset analytics.
- The review store uses the shared `useStaleCache` composable with a dirty flag and the rule
  "stay dirty while the server receipt queue is non-empty" (`review.loadNextPage` re-marks
  itself and the `llm` store until `receipts_queue` drops to zero). That behaviour is what
  analytics needs.

Events already arrive in `summary.events` as
`{ id, name, date_range, total, currency, open }` and render as rows in `AnalyticsView`.
They have no click/expand.

DB schema: `events(id,name,date_from,date_to,…)`,
`expenses(amount, datetime, category_id, event_id,…)`,
`categories(id,name,group_id)`, `category_groups(id,name)`.

## Part 1. Cache invalidation like review's (dirty-until-processed)

### 1.1 Move `stores/analytics.js` onto `useStaleCache`
- Wire up `useStaleCache({ dirtyKey: "dinary:analytics:dirty",
  fetchedKey: "dinary:analytics:fetchedAt", dataKey: "dinary:analytics:v1" })`.
- Store `summary/events/trends` through `readCache`/`writeCache` (one object).
- Replace the TTL check in `fetchAll` with `isStale()`; after a successful fetch —
  `stampFresh()` + `writeCache(...)`.
- Rename the method to `loadIfNeeded()` (consistent with review/income); export `markDirty`.
- Drop `invalidate()`; use `markDirty()` instead.

### 1.2 Mark dirty at every point that changes expenses/incomes/receipts

| Trigger | File | Action |
|---|---|---|
| New expense sent to the server | `composables/flushQueue.js` | on `anyFlushed` → `useAnalyticsStore().markDirty()` |
| Receipt scanned (not a duplicate) | `composables/flushReceiptQueue.js` | next to `useLlmStore().markDirty()` / `useReviewStore().markDirty()` → `useAnalyticsStore().markDirty()` |
| Server receipt queue non-empty | `stores/review.js` `loadNextPage` | inside the `if (q.pending>0 …)` block add `useAnalyticsStore().markDirty()` — this is what "dirty until all receipts are processed" means |
| Category/expense edit, delete, rule confirmation, stuck-receipt resolution, receipt delete | `stores/review.js` (`correct`, `updateExpense`, `deleteExpense`, `confirmAll`, `resolveStuckReceipt`, `deleteReceipt`) | `useAnalyticsStore().markDirty()` |
| Income add/patch/remove | `stores/income.js` | replace 3× `useAnalyticsStore().invalidate()` → `markDirty()` |

### 1.3 Showing fresh data
- `AnalyticsView.onMounted` → `store.loadIfNeeded()` (fetches only when `isStale()`), guarded
  by `isOnline`.
- No background probes in `App.vue` (visibility/online/init) for analytics — it has no badge
  and loads when the tab is opened; the tab-level `loadIfNeeded()` on stale fully covers the
  requirement (deliberate scope narrowing).

Result: after an expense/income is added or a receipt is scanned the store is marked dirty;
while the server finishes processing receipts it is re-marked via `review.loadNextPage`; the
first stats-page open after processing does a fresh fetch and clears the flag — exactly the
review semantics.

## Part 2. Per-event drill-down (on click)

### 2.1 Backend: `GET /api/analytics/events/{event_id}`
New route in `src/dinary/api/analytics.py`, 404 when the event does not exist. Response:

```
{ id, name, date_range, total, currency, open,
  categories: [{ category_id, category_name, group_name, total, currency }],  # sort desc
  days:       [{ date, date_label, total, currency }] }                       # last N days with spend
```

Two new SQL files in `src/dinary/db/sql/`:
- `analytics_event_categories.sql` — `expenses JOIN categories JOIN category_groups
  WHERE event_id=? GROUP BY category ORDER BY SUM(amount) DESC`.
- `analytics_event_days.sql` — `WHERE event_id=? GROUP BY date(datetime) ORDER BY day DESC
  LIMIT 7` (the event's last few days, most recent first).

Reuse `_fmt` and `settings.accounting_currency`; for `date_label` use a short day format
(e.g. "14 Jul").

### 2.2 Frontend
- `api/analytics.js` → `fetchEventDetail(eventId)`.
- `stores/analytics.js` → `eventDetails` (in-memory map by id), `loadEventDetail(id)`
  (fetch + cache), reset `eventDetails` on `reset()`/a new fetch (so the detail obeys the
  dirty flag too).
- `AnalyticsView.vue` — make the event row expandable (accordion).
  Important: use a separate `expandedEventId` state for expansion — the `ev.open` field is
  already taken (it means "the event is still running") and must not be conflated. On expand:
  fetch the detail (skeleton while loading), then two blocks styled like the current cards:
  - By category — "category · amount" rows with a proportional bar (inline CSS, no third-party
    libs — the project has none), sorted descending.
  - By day — "day · amount" rows for the last few days.

## Part 3. Tests (mandatory, same session)

Python (`tests/api/test_api_analytics.py`, class `TestAnalyticsEventDetail`):
- category breakdown sorted descending and amounts correct;
- daily breakdown returns correct sums and honours the limit;
- 404 for a non-existent event;
- currency = accounting_currency, formatting with spaces.

Frontend (`webapp/tests/`):
- new `store-analytics.test.js`: `markDirty` → `isStale` → refetch; `loadIfNeeded` skips the
  fetch on a fresh cache; event-detail caching.
- extend `composable-flush-queue.test.js` and `composable-flush-receipt-queue.test.js`:
  assert `analytics.markDirty`.
- extend `store-review.test.js`: analytics re-marked while the receipt queue is non-empty.
- update `store-income.test.js` for `markDirty` instead of `invalidate`.
- cover the event detail in the component in a new/existing view test.

## Part 4. Specs
- `specs/reference/pwa-analytics.md`: rewrite the "Client cache" section for the dirty flag
  (expense/income/receipt, dirty-until-processed); add the event-detail endpoint and describe
  the "by category" and "by recent days" breakdowns.
- `specs/reference/frontend-cache.md`: add an "Analytics store dirty-flag sources" section
  modelled on review/llm.

(Specs — current state and rules only, no signatures/field names — per CLAUDE.md.)

## Work order and done gate
1. Refactor `analytics.js` onto `useStaleCache` + income-store edits.
2. Add `markDirty` at every point (flushQueue, flushReceiptQueue, review mutations + queue).
3. Backend detail endpoint + 2 SQL files + tests.
4. Event-expansion UI + store detail + tests.
5. Update the specs.
6. `scripts/setup-test-env.sh` (if needed), then the gate: `uv run inv pre` → "All checks
   passed!" and `uv run pytest` → `N passed`, plus a green `cd webapp && npm test`.
   `inv pre` after every batch.

Affected files: `stores/analytics.js`, `stores/income.js`, `stores/review.js`,
`composables/flushQueue.js`, `composables/flushReceiptQueue.js`, `views/AnalyticsView.vue`,
`api/analytics.js`, `src/dinary/api/analytics.py`, 2 new `.sql` files, tests (py + webapp),
2 specs.

## Open questions (defaults)
- "last few days" = the event's last 7 days with spend (not global).
- Detail UI = inline expansion (accordion) in the row. Alternative — a bottom sheet
  (`BaseSheet`), as used elsewhere in the app.
