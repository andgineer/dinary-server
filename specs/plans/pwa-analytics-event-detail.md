# Plan: per-event drill-down on the PWA stats page

## Task

Clicking an event on the stats page shows that event's own stats: how much was spent per
category, plus the spend total for each of the last few days.

## Context

Events already arrive in `summary.events` as `{ id, name, date_range, total, currency, open }`
and render as non-interactive rows in `webapp/src/views/AnalyticsView.vue:92-112`.

Relevant schema: `events(id, name, date_from, date_to, …)`,
`expenses(amount, datetime, category_id, event_id, …)`, `categories(id, name, group_id)`,
`category_groups(id, name)`.

**Do this after `pwa-analytics-refresh.md`.** The two share five files
(`api/analytics.py`, `stores/analytics.js`, `views/AnalyticsView.vue`,
`webapp/tests/store-analytics.test.js`, `specs/reference/pwa-analytics.md`), and that plan
rewrites the store onto `useStaleCache` — building the detail map against the current store shape
means writing it twice. It also supplies the dirty flag that §2's cache-clearing rule leans on;
without it the drill-down follows the 24 h TTL and can show pre-receipt numbers.

## 1. Backend: `GET /api/analytics/events/{event_id}`

New route in `src/dinary/api/analytics.py`, 404 when the event does not exist. Response:

```
{ id, name, date_range, total, currency, open,
  categories: [{ category_id, category_name, group_name, total, share, currency }],  # sort desc
  days:       [{ date, date_label, total, currency }] }        # last N days with spend
```

`total` is the `_fmt`-formatted string, as everywhere else in this endpoint family. `share` is a
separate float in 0…1 — **the row's amount over the event total, an actual share**. Without it
the bar in §2 has no numeric input: the view would have to strip thousands spaces out of `total`
and parse back a value `_fmt` already rounded. Computing it server-side from the raw
`SUM(amount)` is cheaper and exact. `currency` repeats per row to match the shape `events[]`
already uses in the summary response.

Two new SQL files in `src/dinary/db/sql/`:

- `analytics_event_categories.sql` — `expenses JOIN categories LEFT JOIN category_groups
  WHERE event_id=? GROUP BY category ORDER BY SUM(amount) DESC`.
  **`category_groups` must be a LEFT JOIN**: `categories.group_id` is nullable
  (`0001_initial_schema.sql:14`) and only `is_active=1` rows are guaranteed a group
  (`db/catalog.py:90-91`), so an inner join silently drops expenses booked to a since-retired
  category and the breakdown stops summing to the event total. `group_name` is null for those
  rows — render them under a neutral fallback label, never hide them. (`expenses.category_id` is
  `NOT NULL`, so the `categories` join needs no such care.)
- `analytics_event_days.sql` — `WHERE event_id=? GROUP BY substr(datetime, 1, 10)
  ORDER BY day DESC LIMIT 7`.
  **`substr(datetime, 1, 10)`, not `date(datetime)`.** Timestamps carry the user's offset
  (`specs/reference/timestamps.md`, e.g. `2026-07-15 00:30:00+02:00`) and SQLite's `date()`
  normalises to UTC first, filing a spend just after midnight under the previous day. `substr`
  takes the stored local date literally — the date the user saw on their device.
  (`analytics_summary.sql` has the same skew via `strftime`; at month granularity it costs a
  couple of hours a month. Out of scope here.)

Reuse `_fmt` and `settings.accounting_currency`. `date_label` needs a short day format
(e.g. "14 Jul") — `_fmt_date_range` does not cover single dates.

## 2. Frontend

- `api/analytics.js` → `fetchEventDetail(eventId)`.
- `stores/analytics.js` → `eventDetails` (in-memory map by id, not persisted) and
  `loadEventDetail(id)` (fetch + cache). Clear `eventDetails` after every successful summary
  fetch — that is what makes the detail obey the dirty flag, since a stale summary always
  refetches first. (The store has no `reset()` today and does not need one.)
