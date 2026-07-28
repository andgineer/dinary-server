# Plan: PWA stats page refresh (dirty-until-processed)

## Task

The stats page must refresh when an expense or income is added, when a receipt is scanned, and
when an event is edited. Caching mirrors the review page: data counts as dirty until the server
has finished processing all receipts.

## Context

Stats page = `webapp/src/views/AnalyticsView.vue` + `webapp/src/stores/analytics.js` +
`GET /api/analytics/summary` (`src/dinary/api/analytics.py`).

`analytics.js` today: 24 h TTL only (`lastFetched`), no dirty flag, invalidated by the income
store alone. Adding an expense or scanning a receipt does not reset it.

`stores/review.js` already implements the target behaviour on the shared `useStaleCache`
composable. **This plan copies that behaviour; it does not invent a variant of it.**

## 1. Backend: `receipts_queue` in the summary response

`GET /api/analytics/summary` returns the same `receipts_queue` block the review feed does —
import `classification_job_counts` (`db/receipts.py:305`, used at
`api/controllers/rules.py:209`), do not re-query.

Analytics has no badge and no background probe, so nothing external can re-mark it once the flag
is cleared. Carrying the queue in its own response is what lets each fetch decide whether it
counts as fresh.

**Read the queue counts first, before the aggregates.** `get_db` yields an autocommit connection
(`db/storage.py:291-297`), so each SELECT is its own snapshot. Counts read last means a job
committing mid-handler yields totals without the new expense *and* an empty queue — the flag
clears on data that is already stale. Counts first costs at most one extra refetch.

## 2. `stores/analytics.js` onto `useStaleCache`

- `useStaleCache({ dirtyKey: "dinary:analytics:dirty", fetchedKey: "dinary:analytics:fetchedAt",
  dataKey: "dinary:analytics:v1" })`; hold `summary`/`events`/`trends` through
  `readCache`/`writeCache` as one object. The data key is reused as-is: the old build's extra
  `lastFetched` field is ignored by the new reader, and the absent `fetchedKey` makes the first
  `isStale()` true → one refetch.
- End of a successful fetch: `writeCache(...)`, `stampFresh()`, then `markDirty()` when any of
  `pending`, `in_progress`, `sleeping` is non-zero (`poisoned` excluded — §3). This is
  `review.loadNextPage`'s exact shape (`stores/review.js:98-103`) — copy it.
  **Not `bumpFetchTime()`**: it leaves `dirtyFlag` untouched, so on a cold start (empty
  localStorage, or a receipt scanned on another device) the flag never goes up and the next
  stats-page open never refetches.
- `fetchAll` → `loadIfNeeded()` gated on `isStale()`; export `markDirty`; drop `invalidate()`.
- `AnalyticsView.onMounted` → `store.loadIfNeeded()`, guarded by `isOnline`. No background probes
  in `App.vue` — the page has no badge and loads on tab open.

## 3. Which queue buckets count as "still processing"

`pending`, `in_progress`, `sleeping` count. `poisoned` does not.

A poisoned job is terminal: only a re-POST of the receipt returns it to `pending`
(`db/receipts.py:296-300`), and every user action that can trigger that already marks analytics
dirty in §4. Counting it would pin the flag up permanently — one unresolved poisoned receipt
would mean a refetch on every stats-page open, forever.

`sleeping` counts even though its retry schedule tops out at one a day and never gives up
(`task.py:89-99`): a sleeping job still completes on its own, so a fetch taken during its
3 s / 60 s / 15 min steps would otherwise stamp fresh and miss the result. The cost is that a
permanently failing receipt makes every stats-page open a refetch — accepted, the page is opened
by hand and it is one request.

Both rules go into `specs/reference/frontend-cache.md` (§6); they outlive this plan.

## 4. Mark dirty at every point that changes what the stats page shows

