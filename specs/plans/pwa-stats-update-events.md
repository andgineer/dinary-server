# Plan: PWA stats refresh, per-event drill-down, LLM view cleanup

## Task

1. The PWA stats page must refresh when expenses/incomes are added and when a receipt is
   scanned. Caching logic mirrors the review page: data counts as dirty until the server has
   fully processed all receipts.
2. Clicking an event shows that event's stats: how much was spent per category, plus the
   spend total for each of the last few days.
3. The LLM view drops the deploy-path hint from the pool header and starts showing the two
   fields the status API already returns but the UI discards: the cooldown deadline and the
   time of the last call.

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

### 1.0 Backend: `receipts_queue` in the summary response
`GET /api/analytics/summary` gains the same `receipts_queue` block the review feed already
returns (`rules.py:209` → `classification_job_counts`, `src/dinary/db/receipts.py:305`) —
import that helper, do not re-query.

This is what makes the rule hold. Review can stay dirty-until-processed only because its *own*
response carries the queue, so `loadNextPage` re-marks itself on every fetch
(`stores/review.js:100-103`). Analytics has no such signal and — per 1.3 — no background probe,
so without the queue in its own response the flag would clear on the first stats-page open
after a scan and never come back:

> scan → `markDirty` → user opens Stats while `pending > 0` → fetch → `stampFresh()` → clean.
> The server finishes 30 s later. `review.loadNextPage` re-marks only *while* the queue is
> non-empty, the drain to zero raises no event, and review may not be mounted at all — nothing
> ever marks analytics again. Stats show pre-receipt numbers until the 24 h TTL or an unrelated
> income edit.

### 1.1 Move `stores/analytics.js` onto `useStaleCache`
- Wire up `useStaleCache({ dirtyKey: "dinary:analytics:dirty",
  fetchedKey: "dinary:analytics:fetchedAt", dataKey: "dinary:analytics:v1" })`.
- Store `summary/events/trends` through `readCache`/`writeCache` (one object). The data key is
  reused as-is: an object written by the old build carries an extra `lastFetched` field that
  the new reader ignores, and the missing `fetchedKey` makes `isStale()` true → one refetch.
- Replace the TTL check in `fetchAll` with `isStale()`. After a successful fetch —
  `writeCache(...)`, then **`stampFresh()` only when every `receipts_queue` counter is zero;
  otherwise `bumpFetchTime()`**, which records the fetch but leaves the dirty flag up.
  `useStaleCache` already exports `bumpFetchTime` for exactly this (currently unused in `src/`,
  covered by `composable-stale-cache.test.js:74`).
- Rename the method to `loadIfNeeded()` (consistent with review/income); export `markDirty`.
- Drop `invalidate()`; use `markDirty()` instead.

### 1.2 Mark dirty at every point that changes expenses/incomes/receipts

| Trigger | File | Action |
|---|---|---|
| New expense sent to the server | `composables/flushQueue.js` | on `anyFlushed` → `useAnalyticsStore().markDirty()` |
| Receipt scanned (not a duplicate) | `composables/flushReceiptQueue.js` | next to `useLlmStore().markDirty()` / `useReviewStore().markDirty()` → `useAnalyticsStore().markDirty()` |
| Server receipt queue non-empty | `stores/review.js` `loadNextPage` | inside the `if (q.pending>0 …)` block add `useAnalyticsStore().markDirty()` — belt-and-braces only, it fires just when review happens to be fetching. The rule itself is carried by 1.0 |
| Category/expense edit, delete, rule confirmation, stuck-receipt resolution, receipt delete | `stores/review.js` (`correct`, `updateExpense`, `deleteExpense`, `confirmAll`, `resolveStuckReceipt`, `deleteReceipt`) | `useAnalyticsStore().markDirty()` |
| Income add/patch/remove | `stores/income.js` | replace 3× `useAnalyticsStore().invalidate()` → `markDirty()` |

### 1.3 Showing fresh data
- `AnalyticsView.onMounted` → `store.loadIfNeeded()` (fetches only when `isStale()`), guarded
  by `isOnline`.
- No background probes in `App.vue` (visibility/online/init) for analytics — it has no badge
  and loads when the tab is opened; the tab-level `loadIfNeeded()` on stale fully covers the
  requirement (deliberate scope narrowing).

Result: after an expense/income is added or a receipt is scanned the store is marked dirty. A
fetch taken while the server queue is still draining writes the data but keeps the flag up
(1.0), so the next stats-page open refetches; the first fetch that sees an empty queue clears
the flag — exactly the review semantics, and independent of whether review was ever opened.

