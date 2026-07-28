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
returns (`api/controllers/rules.py:209` → `classification_job_counts`,
`src/dinary/db/receipts.py:305`) — import that helper, do not re-query.

**Read the queue counts first, before the aggregates.** `get_db` yields a connection in
autocommit (`db/storage.py:291-297`), so every SELECT in the handler is its own snapshot. With
the counts read last, a job that commits between the aggregate reads and the count read gives
totals without the new expense *and* an empty queue → `stampFresh()` → the flag is cleared on
data that is already stale. Reading the counts first inverts the race into the harmless
direction: a stale-but-busy reading costs one extra refetch.

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

**`poisoned` is excluded from the "still draining" test; `pending`, `in_progress` and
`sleeping` are not.** A poisoned job is terminal: nothing moves it back to `pending` on its own
(the only writer of `status='pending'` is the re-enqueue in `db/receipts.py`, reached by a
re-POST of the receipt). It can therefore never change analytics data without a user action —
and every such action (rescan → `flushReceiptQueue`, `resolveStuckReceipt`, `deleteReceipt`)
already marks analytics dirty in 1.2. Counting it would pin the flag up permanently: one
unresolved poisoned receipt = a refetch on *every* Stats-page open, forever, with no
compensating correctness.

`sleeping` must stay in the test even though it has the same unbounded tail (`_retry_delay` in
`background/classification/task.py:89-99` tops out at one retry a day and, by design, never
gives up). A sleeping job *does* still complete on its own, so a fetch taken during the 3 s /
60 s / 15 min steps that stamped fresh would miss the result — the exact hole 1.0 exists to
close.

### 1.1 Move `stores/analytics.js` onto `useStaleCache`
- Wire up `useStaleCache({ dirtyKey: "dinary:analytics:dirty",
  fetchedKey: "dinary:analytics:fetchedAt", dataKey: "dinary:analytics:v1" })`.
- Store `summary/events/trends` through `readCache`/`writeCache` (one object). The data key is
  reused as-is: an object written by the old build carries an extra `lastFetched` field that
  the new reader ignores, and the missing `fetchedKey` makes `isStale()` true → one refetch.
- Replace the TTL check in `fetchAll` with `isStale()`. After a successful fetch —
  `writeCache(...)`, then **`stampFresh()` unconditionally, followed by `markDirty()` when any
  of `pending`, `in_progress`, `sleeping` is non-zero** (`poisoned` deliberately not counted —
  see 1.0). This is `review.loadNextPage`'s exact shape (`stores/review.js:98-103`).
  **Not `bumpFetchTime()`.** That call only writes `lastFetchedAt`; it leaves `dirtyFlag`
  untouched, which re-marks nothing when the flag was not already up. On a cold start (empty
  localStorage, or a receipt scanned on another device) the sequence is
  `isStale()` true because `lastFetchedAt` is null → fetch → queue busy → `bumpFetchTime()` →
  `dirtyFlag` still `false` → `isStale()` false → the next open never refetches. That is the
  very hole 1.0 exists to close. `stampFresh()` + `markDirty()` reaches the same end state
  (`lastFetchedAt` written, flag up) and works regardless of the flag's prior value, so
  `bumpFetchTime` stays unused by any store.
- Rename the method to `loadIfNeeded()` (consistent with review/income); export `markDirty`.
- Drop `invalidate()`; use `markDirty()` instead.

### 1.2 Mark dirty at every point that changes what the stats page shows

| Trigger | File | Action |
|---|---|---|
| New expense sent to the server | `composables/flushQueue.js` | on `anyFlushed` → `useAnalyticsStore().markDirty()` |
| Receipt scanned (not a duplicate) | `composables/flushReceiptQueue.js` | next to `useLlmStore().markDirty()` / `useReviewStore().markDirty()` → `useAnalyticsStore().markDirty()` |
| Server receipt queue still draining | `stores/review.js` `loadNextPage` | belt-and-braces only, it fires just when review happens to be fetching; the rule itself is carried by 1.0. **Not inside the existing `if (q.pending>0 …)` block** — that one also fires on `poisoned`, which 1.0 rules out. Add a separate `if (q.pending > 0 \|\| q.in_progress > 0 \|\| q.sleeping > 0) useAnalyticsStore().markDirty()`, leaving review's own condition untouched |
| Category/expense edit, delete, stuck-receipt resolution, receipt delete | `stores/review.js` (`correct`, `updateExpense`, `deleteExpense`, `resolveStuckReceipt`, `deleteReceipt`) | `useAnalyticsStore().markDirty()` |
| Income add/patch/remove | `stores/income.js` | replace 3× `useAnalyticsStore().invalidate()` → `markDirty()` |
| Event added, renamed, re-dated or deleted | `stores/catalog.js` (`add`, `patch`, `remove`) | `if (kind === "event") useAnalyticsStore().markDirty()` — one line in each of the three generic dispatchers (`catalog.js:561,578,591`) |

