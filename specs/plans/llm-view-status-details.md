# Plan: LLM view — drop the deploy-path hint, surface cooldown and last-call time

## Task

The LLM view drops the deploy-path hint from the pool header and starts showing the two fields
the status API already returns but the UI discards: the cooldown deadline and the time of the
last call.

## Context

Pure frontend. `GET /api/llm/status` already emits `cooldown_until` and `last_at`
(`_snapshot_to_dict`, `src/dinary/api/controllers/llm.py:39-53`); neither reaches the screen. No
controller, SQL or Python-test changes.

Independent of the two `pwa-analytics-*` plans — no shared files.

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

## 2. Shared time helpers

`formatRelative` exists only as a local function inside
`webapp/src/components/ReceiptCascadeCard.vue:34-43`. Extract, do not copy (CLAUDE.md forbids
duplicating logic to avoid an import).

- New `webapp/src/composables/time.js`:
  - `formatRelative(iso, now)` — moved verbatim, with `Date.now()` replaced by an injected `now`
    (default `Date.now()`) so a ticking ref can drive it.
  - `formatRemaining(iso, now)` — the mirror for a future deadline: `"<1 min"`, `"Nm"`, `"Nh"`;
    returns `null` once the deadline has passed.
  - Both keep the existing SQLite-timestamp tolerance
    (`iso.includes("T") ? iso : iso.replace(" ", "T") + "Z"`).
- `ReceiptCascadeCard.vue` — delete the local `formatRelative`, import it. `formatDateTime` stays
  local (single user).
- New `webapp/src/composables/useNow.js` — `useNow(intervalMs = 30_000)` returns a `now` ref of
  epoch ms, ticking on `setInterval`, cleared in `onBeforeUnmount`. Needed because `LLMView`'s
  existing 30 s timer (`LLMView.vue:26-28`) only refetches while `llmStore.dirtyFlag` is set;
  without an independent tick a countdown rendered from a static `cooldown_until` freezes.

`composables/`, not a new `utils/`: pure non-composable helpers already live there
(`composables/receipt.js`, `composables/swHealth.js`).

## 3. `ProviderCard`: cooldown deadline

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
it reconciles `status`, `call_count` and `last_at` in the same round trip instead of re-deriving
the badge client-side and drifting from `_derive_status`.

This leaves up to one tick (30 s) in which the badge reads `cooling down` with no remainder
beside it. Accepted: closing it means deriving the badge on the client, which is the drift this
section refuses. Half a minute of a stale badge is not the failure mode being fixed — a badge
stuck for the whole mount is.

## 4. `ProviderCard`: when the last call happened

- The meta row (`ProviderCard.vue:53-58`) renders `412 calls · last: ok`. Append the relative
  time from `provider.last_at`: `412 calls · last: ok · 3m ago`.
- `v-if` on `provider.last_at` — a provider with `call_count === 0` has none.
- Keep the existing `[data-status]` colouring on `.last-status`; the timestamp is a separate
  muted span so an error status stays visually distinct.

Rationale: `last: ok` from two weeks ago and from a minute ago look identical today, yet they are
"the pipeline is dead" vs "the pipeline is working".

## 5. Explicitly out of scope

- **Which provider is next in the failover order** — the most valuable missing signal, but
  `llmbroker.Optimizer` exposes no routing-order accessor (`wilson_bound`, `is_demoted`,
  `load_scores` only). Deriving it from preset order + status would duplicate broker logic and
  drift. Needs an upstream llmbroker addition.
- `health.strategy` (`"failover"`, computed in `llm_status`) stays unrendered — the bare word
  tells the user nothing.
- `base_url` stays unrendered.

## 6. Tests (`webapp/tests/`)

- new `composable-time.test.js` (the directory names composable tests `composable-*.test.js`):
  `formatRelative` boundaries (just now / m / h / d) and the SQLite-timestamp form;
  `formatRemaining` returns `null` for a past deadline and the right bucket for future ones.
- new `composable-use-now.test.js`: the ref advances on fake timers, the interval is cleared on
  unmount.
- extend `component-provider-card.test.js`: `cooling` + future `cooldown_until` renders the
  remaining time; past/null renders no extra span; `last_at` renders the relative suffix;
  `call_count === 0` with no `last_at` renders neither. Every case passes an explicit `now` — the
  shared `BASE_PROVIDER` fixture pins `last_at` to `2026-05-10T11:30:00+00:00`, so an assertion
  resting on the real `Date.now()` would be a date-dependent test that rots.
- **new `component-llm-view.test.js`** — `webapp/tests/` has no `LLMView` test today, so this is a
  file from scratch (mount + stubbed `llm` store): the header no longer contains `.pool-hint` or
  the text `.deploy/llms.toml` while the empty state still does; and, on fake timers, a provider
  whose `cooldown_until` has passed triggers a refetch on the next tick even with `dirtyFlag`
  clear (the §3 carve-out).
- `ReceiptCascadeCard` must stay green after `formatRelative` moves out — `ReceiptCascadeCard.test.js`
  exists; extend it if the relative-time line is untested.

## 7. Specs

- `specs/ui/screens.md`, `## LLM view`:
  - drop `from llms.toml` from the mockup header (`:337`);
  - in `### ProviderCard rules`, state that a cooling provider shows how long the cooldown still
    has to run, and that the usage line carries the time of the last call;
  - the section closes with "Refresh polled every 30 s when online", already only half-true (the
    tick refetches only while the store is dirty) and changed again by §3 — restate as: refetch
    on open, and while a provider is cooling, once its deadline passes;
  - the `RECEIPT QUEUE` strip (mockup `:333`, section `### Receipt queue strip` at `:357`) is
    documented under `## LLM view` but implemented in `ReviewView.vue:188-199` — move the section
    and the mockup rows into the Review view section. Pre-existing divergence, fixed here because
    the same mockup is being edited anyway. While moving, fix the first chip: the spec says
    `N ready`, the code renders `N queued` (`ReviewView.vue:196`).
- `specs/reference/llmbroker-integration.md`, `## Admin screen`: the per-provider inventory gains
  the cooldown remainder and the last-call time.

## Work order and done gate

1. Extract `composables/time.js` + `useNow.js`, repoint `ReceiptCascadeCard`.
2. Drop the pool hint (§1).
3. `ProviderCard` cooldown + last-call lines, `LLMView` tick fix (§3, §4).
4. Tests (§6), specs (§7).
5. Gate: `uv run inv pre` → "All checks passed!", `uv run pytest` → `N passed`,
   `cd webapp && npm test` green. `inv pre` after every batch.

Affected files: `views/LLMView.vue`, `components/ProviderCard.vue`,
`components/ReceiptCascadeCard.vue`, new `composables/time.js`, new `composables/useNow.js`,
webapp tests, 2 specs.

## Open questions (defaults)

- Cooldown countdown granularity = 30 s (`useNow` default), matching the view's existing poll
  interval. A finer tick buys nothing on a minutes-long cooldown.