Cost: every stats-page open while receipts are in flight is one extra request on the next open.
Bounded by the queue draining, and the page is opened by hand.

## Part 2. Per-event drill-down (on click)

### 2.1 Backend: `GET /api/analytics/events/{event_id}`
New route in `src/dinary/api/analytics.py`, 404 when the event does not exist. Response:

```
{ id, name, date_range, total, currency, open,
  categories: [{ category_id, category_name, group_name, total, currency }],  # sort desc
  days:       [{ date, date_label, total, currency }] }                       # last N days with spend
```

Two new SQL files in `src/dinary/db/sql/`:
- `analytics_event_categories.sql` — `expenses JOIN categories LEFT JOIN category_groups
  WHERE event_id=? GROUP BY category ORDER BY SUM(amount) DESC`.
  **`category_groups` must be a LEFT JOIN**: `categories.group_id` is nullable
  (`0001_initial_schema.sql:14`) and only `is_active=1` rows are guaranteed a group
  (`db/catalog.py:90-91`), so an inner join silently drops expenses booked to a since-retired
  category and the breakdown stops summing to the event total. `group_name` is then null for
  those rows — render them under a neutral fallback label rather than hiding them.
  (`categories.category_id` needs no such care: `expenses.category_id` is `NOT NULL`.)
