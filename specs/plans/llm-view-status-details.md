# Plan: LLM view — drop the deploy-path hint, show the cooldown countdown, replace the call counter with a failure ratio

## Task

1. The LLM view drops the deploy-path hint from the pool header.
2. A cooling provider shows how long its cooldown still has to run (`cooldown_until` is already
   returned by the status API and discarded by the UI).
3. The per-provider call counter is replaced by the share of failed calls over a fixed recent
   window. The counter, the last-call status and the last-call time all leave the screen.

## Context

`GET /api/llm/status` (`_snapshot_to_dict`, `src/dinary/api/controllers/llm.py:39-53`) emits
`cooldown_until`, `call_count`, `last_status` and `last_at`. Only the first is worth rendering;
the other three are dropped from the response and replaced by the window aggregate below, so this
plan is no longer frontend-only — it touches the controller and needs Python tests.

Independent of the two `pwa-analytics-*` plans — no shared files (they touch
`src/dinary/api/analytics.py`, this one `src/dinary/api/controllers/llm.py`).

### Why the counter goes

An absolute call count has no baseline to compare against and, under failover, is predetermined:
the top healthy provider takes essentially all traffic and the rest idle in reserve. "11 calls"
versus "25 calls" changes no decision on a screen whose only decisions are *disable a provider*,
*add a key*, *wait out a cooldown*.