The event row is the one catalog entity the stats page renders directly: a rename changes the
displayed name, a date shift changes both `date_range` and the `open` pill and can move the
event in or out of the 12-month window in `analytics_events.sql`, and Part 2 makes events
clickable on top of that. Groups and tags need no marking — they surface only through
`trends[].basket_name`, which is recomputed from the same fetch.

**Not `confirmAll`.** Bulk rule confirmation (`api/controllers/rules.py:128-142`) only writes
`confidence_level = 4` on the rules and their expenses — no amount, category, event or date
changes, so no figure on the stats page can move. Marking dirty there would cost a refetch per
confirmation and buy nothing. (`correct` is different: its doubtful branch goes through
`approve_rule_category`, which does re-book expenses to another category and therefore moves
the trend baskets.)

### 1.3 Showing fresh data
- `AnalyticsView.onMounted` → `store.loadIfNeeded()` (fetches only when `isStale()`), guarded
  by `isOnline`.
- No background probes in `App.vue` (visibility/online/init) for analytics — it has no badge
  and loads when the tab is opened; the tab-level `loadIfNeeded()` on stale fully covers the
  requirement (deliberate scope narrowing).

Result: after an expense/income is added or a receipt is scanned the store is marked dirty. A
fetch taken while the server queue is still draining writes the data and immediately re-marks
the flag (1.0), so the next stats-page open refetches; the first fetch that sees a drained queue
leaves the flag clear — the review semantics minus the terminal `poisoned` bucket, and
independent of whether review was ever opened or whether the flag was up before the fetch.

Cost: while receipts are in flight every stats-page open costs one extra request on the next
open. In the normal case that ends when the queue drains. It is *not* strictly bounded: a
receipt whose fetch keeps failing sits in `sleeping` on the one-a-day retry indefinitely, and
for as long as it does, every stats-page open is a refetch. Accepted — the page is opened by
hand, it is one request, and the alternative (dropping `sleeping`) silently loses results.

## Part 2. Per-event drill-down (on click)

### 2.1 Backend: `GET /api/analytics/events/{event_id}`
New route in `src/dinary/api/analytics.py`, 404 when the event does not exist. Response:

```
{ id, name, date_range, total, currency, open,
  categories: [{ category_id, category_name, group_name, total, share, currency }],  # sort desc
  days:       [{ date, date_label, total, currency }] }        # last N days with spend
```

`total` is the `_fmt`-formatted string, as everywhere else in this endpoint family. **`share`
is a separate float in 0…1 — the row's amount over the event total**, i.e. an actual share, not
a bar width. Without it the proportional bar in 2.2 has no numeric input: the frontend would
have to strip the thousands spaces out of `total` and parse back a value `_fmt` already rounded.
Computing the ratio server-side from the raw `SUM(amount)`, before formatting, is both cheaper
and exact.

The bar in 2.2 is full-width for the top row, so its width is `share / categories[0].share` —
the rows are sorted descending, so `categories[0]` is the largest. That is one float division on
data the view already holds; normalising server-side against the largest row instead would put
a presentation-derived number behind a field named `share`, which every later reader would
misread as a share of the total. (`currency` is repeated per row on purpose — it matches the
shape `events[]` already uses in the summary response.)

Two new SQL files in `src/dinary/db/sql/`:
- `analytics_event_categories.sql` — `expenses JOIN categories LEFT JOIN category_groups
  WHERE event_id=? GROUP BY category ORDER BY SUM(amount) DESC`.
  **`category_groups` must be a LEFT JOIN**: `categories.group_id` is nullable
  (`0001_initial_schema.sql:14`) and only `is_active=1` rows are guaranteed a group
  (`db/catalog.py:90-91`), so an inner join silently drops expenses booked to a since-retired
  category and the breakdown stops summing to the event total. `group_name` is then null for
  those rows — render them under a neutral fallback label rather than hiding them.
  (`categories.category_id` needs no such care: `expenses.category_id` is `NOT NULL`.)