- `analytics_event_days.sql` — `WHERE event_id=? GROUP BY date(datetime) ORDER BY day DESC
  LIMIT 7` (the event's last few days, most recent first). Bucketing on the raw `datetime`
  matches how `analytics_summary.sql` already buckets months.

Reuse `_fmt` and `settings.accounting_currency`; for `date_label` use a short day format
(e.g. "14 Jul").

### 2.2 Frontend
- `api/analytics.js` → `fetchEventDetail(eventId)`.
- `stores/analytics.js` → `eventDetails` (in-memory map by id, not persisted),
  `loadEventDetail(id)` (fetch + cache). Clear `eventDetails` at the start of every successful
  summary fetch — that is what makes the detail obey the dirty flag, since a stale flag always
  forces a summary refetch first. (The store has no `reset()` today and does not need one.)
- `AnalyticsView.vue` — make the event row expandable (accordion).
  Important: use a separate `expandedEventId` state for expansion — the `ev.open` field is
  already taken (it means "the event is still running") and must not be conflated. On expand:
  fetch the detail (skeleton while loading), then two blocks styled like the current cards:
  - By category — "category · amount" rows with a proportional bar (inline CSS, no third-party
    libs — the project has none), sorted descending.
  - By day — "day · amount" rows for the last few days. Give the block an explicit
    "last 7 days" heading: `days` is capped at 7 rows, so for a longer event it does not add up
    to the event total in the row above, and an unlabelled block reads as a bug.
- Accessibility: `.event-row` is a `<div>` today (`AnalyticsView.vue:93-98`). The clickable
  part becomes a `<button>` carrying `aria-expanded` and `aria-controls`, with the detail
  panel as its target.
- Offline/error: `eventDetails` is memory-only, so an expanded row on a cold offline start has
  nothing to show and no way to fetch. Render an explicit "offline" / "failed to load" line in
  the panel — never a skeleton that spins forever.

## Part 3. LLM view: drop the path hint, surface cooldown and last-call time

Pure frontend — `GET /api/llm/status` already returns everything needed
(`_snapshot_to_dict` in `src/dinary/api/controllers/llm.py:39-53` emits `cooldown_until` and
`last_at`; neither reaches the screen). No controller or SQL changes.

### 3.1 Remove the preset-path hint from the pool header
- `webapp/src/views/LLMView.vue:42` — delete
  `<span class="pool-hint">from .deploy/llms.toml</span>`, and the `.pool-hint` CSS rule
  (`LLMView.vue:95-101`) with it.
- No other CSS change: `.pool-header` is `justify-content: space-between`, so with the hint
  gone the remaining two children (label, refresh `IconBtn`) still sit left/right. The
  `margin-right: auto` that did that job lived on `.pool-hint` and goes away with it.
- Keep the path in the empty state (`LLMView.vue:67`). That is the one state where it is
  actionable — the pool is empty and the operator has to fill the preset file.

Rationale: `.deploy/` is gitignored and lives on the server, so the path is unreachable from
the PWA; the read-only nature of the pool is already conveyed by the absence of an add
button. The hint is deploy detail leaking into the UI.

### 3.2 Shared time helpers (no duplication)
`formatRelative` currently exists only as a local function inside
`webapp/src/components/ReceiptCascadeCard.vue:34-43`. Extract, do not copy (CLAUDE.md forbids
duplicating logic to avoid an import):

- New `webapp/src/utils/time.js` (new directory) exporting:
  - `formatRelative(iso, now)` — moved verbatim from `ReceiptCascadeCard`, with `Date.now()`
    replaced by an injected `now` (default `Date.now()`) so the ticking ref drives it.
  - `formatRemaining(iso, now)` — the mirror for a future deadline: `"<1 min"`, `"Nm"`,
    `"Nh"`; returns `null` once the deadline has passed.
  - Both keep the existing SQLite-timestamp tolerance (`iso.includes("T") ? iso :
    iso.replace(" ", "T") + "Z"`).
- `ReceiptCascadeCard.vue` — delete the local `formatRelative`, import it from the new module.
  `formatDateTime` stays local (single user).
- New `webapp/src/composables/useNow.js` — `useNow(intervalMs = 30_000)` returns a `now` ref
  of epoch ms, ticking on `setInterval`, cleared in `onBeforeUnmount`.

`useNow` is needed because `LLMView`'s existing 30 s timer (`LLMView.vue:26-28`) only refetches
when `llmStore.dirtyFlag` is set; without an independent tick a countdown rendered from a
static `cooldown_until` would freeze on screen.

### 3.3 `ProviderCard`: cooldown deadline
- `webapp/src/components/ProviderCard.vue` — add a `now` prop (Number, required) and render
  the remaining cooldown next to the status badge when `provider.status === 'cooling'`:
  badge text stays `cooling down`, a sibling `.cooldown-left` span shows
  `formatRemaining(provider.cooldown_until, now)`.
- Render nothing extra when `cooldown_until` is null or already in the past — `formatRemaining`
  returns `null` and the span is `v-if`-ed out. The status badge keeps its own precedence
  (`_derive_status`: disabled → no_key → cooling → available), so this is display-only.
- `LLMView.vue` — `const now = useNow()`, pass `:now="now"` to every `ProviderCard`.

Rationale: `cooling down` with no deadline is indistinguishable from "broken". The deadline is
what tells the user to wait instead of intervening.

### 3.4 `ProviderCard`: when the last call happened
- The meta row (`ProviderCard.vue:53-58`) currently renders `412 calls · last: ok`. Append the
  relative time from `provider.last_at`: `412 calls · last: ok · 3m ago`.
- `v-if` on `provider.last_at` — a provider with `call_count === 0` has none.
- Keep the existing `[data-status]` colouring on `.last-status`; the timestamp is a separate
  muted span so an error status stays visually distinct.

Rationale: `last: ok` from two weeks ago and `last: ok` from a minute ago look identical today,
yet they are "the pipeline is dead" vs "the pipeline is working".

### 3.5 Explicitly out of scope
- **Which provider is next in the failover order.** The most valuable missing signal, but
  `llmbroker.Optimizer` exposes no routing-order accessor (`wilson_bound`, `is_demoted`,
  `load_scores` only). Deriving it in dinary from preset order + status would duplicate broker
  logic and drift. Needs an upstream llmbroker addition — separate task.
- `health.strategy` (`"failover"`, computed in `llm_status`) stays unrendered: the bare word
  tells the user nothing.
- `base_url` stays unrendered.

## Part 4. Tests (mandatory, same session)

Python (`tests/api/test_api_analytics.py`, class `TestAnalyticsEventDetail`):
- category breakdown sorted descending and amounts correct;
- **an expense in a category with `group_id IS NULL` still appears, and the breakdown sums to
  the event total** — the regression test for the LEFT JOIN in 2.1;
- daily breakdown returns correct sums and honours the limit;
- 404 for a non-existent event;
- currency = accounting_currency, formatting with spaces.

Python, Part 1 (`tests/api/test_api_analytics.py`): the summary response carries
`receipts_queue`, zeroed when no jobs exist and reflecting queued jobs otherwise.

Frontend (`webapp/tests/`):
- new `store-analytics.test.js`: `markDirty` → `isStale` → refetch; `loadIfNeeded` skips the
  fetch on a fresh cache; event-detail caching; details cleared by a summary refetch.
- **the dirty-until-processed rule itself**, in `store-analytics.test.js`: a fetch answering
  with a non-empty `receipts_queue` leaves `isStale()` true (so the next open refetches), a
  fetch answering with an all-zero queue clears it. This is the requirement from Task §1 —
  the `store-review.test.js` case below only covers the secondary trigger.
- extend `composable-flush-queue.test.js` and `composable-flush-receipt-queue.test.js`:
  assert `analytics.markDirty`.
- extend `store-review.test.js`: analytics re-marked while the receipt queue is non-empty.
- update `store-income.test.js` for `markDirty` instead of `invalidate`.
- cover the event detail in the component in a new/existing view test.

Frontend, Part 3 (`webapp/tests/`):
- new `utils-time.test.js`: `formatRelative` boundaries (just now / m / h / d) and the
  SQLite-timestamp form; `formatRemaining` returns `null` for a past deadline and the right
  bucket for future ones.
- new `composable-use-now.test.js`: the ref advances on fake timers and the interval is cleared
  on unmount.
- extend `component-provider-card.test.js`: `cooling` + future `cooldown_until` renders the
  remaining time; past/null `cooldown_until` renders no extra span; `last_at` renders the
  relative suffix; `call_count === 0` with no `last_at` renders neither.
- extend/add an `LLMView` test: the header no longer contains `.pool-hint` or the text
  `.deploy/llms.toml`, while the empty state still does.
- `ReceiptCascadeCard` must stay green after `formatRelative` moves out — check
  `webapp/tests/` for existing coverage and extend it if the relative-time line is untested.

No new Python tests for Part 3 — no backend change.

## Part 5. Specs
- `specs/reference/pwa-analytics.md`: rewrite the "Client cache" section for the dirty flag
  (expense/income/receipt, dirty-until-processed — stating that a fetch taken while receipts
  are still being processed does not count as fresh); add the event-detail endpoint and
  describe the "by category" and "by recent days" breakdowns, including that the category
  breakdown covers the whole event while the day breakdown is capped at the most recent days.
- `specs/reference/frontend-cache.md`: add an "Analytics store dirty-flag sources" section
  modelled on review/llm.
- `specs/ui/screens.md`, `## LLM view` — three fixes, all from Part 3:
  - drop `from llms.toml` from the mockup header (`:337`);
  - state in the `### ProviderCard rules` list that a cooling provider shows how long the
    cooldown still has to run, and that the usage line carries the time of the last call;
  - the `RECEIPT QUEUE` strip (mockup `:333`, section `### Receipt queue strip` at `:357`) is
    documented under `## LLM view` but implemented in `ReviewView.vue:188-199` — move the
    section and the mockup rows into the Review view section. Pre-existing divergence, fixed
    here because the same mockup is being edited anyway.
- `specs/reference/llmbroker-integration.md`, `## Admin screen`: the screen's inventory of what
  it shows per provider gains the cooldown remainder and the last-call time.

(Specs — current state and rules only, no signatures/field names — per CLAUDE.md.)

## Work order and done gate
0. Add `receipts_queue` to the summary response (1.0) — everything in Part 1 rests on it.
1. Refactor `analytics.js` onto `useStaleCache` (queue-aware stamping) + income-store edits.
2. Add `markDirty` at every point (flushQueue, flushReceiptQueue, review mutations + queue).
3. Backend detail endpoint + 2 SQL files + tests.
4. Event-expansion UI + store detail + tests.
5. LLM view: extract `utils/time.js` + `useNow`, drop the pool hint, add the cooldown and
   last-call lines + tests. Independent of steps 1-4 — can land first or in its own commit.
6. Update the specs.
7. `scripts/setup-test-env.sh` (if needed), then the gate: `uv run inv pre` → "All checks
   passed!" and `uv run pytest` → `N passed`, plus a green `cd webapp && npm test`.
   `inv pre` after every batch.

Affected files: `stores/analytics.js`, `stores/income.js`, `stores/review.js`,
`composables/flushQueue.js`, `composables/flushReceiptQueue.js`, `views/AnalyticsView.vue`,
`api/analytics.js`, `src/dinary/api/analytics.py`, 2 new `.sql` files, `views/LLMView.vue`,
`components/ProviderCard.vue`, `components/ReceiptCascadeCard.vue`, new `utils/time.js`, new
`composables/useNow.js`, tests (py + webapp), 4 specs.

## Open questions (defaults)
- "last few days" = the event's last 7 days with spend (not global).
- Detail UI = inline expansion (accordion) in the row. Alternative — a bottom sheet
  (`BaseSheet`), as used elsewhere in the app.
- Cooldown countdown granularity = 30 s (`useNow` default), matching the view's existing poll
  interval. A finer tick buys nothing on a minutes-long cooldown.