The number is also less than it claims. `snapshot().metrics` is derived from a cached journal
tail (`llmbroker/broker/learning.py`), rebuilt only when the pool is provisioned or when a call
fails — a run of successful calls never moves it, and the count is capped by the tail window.
That is the documented llmbroker contract (`specs/reference/decisions.md`, "Metrics stay in
`snapshot()` … computed from the cached journal tail, with no queries of its own"), not a defect
to fix upstream: this plan simply stops reading a field whose guarantee does not match what the
screen needs, and derives its own aggregate from the journal instead.

What replaces it answers the one question the screen cannot answer today: a provider with
`status: available` and a third of its calls failing over the week looks perfectly healthy here,
and its failures surface only in the moment it is cooling — i.e. while the user is already
waiting on a receipt.

## 1. Remove the preset-path hint from the pool header

- `webapp/src/views/LLMView.vue:42` — delete `<span class="pool-hint">from .deploy/llms.toml</span>`
  and the `.pool-hint` CSS rule (`LLMView.vue:95-101`) with it.
- No other CSS change: `.pool-header` is `justify-content: space-between`, so the remaining two
  children (label, refresh `IconBtn`) still sit left/right. The `margin-right: auto` that did
  that job lived on `.pool-hint` and goes with it.
- Keep the path in the empty state (`LLMView.vue:67`) — the one state where it is actionable: the
  pool is empty and the operator has to fill the preset file.

Rationale: `.deploy/` is gitignored and lives on the server, so the path is unreachable from the
PWA, and the pool's read-only nature is already conveyed by the absence of an add button.

## 2. Backend: failure ratio over a recent window

`llm_status` (`src/dinary/api/controllers/llm.py:56`) gains one journal read and folds the result
into each provider dict.

- Source: `await llms.calls(limit=…)` — the broker's public journal accessor
  (`AsyncBroker.calls`, scope-aware). It reads the store directly and does **not** call
  `ensure_pool()`, so it is safe on an empty registry; it raises `TypeError` only for a
  non-queryable backend, which the sqlite store is not. Do not query `llmbroker_calls` directly:
  the llmbroker table schema is explicitly not a public contract.
- Skip the read entirely when the snapshot is empty — nothing to attribute rows to.
- Keep rows with `kind == "call"`. The journal interleaves `kind == "quality"` records, whose
  `status` is `None` by construction; counting them would inflate the denominator.
- Window: rows whose `ts` is within the last 7 days. Drop rows with `ts is None` rather than
  treating them as recent. Journal retention is 90 days upstream (`llmbroker/sqlite/store.py`),
  so a 7-day window is always fully covered.
- `limit=1000`: the pipeline writes about one call plus one quality record per receipt, so a week
  of normal use is a few hundred rows. The window is bounded by *both* the limit and the 7 days —
  if the tail ever fills the limit the effective window is shorter. Accepted, not detected: at
  this volume it does not arise, and the alternative is an unbounded read.
- Failure = any `status` other than `CallStatus.OK` (`RATE_LIMITED`, `UNAVAILABLE`, `ERROR`).
  **429 counts as a failure on purpose**: for the user the effect is identical — that call
  returned nothing and the request spilled to the next model. That quota exhaustion is "normal"
  for a free tier is exactly what the number should make visible, not hide.
- Replace `call_count`, `last_status` and `last_at` in the response with `recent_calls`,
  `recent_failures` and `recent_window_days`. The window length ships in the response rather than
  being hardcoded on both sides.
- A provider with no rows in the window gets `recent_calls: 0`, `recent_failures: 0` — and the UI
  must render that as "no calls", never as "no failures" (§4). `_snapshot_to_dict` stops touching
  `snap.metrics` altogether.

`demoted` and `quality_bound` stay as they are — they come from the pool and the optimizer and
are current at fetch time.

## 3. `ProviderCard`: cooldown deadline

- New `webapp/src/composables/useNow.js` — `useNow(intervalMs = 30_000)` returns a `now` ref of
  epoch ms, ticking on `setInterval`, cleared in `onBeforeUnmount`. Needed because `LLMView`'s
  existing 30 s timer (`LLMView.vue:26-28`) only refetches while `llmStore.dirtyFlag` is set;
  without an independent tick a countdown rendered from a static `cooldown_until` freezes.
- `formatRemaining(iso, now)` — `"<1 min"`, `"Nm"`, `"Nh"`, `null` once the deadline has passed —
  stays a local function inside `ProviderCard.vue`. It has exactly one caller, the same reason
  `formatDateTime` stays local in `ReceiptCascadeCard.vue`. Keep the SQLite-timestamp tolerance
  used there (`iso.includes("T") ? iso : iso.replace(" ", "T") + "Z"`).
  **`ReceiptCascadeCard.vue` is not touched by this plan.** An earlier revision extracted its
  `formatRelative` into a shared module for the last-call line; with that line gone (§4) the
  extraction has no second caller and would be a refactor with no beneficiary.
- `webapp/src/components/ProviderCard.vue` — add a `now` prop (Number, default `Date.now()`) and
  render the remaining cooldown next to the status badge when `provider.status === 'cooling'`:
  badge text stays `cooling down`, a sibling `.cooldown-left` span shows
  `formatRemaining(provider.cooldown_until, now)`. Default rather than `required`:
  `component-provider-card.test.js` mounts with only `provider` in ~a dozen places, and a
  required prop turns each into a Vue warning for no gain.
- Nothing extra when `cooldown_until` is null or past — `formatRemaining` returns `null` and the
  span is `v-if`-ed out. The badge keeps its server-side precedence (`_derive_status`:
  disabled → no_key → cooling → available); this is display-only.
- `LLMView.vue` — `const now = useNow()`, pass `:now="now"` to every `ProviderCard`.

**The badge must not outlive the deadline.** `status` is derived server-side at fetch time, and
the 30 s timer only refetches while `dirtyFlag` is set — which `refresh()` immediately clears via
`stampFresh()`. In practice the pool is fetched once per mount and then frozen, so a ticking
`now` alone would render `cooling down` with the remainder gone: exactly the
"indistinguishable from broken" state this section exists to remove.

Fix inside the existing timer callback (`LLMView.vue:26-28`): also refresh when any provider has
`status === 'cooling'` and a `cooldown_until` that has passed. One condition, no new watcher, and
it reconciles `status` and the window counters in the same round trip instead of re-deriving the
badge client-side and drifting from `_derive_status`.

This leaves up to one tick (30 s) in which the badge reads `cooling down` with no remainder
beside it. Accepted: closing it means deriving the badge on the client, which is the drift this
section refuses. Half a minute of a stale badge is not the failure mode being fixed — a badge
stuck for the whole mount is.

## 4. `ProviderCard`: the meta row becomes a reliability line

The meta row (`ProviderCard.vue:53-58`) renders `412 calls · last: ok`. Both spans go; one span
takes their place, driven by the §2 fields:

| Condition | Rendered | Tone |
|---|---|---|
| `recent_calls === 0` | `no calls · 7 d` | muted |
| `recent_failures === 0` | `no failures · 7 d` | muted |
| otherwise | `2 failures / 41 · 7 d` | danger |

- The day count comes from `recent_window_days`, never a literal in the template.
- The zero-call case must not fall through to "no failures" — an unused provider is not a healthy
  one, and encoding "no data" as "all good" is the exact confusion this plan removes.
- Drop the `.last-status[data-status=…]` colouring rules with the span they styled; the new line
  carries its own single danger state.

## 5. Explicitly out of scope

- **Which provider is next in the failover order** — the most valuable missing signal, but
  `llmbroker.Optimizer` exposes no routing-order accessor (`wilson_bound`, `is_demoted`,
  `load_scores` only). Deriving it from preset order + status would duplicate broker logic and
  drift. Needs an upstream llmbroker addition.
- **Median latency** (`latency_ms` is in the journal): it shapes expectations after a scan but
  changes no decision on this screen.
- `health.strategy` (`"failover"`, computed in `llm_status`) stays unrendered — the bare word
  tells the user nothing.
- `base_url` stays unrendered.
- No llmbroker change. `snapshot().metrics` keeps its documented cached-tail semantics; dinary
  simply stops reading it.

## 6. Tests

Python (`tests/api/test_admin_llm.py`) — the existing fixtures run the real broker against the
test DB, so seed the journal through the public `llmbroker.sqlite.Store` (`record(Call(…))`),
never with raw SQL against `llmbroker_*`. New class `TestProviderReliability`:
- `recent_calls` / `recent_failures` count only `kind == "call"` rows — a `kind == "quality"`
  record in the same window moves neither;
- rows older than the window are excluded, rows inside it are counted;
- every non-`OK` status counts as a failure, `RATE_LIMITED` included;
- a provider with no rows returns `recent_calls == 0` and `recent_failures == 0`;
- `recent_window_days` is present and matches the window used for filtering;
- the response no longer carries `call_count`, `last_status` or `last_at` — update the field
  inventory in `test_status_provider_fields` (`tests/api/test_admin_llm.py:110-130`) accordingly,
  asserting the three are absent.

Frontend (`webapp/tests/`):
- new `composable-use-now.test.js`: the ref advances on fake timers, the interval is cleared on
  unmount.
- extend `component-provider-card.test.js`: all three reliability-line states from §4, including
  that `recent_calls === 0` renders "no calls" and not "no failures"; `cooling` + future
  `cooldown_until` renders the remaining time while past/null renders no extra span. Every case
  passes an explicit `now` — an assertion resting on the real `Date.now()` would be a
  date-dependent test that rots. Update the shared `BASE_PROVIDER` fixture: `call_count`,
  `last_status` and the pinned `last_at` (`2026-05-10T11:30:00+00:00`) go, the three `recent_*`
  fields arrive.
- **new `component-llm-view.test.js`** — `webapp/tests/` has no `LLMView` test today, so this is a
  file from scratch (mount + stubbed `llm` store): the header no longer contains `.pool-hint` or
  the text `.deploy/llms.toml` while the empty state still does; and, on fake timers, a provider
  whose `cooldown_until` has passed triggers a refetch on the next tick even with `dirtyFlag`
  clear (the §3 carve-out).

## 7. Specs

- `specs/ui/screens.md`, `## LLM view`:
  - drop `from llms.toml` from the mockup header (`:337`);
  - in `### ProviderCard rules`, state that a cooling provider shows how long the cooldown still
    has to run, and that the usage line reports how many of the provider's recent calls failed
    over a fixed window — with "no calls in the window" shown as its own state, distinct from
    "no failures";
  - update the mockup's usage line, which still shows a call count;
  - the section closes with "Refresh polled every 30 s when online", already only half-true (the
    tick refetches only while the store is dirty) and changed again by §3 — restate as: refetch
    on open, and while a provider is cooling, once its deadline passes;
  - the `RECEIPT QUEUE` strip (mockup `:333`, section `### Receipt queue strip` at `:357`) is
    documented under `## LLM view` but implemented in `ReviewView.vue:188-199` — move the section
    and the mockup rows into the Review view section. Pre-existing divergence, fixed here because
    the same mockup is being edited anyway. While moving, fix the first chip: the spec says
    `N ready`, the code renders `N queued` (`ReviewView.vue:196`).
- `specs/reference/llmbroker-integration.md`, `## Admin screen`: the per-provider inventory gains
  the cooldown remainder and the recent-failure ratio, and loses the call count. State the rule
  that the screen reports reliability over a recent window rather than lifetime usage, and that
  the aggregate is derived from the broker's journal accessor — the cached snapshot metrics are
  not read.

## Work order and done gate

1. Backend window aggregate (§2) + Python tests — the frontend has nothing to render without it.
2. Drop the pool hint (§1).
3. `useNow.js`, `ProviderCard` cooldown + reliability line, `LLMView` tick fix (§3, §4).
4. Frontend tests (§6), specs (§7).
5. Gate: `uv run inv pre` → "All checks passed!", `uv run pytest` → `N passed`,
   `cd webapp && npm test` green. `inv pre` after every batch.

Affected files: `src/dinary/api/controllers/llm.py`, `tests/api/test_admin_llm.py`,
`views/LLMView.vue`, `components/ProviderCard.vue`, new `composables/useNow.js`, webapp tests,
2 specs.

## Open questions (defaults)

- Window = 7 days. Long enough to cover a quiet week of receipts, short enough that a provider
  that has recovered stops being blamed for it.
- Cooldown countdown granularity = 30 s (`useNow` default), matching the view's existing poll
  interval. A finer tick buys nothing on a minutes-long cooldown.
</content>
</invoke>