- `analytics_event_days.sql` — `WHERE event_id=? GROUP BY substr(datetime, 1, 10)
  ORDER BY day DESC LIMIT 7` (the event's last few days, most recent first).
  **`substr(datetime, 1, 10)`, not `date(datetime)`.** Timestamps are stored with the user's
  offset (`specs/reference/timestamps.md`, e.g. `2026-07-15 00:30:00+02:00`), and SQLite's
  `date()` normalises to UTC first — `date('2026-07-15 00:30:00+02:00')` is `2026-07-14`. A
  spend just after midnight would be filed under the previous day. `substr` takes the stored
  local date literally, which is the date the user saw on their device.
  (`analytics_summary.sql` has the same skew via `strftime`, but at month granularity it
  touches a couple of hours a month; at day granularity it touches a couple of hours a day.
  Not fixed here — out of scope.)

Reuse `_fmt` and `settings.accounting_currency`; for `date_label` use a short day format
(e.g. "14 Jul").

### 2.2 Frontend
- `api/analytics.js` → `fetchEventDetail(eventId)`.
- `stores/analytics.js` → `eventDetails` (in-memory map by id, not persisted),
  `loadEventDetail(id)` (fetch + cache). Clear `eventDetails` once a summary fetch has
  resolved successfully, alongside `writeCache(...)` — that is what makes the detail obey the
  dirty flag, since a stale flag always forces a summary refetch first. (The store has no
  `reset()` today and does not need one.)
- `AnalyticsView.vue` — make the event row expandable (accordion).
  Important: use a separate `expandedEventId` state for expansion — the `ev.open` field is
  already taken (it means "the event is still running") and must not be conflated. On expand:
  fetch the detail (skeleton while loading), then two blocks styled like the current cards:
  - By category — "category · amount" rows with a proportional bar sized `share /
    categories[0].share` (inline CSS, no third-party libs — the project has none), sorted
    descending.
  - By day — "day · amount" rows for the last few days. Give the block an explicit
    "last 7 days" heading: `days` is capped at 7 rows, so for a longer event it does not add up
    to the event total in the row above, and an unlabelled block reads as a bug.
- Accessibility: `.event-row` is a `<div>` today (`AnalyticsView.vue:93-98`). The clickable
  part becomes a `<button>` carrying `aria-expanded` and `aria-controls`, with the detail
  panel as its target.
- Offline/error: `eventDetails` is memory-only, so an expanded row on a cold offline start has
  nothing to show and no way to fetch. Render an explicit "offline" / "failed to load" line in
  the panel — never a skeleton that spins forever.
- Empty: an event with no expenses is a normal row, not an edge case — `analytics_events.sql`
  LEFT JOINs the expenses, so a trip created in advance sits in the list with a `0` total. Both
  breakdowns come back empty for it; render a single "no expenses yet" line instead of two
  headed blocks with nothing under them.

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

- New `webapp/src/composables/time.js` exporting:
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

`composables/`, not a new `utils/` directory: pure non-composable helpers already live there
(`composables/receipt.js` is `parseReceiptUrl`, `composables/swHealth.js` is two plain
functions). One module is not a reason to open a second shared-code directory.

`useNow` is needed because `LLMView`'s existing 30 s timer (`LLMView.vue:26-28`) only refetches
when `llmStore.dirtyFlag` is set; without an independent tick a countdown rendered from a
static `cooldown_until` would freeze on screen.

### 3.3 `ProviderCard`: cooldown deadline
- `webapp/src/components/ProviderCard.vue` — add a `now` prop (Number, default `Date.now()`)
  and render the remaining cooldown next to the status badge when
  `provider.status === 'cooling'`: badge text stays `cooling down`, a sibling `.cooldown-left`
  span shows `formatRemaining(provider.cooldown_until, now)`.
  Default, not `required`: `component-provider-card.test.js` mounts the component with only
  `provider` in ~a dozen places, and a required prop turns every one of them into a Vue warning
  for no gain.
- Render nothing extra when `cooldown_until` is null or already in the past — `formatRemaining`
  returns `null` and the span is `v-if`-ed out. The status badge keeps its own precedence
  (`_derive_status`: disabled → no_key → cooling → available), so this is display-only.
- `LLMView.vue` — `const now = useNow()`, pass `:now="now"` to every `ProviderCard`.

**The badge must not outlive the deadline.** `status` is derived server-side at fetch time, and
`LLMView`'s 30 s timer only refetches when `llmStore.dirtyFlag` is set — which `refresh()`
immediately clears via `stampFresh()`. In practice the pool is fetched once per mount and then
frozen. A ticking `now` alone would therefore produce `cooling down` with the remainder gone —
precisely the "indistinguishable from broken" state this section exists to remove.

Fix inside the existing timer callback in `LLMView.vue:26-28`: also refresh when any provider
has `status === 'cooling'` and a `cooldown_until` that has passed. One condition, no new
watcher, and it reconciles `status`, `call_count` and `last_at` in the same round trip rather
than re-deriving the badge on the client and drifting from `_derive_status`.

This leaves a window of up to one tick (30 s) in which the badge still reads `cooling down` with
no remainder beside it, since `formatRemaining` returns `null` the moment the deadline passes.
Accepted: closing it means deriving the badge on the client, which is exactly the drift from
`_derive_status` this section refuses. Half a minute of a stale badge is not the failure mode
being fixed — a badge stuck for the whole mount is.

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
  the event total** — the regression test for the LEFT JOIN in 2.1. Use whole-unit amounts in
  the fixture: `_fmt` rounds every row independently with Python's banker's rounding, so for
  fractional amounts the per-category strings legitimately need not add up to the formatted
  total, and the test would fail for a reason that has nothing to do with the join;
- `share` is each category's fraction of the event total and the values sum to 1.0;
- daily breakdown returns correct sums and honours the limit;
- **a spend just after local midnight is filed under its local date**, not the UTC-shifted one
  — the regression test for `substr` vs `date()` in 2.1;
- 404 for a non-existent event;
- currency = accounting_currency, formatting with spaces.

Python, Part 1 (`tests/api/test_api_analytics.py`): the summary response carries
`receipts_queue`, zeroed when no jobs exist and reflecting queued jobs otherwise.

Frontend (`webapp/tests/`):
- new `store-analytics.test.js`: `markDirty` → `isStale` → refetch; `loadIfNeeded` skips the
  fetch on a fresh cache; event-detail caching; details cleared by a summary refetch.
- **the dirty-until-processed rule itself**, in `store-analytics.test.js`: a fetch answering
  with a non-empty `pending`/`in_progress`/`sleeping` leaves `isStale()` true (so the next open
  refetches), a fetch answering with those three at zero clears it — **including when
  `poisoned > 0`**, which is the 1.0 carve-out and needs its own case. This is the requirement
  from Task §1; the `store-review.test.js` case below only covers the secondary trigger.
- **the cold-start case explicitly**: starting from empty localStorage (no dirty flag, no
  `lastFetchedAt`), a first fetch that answers with a busy queue must leave `isStale()` true.
  This is what fails if 1.1 is implemented with `bumpFetchTime()` instead of
  `stampFresh()` + `markDirty()`, and no other case in this list catches it — they all start
  from an already-dirty store.
- extend `composable-flush-queue.test.js` and `composable-flush-receipt-queue.test.js`:
  assert `analytics.markDirty`. In the first file, `:146` is currently named "does not call
  markDirty (only receipt sends invalidate llm/review)" and asserts it for the llm and review
  stores — it stays valid but the name becomes wrong; rename it to name those two stores.
- extend `store-review.test.js`: analytics re-marked while the receipt queue is non-empty, and
  **not** marked by `confirmAll`.
- `store-income.test.js` has no analytics coverage at all today (no reference to `invalidate`
  or to the analytics store) — this is new coverage to write, not an edit.
- extend `store-catalog.test.js`: `patch`/`remove`/`add` with `kind === "event"` mark analytics
  dirty, with `kind === "tag"` (or `"group"`) do not.
- **new `component-analytics-view.test.js`** — there is no `AnalyticsView` test in
  `webapp/tests/` today, so the event drill-down needs a file from scratch (mount + stubbed
  store): collapsed by default, expanding fetches the detail once and caches it, `aria-expanded`
  flips, and the empty/offline panels render their line instead of a skeleton.

Frontend, Part 3 (`webapp/tests/`):
- new `composable-time.test.js` (the directory names composable tests `composable-*.test.js`,
  and the module lands in `composables/`): `formatRelative` boundaries (just now / m / h / d)
  and the SQLite-timestamp form; `formatRemaining` returns `null` for a past deadline and the
  right bucket for future ones.
- new `composable-use-now.test.js`: the ref advances on fake timers and the interval is cleared
  on unmount.
- extend `component-provider-card.test.js`: `cooling` + future `cooldown_until` renders the
  remaining time; past/null `cooldown_until` renders no extra span; `last_at` renders the
  relative suffix; `call_count === 0` with no `last_at` renders neither. Every one of these
  passes an explicit `now` — the shared `BASE_PROVIDER` fixture pins `last_at` to
  `2026-05-10T11:30:00+00:00`, so an assertion resting on the real `Date.now()` would be a
  date-dependent test that rots (CLAUDE.md forbids leaving those).
- **new `component-llm-view.test.js`** — `webapp/tests/` has no `LLMView` test today either, so
  this is a second file from scratch (mount + stubbed `llm` store): the header no longer
  contains `.pool-hint` or the text `.deploy/llms.toml`, while the empty state still does; and,
  on fake timers, a provider whose `cooldown_until` has passed triggers a refetch on the next
  tick even with `dirtyFlag` clear (the 3.3 carve-out).
- `ReceiptCascadeCard` must stay green after `formatRelative` moves out — check
  `webapp/tests/` for existing coverage and extend it if the relative-time line is untested.

No new Python tests for Part 3 — no backend change.

## Part 5. Specs
- `specs/reference/pwa-analytics.md`: rewrite the "Client cache" section for the dirty flag
  (expense/income/receipt/event edit, dirty-until-processed — stating that a fetch taken while
  receipts are still being processed does not count as fresh); add the event-detail endpoint and
  describe the "by category" and "by recent days" breakdowns, including that the category
  breakdown covers the whole event while the day breakdown is capped at the most recent days.
  **In prose only.** The file already carries a `Data source` block listing response field
  names — a pre-existing violation of the "no field names in specs" rule. Do not extend it with
  the new endpoint (cleaning up the existing block is a separate task, not this one).
- `specs/reference/frontend-cache.md`: add an "Analytics store dirty-flag sources" section
  modelled on review/llm, stating the `poisoned` carve-out as a rule (a terminal failure is not
  "still processing"). The `useStaleCache` section needs no change — 1.1 uses
  `stampFresh()` + `markDirty()`, so "`bumpFetchTime()` … is not used by any store" stays true.
- `specs/ui/screens.md`, `## LLM view` — from Part 3:
  - drop `from llms.toml` from the mockup header (`:337`);
  - state in the `### ProviderCard rules` list that a cooling provider shows how long the
    cooldown still has to run, and that the usage line carries the time of the last call;
  - the section closes with "Refresh polled every 30 s when online", which was already only
    half-true (the tick refetches only while the store is dirty) and changes again under 3.3 —
    restate it as: refetch on open, and while a provider is cooling, once its deadline passes.
  - the `RECEIPT QUEUE` strip (mockup `:333`, section `### Receipt queue strip` at `:357`) is
    documented under `## LLM view` but implemented in `ReviewView.vue:188-199` — move the
    section and the mockup rows into the Review view section. Pre-existing divergence, fixed
    here because the same mockup is being edited anyway. While moving it, fix the first chip:
    the spec says `N ready`, the code renders `N queued` (`ReviewView.vue:196`).
- `specs/reference/llmbroker-integration.md`, `## Admin screen`: the screen's inventory of what
  it shows per provider gains the cooldown remainder and the last-call time.

(Specs — current state and rules only, no signatures/field names — per CLAUDE.md.)

## Work order and done gate
0. Add `receipts_queue` to the summary response (1.0) — everything in Part 1 rests on it.
1. Refactor `analytics.js` onto `useStaleCache` (queue-aware stamping) + income-store edits.
2. Add `markDirty` at every point (flushQueue, flushReceiptQueue, review mutations + queue,
   catalog event mutations).
3. Backend detail endpoint + 2 SQL files + tests.
4. Event-expansion UI + store detail + tests.
5. LLM view: extract `composables/time.js` + `useNow`, drop the pool hint, add the cooldown and
   last-call lines and the deadline-expiry refetch + tests. Independent of steps 1-4 — can land
   first or in its own commit.
6. Update the specs.
7. `scripts/setup-test-env.sh` (if needed), then the gate: `uv run inv pre` → "All checks
   passed!" and `uv run pytest` → `N passed`, plus a green `cd webapp && npm test`.
   `inv pre` after every batch.

Affected files: `stores/analytics.js`, `stores/income.js`, `stores/review.js`,
`stores/catalog.js`,
`composables/flushQueue.js`, `composables/flushReceiptQueue.js`, `views/AnalyticsView.vue`,
`api/analytics.js`, `src/dinary/api/analytics.py`, 2 new `.sql` files, `views/LLMView.vue`,
`components/ProviderCard.vue`, `components/ReceiptCascadeCard.vue`, new `composables/time.js`,
new `composables/useNow.js`, tests (py + webapp), 4 specs.

## Open questions (defaults)
- "last few days" = the event's last 7 days with spend (not global).
- Detail UI = inline expansion (accordion) in the row. Alternative — a bottom sheet
  (`BaseSheet`), as used elsewhere in the app.
- Cooldown countdown granularity = 30 s (`useNow` default), matching the view's existing poll
  interval. A finer tick buys nothing on a minutes-long cooldown.