- `AnalyticsView.vue` — make the event row an accordion. Use a separate `expandedEventId` for
  expansion state: `ev.open` is already taken (it means "the event is still running") and must
  not be conflated. On expand, fetch the detail (skeleton while loading), then two blocks styled
  like the existing cards:
  - **By category** — "category · amount" rows sorted descending, each with a proportional bar
    sized `share / categories[0].share` (the list is sorted, so `categories[0]` is the largest).
    Inline CSS — the project has no chart library and is not gaining one.
  - **By day** — "day · amount" rows under an explicit "last 7 days" heading. `days` is capped at
    7, so for a longer event it will not add up to the event total shown above, and an unlabelled
    block reads as a bug.
- Accessibility: `.event-row` is a `<div>` today (`AnalyticsView.vue:93-98`). The clickable part
  becomes a `<button>` with `aria-expanded` and `aria-controls` pointing at the detail panel.
- Offline/error: `eventDetails` is memory-only, so an expanded row on a cold offline start has
  nothing to show and no way to fetch. Render an explicit "offline" / "failed to load" line —
  never a skeleton that spins forever.
- Empty: an event with no expenses is normal, not an edge case — `analytics_events.sql` LEFT
  JOINs the expenses, so a trip created in advance sits in the list with a `0` total. Render one
  "no expenses yet" line instead of two headed blocks with nothing under them.

## 3. Tests

Python (`tests/api/test_api_analytics.py`, class `TestAnalyticsEventDetail`):
- category breakdown sorted descending, amounts correct;
- **an expense in a category with `group_id IS NULL` still appears and the breakdown sums to the
  event total** — the regression test for the LEFT JOIN. Use whole-unit amounts: `_fmt` rounds
  every row independently with banker's rounding, so with fractional amounts the per-category
  strings legitimately need not add up, and the test would fail for a reason unrelated to the
  join;
- `share` is each category's fraction of the event total and the values sum to 1.0;
- daily breakdown returns correct sums and honours the limit;
- **a spend just after local midnight is filed under its local date**, not the UTC-shifted one —
  the regression test for `substr` vs `date()`;
- 404 for a non-existent event;
- currency = accounting_currency, formatting with spaces.

Frontend (`webapp/tests/`):
- extend `store-analytics.test.js` (created by `pwa-analytics-refresh.md`; a new file if this
  plan lands first): event-detail caching, and details cleared by a summary refetch.
- **new `component-analytics-view.test.js`** — `webapp/tests/` has no `AnalyticsView` test today,
  so this is a file from scratch (mount + stubbed store): collapsed by default, expanding fetches
  the detail once and caches it, `aria-expanded` flips, and the empty/offline panels render their
  line instead of a skeleton.

## 4. Specs

`specs/reference/pwa-analytics.md`: add the event-detail endpoint and describe the "by category"
and "by recent days" breakdowns, including that the category breakdown covers the whole event
while the day breakdown is capped at the most recent days. **Prose only** — the file's existing
`Data source` block lists response field names, a pre-existing violation of the "no field names
in specs" rule; do not extend it.

## Work order and done gate

1. Endpoint + 2 SQL files + Python tests.
2. Store detail map + `AnalyticsView` expansion + frontend tests.
3. Spec.
4. Gate: `uv run inv pre` → "All checks passed!", `uv run pytest` → `N passed`,
   `cd webapp && npm test` green. `inv pre` after every batch.

Affected files: `src/dinary/api/analytics.py`, 2 new `.sql` files, `api/analytics.js`,
`stores/analytics.js`, `views/AnalyticsView.vue`, tests (py + webapp), 1 spec.

## Open questions (defaults)

- "last few days" = the event's last 7 days with spend, not the last 7 calendar days.
- Detail UI = inline accordion in the row. Alternative — a bottom sheet (`BaseSheet`), as used
  elsewhere in the app.