| Trigger | File | Action |
|---|---|---|
| New expense sent to the server | `composables/flushQueue.js` | on `anyFlushed` → `useAnalyticsStore().markDirty()` |
| Receipt scanned (not a duplicate) | `composables/flushReceiptQueue.js` | next to the existing `useLlmStore().markDirty()` / `useReviewStore().markDirty()` |
| Server receipt queue still draining | `stores/review.js` `loadNextPage` | belt-and-braces; the rule itself is carried by §1. **Not inside the existing `if (q.pending>0 …)` block** — that one also fires on `poisoned`. Add a separate `if (q.pending > 0 \|\| q.in_progress > 0 \|\| q.sleeping > 0) useAnalyticsStore().markDirty()`, leaving review's own condition untouched |
| Expense correction, edit, delete, stuck-receipt resolution, receipt delete | `stores/review.js` (`correct`, `updateExpense`, `deleteExpense`, `resolveStuckReceipt`, `deleteReceipt`) | `useAnalyticsStore().markDirty()` |
| Income add/patch/remove | `stores/income.js` | replace 3× `useAnalyticsStore().invalidate()` → `markDirty()` |
| Event added, renamed, re-dated or deleted | `stores/catalog.js` (`add`, `patch`, `remove` — `catalog.js:561,578,591`) | `if (kind === "event") useAnalyticsStore().markDirty()` |

Events are the one catalog entity the stats page renders directly: a rename changes the displayed
name, a date shift changes `date_range`, the `open` pill, and whether the event is still inside
the 12-month window of `analytics_events.sql`. Groups and tags need no marking — they surface
only through `trends[].basket_name`, recomputed from the same fetch.

**Not `confirmAll`.** Bulk rule confirmation (`api/controllers/rules.py:128-142`) writes only
`confidence_level = 4` on rules and their expenses — no amount, category, event or date moves, so
no figure on the page can change. (`correct` is different: its doubtful branch goes through
`approve_rule_category`, which re-books expenses to another category.)

## 5. Tests

Python (`tests/api/test_api_analytics.py`): the summary response carries `receipts_queue`, zeroed
when no jobs exist and reflecting queued jobs otherwise.

Frontend (`webapp/tests/`):
- new `store-analytics.test.js`:
  - `markDirty` → `isStale` → refetch; `loadIfNeeded` skips the fetch on a fresh cache;
  - a fetch answering with a non-empty `pending`/`in_progress`/`sleeping` leaves `isStale()`
    true; with those three at zero it clears — **including when `poisoned > 0`**, the §3
    carve-out, which needs its own case;
  - **cold start**: from empty localStorage (no dirty flag, no `lastFetchedAt`), a first fetch
    answering with a busy queue must leave `isStale()` true. This is the case that fails if §2 is
    built on `bumpFetchTime()`; every other case here starts from an already-dirty store.
- extend `composable-flush-queue.test.js` and `composable-flush-receipt-queue.test.js`: assert
  `analytics.markDirty`. In the first file, `:146` is named "does not call markDirty (only
  receipt sends invalidate llm/review)" and asserts it for the llm and review stores — still
  valid, but rename it to name those two stores explicitly.
- extend `store-review.test.js`: analytics re-marked while the receipt queue is non-empty, and
  **not** marked by `confirmAll`.
- extend `store-catalog.test.js`: `add`/`patch`/`remove` with `kind === "event"` mark analytics
  dirty; with `kind === "tag"` they do not.
- `store-income.test.js` has no analytics coverage today — new coverage to write, not an edit.

## 6. Specs

- `specs/reference/frontend-cache.md`: add an "Analytics store dirty-flag sources" section
  modelled on the review/llm ones, and state the §3 rule — a terminal failure is not "still
  processing". The `useStaleCache` section needs no change: §2 uses `stampFresh()` +
  `markDirty()`, so "`bumpFetchTime()` … is not used by any store" stays true.
- `specs/reference/pwa-analytics.md`: rewrite the "Client cache" section for the dirty flag
  (expense/income/receipt/event edit; a fetch taken while receipts are still being processed does
  not count as fresh). **Prose only** — the file's existing `Data source` block lists response
  field names, a pre-existing violation of the "no field names in specs" rule; do not extend it.

## Work order and done gate

1. `receipts_queue` in the summary response (§1) — everything else rests on it.
2. `analytics.js` onto `useStaleCache` (§2) + `AnalyticsView` wiring + income-store edits.
3. `markDirty` at the remaining points (§4).
4. Tests (§5), specs (§6).
5. Gate: `uv run inv pre` → "All checks passed!", `uv run pytest` → `N passed`,
   `cd webapp && npm test` green. `inv pre` after every batch.

Affected files: `src/dinary/api/analytics.py`, `stores/analytics.js`, `stores/income.js`,
`stores/review.js`, `stores/catalog.js`, `composables/flushQueue.js`,
`composables/flushReceiptQueue.js`, `views/AnalyticsView.vue`, tests (py + webapp), 2 specs.
