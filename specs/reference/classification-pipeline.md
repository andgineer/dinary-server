# Classification Pipeline — Architecture

## No DB connections during HTTP

The pipeline never holds an open database connection while making a network call.
Connections are opened for synchronous SQLite work, closed before any `await`
that touches the network, then re-opened for the next write phase. This prevents
connection contention under concurrent job processing.

## Confidence rules

**Rule-hit threshold**: a stored rule with `confidence_level = 1` is treated as
a miss and its item is re-queued for LLM classification. Confidence-1 means "seen
but uncertain" — the rule exists only to record normalised name history, not to
make a definitive assignment.

**Confidence penalty**: if a provider failed mid-loop and a fallback provider
was used, a penalty is applied to LLM-classified items' confidence. Rule-hit items are not penalised — their stored confidence is already
calibrated from a prior LLM call. The penalty signals that the result comes from
a less reliable source than the configured primary.

**Always-unconditional alternatives**: the system prompt always requests
alternative categories, regardless of confidence. Gating on confidence was tried
and caused the model to inflate confidence-4 scores to avoid the extra work of
listing alternatives. Requiring them unconditionally keeps confidence calibrated.

**User corrections set confidence 4 and clear alternatives**: a user correction
is the highest authority. Clearing stored alternatives prevents stale LLM
suggestions from surfacing in future review flows after the user has expressed a
definitive preference.

**Rule creation threshold**: rules are created for any item with a known
category, regardless of confidence level. A confidence-1 rule records the
normalised name but is still treated as a miss for re-queuing — the item is
sent to the LLM again on next classification, so a definitive assignment can
eventually replace it.

## Error handling strategy

Network/parse errors on receipt fetch are treated differently:
- Transient errors — network failures and a not-yet-indexed receipt (SUF
  returns no items via either fetch path) — release the job for retry later,
  with no retry ceiling.
- Structural parse errors (the response itself is malformed, not just empty):
  poison the job.

This distinction matters because the government fiscal APIs are unreliable;
treating all failures as permanent would silently discard valid receipts. See
[receipt-fetching.md](receipt-fetching.md) for the fetch strategy and
reliability characteristics of the Serbian and Montenegrin fiscal services.

A poisoned job — or one stuck retrying indefinitely — is not a dead end; see
"Manual resolution" below.

## Manual resolution

A receipt sitting in `pending`, `in_progress`, or `poisoned` status can be
converted into an expense manually at any time, regardless of why it's stuck.
The user picks the category (and optionally tags, an event, and a comment);
the amount and purchase date come from the receipt's QR payload (see
[receipt-fetching.md](receipt-fetching.md#qr-payload-as-amountdate-source)),
not from SUF, so this works even for receipts SUF has never returned data for.

The resulting expense is recorded at confidence level 4 (user-provided, the
highest level) with no associated classification rule. This guarantees a
receipt accepted by the API never stays unprocessed forever, independent of
the cause of the stall.

## LLM execution failure and retry

An LLM execution is considered failed when: the response is not valid JSON,
any classified item has no assigned category, or the count of classified items
differs from the count sent. A fully assigned response at any confidence level
is not a failure.

On execution failure the pipeline retries with a different provider, up to
three attempts bounded by the number of configured providers. Both outcomes are
reported back as model quality — a failure negatively, an accepted response
positively (see [llmbroker-integration.md](llmbroker-integration.md)).
If all attempts fail, the job enters the frequency-based exhaustion fallback
rather than being poisoned.

## Frequency-based exhaustion fallback

After all provider attempts are exhausted, every unclassified item receives a
best-guess assignment drawn from the most-frequently-used categories over the
last 3 months. The top category becomes the primary at confidence 1; the next
five become alternatives. This lands the receipt in the review queue pre-filled
for the user rather than silently losing it.

Padding: when fewer than six categories appear in recent expense history, the
list is extended with active categories (ordered by ID) until six entries are
reached.

If fewer than 5 active categories exist the fallback is treated as a transient
error and retried under the existing job retry policy (15-minute retries for a
day, then once daily). This prevents a broken installation from silently
discarding receipts.

## Sync DB calls on the event loop

Every sync DB call that involves `BEGIN IMMEDIATE` is wrapped in
`asyncio.to_thread`. SQLite's write lock wait (up to `busy_timeout`, 5 s) would
block the event loop and freeze all API responses if called directly from a
coroutine. This matters in two cases: two receipts processed in parallel both
reaching the persist step simultaneously, and an API write racing with the
classification drain.

Sheet logging already used `asyncio.to_thread` for the same reason; the
classification drain follows the same pattern.

## FastAPI routes are plain `def`

Receipt and expense route handlers are plain `def` (synchronous), not
`async def` wrapping `asyncio.to_thread`. FastAPI automatically runs `def`
handlers in its thread pool — the `async def + to_thread` double-hop adds a
pointless context switch with no benefit.

The consequence of plain `def` routes is that `notify_new_receipt()` (which
wakes the classification drain) must use `loop.call_soon_threadsafe()` rather
than calling `asyncio.Event.set()` directly. `asyncio.Event.set()` is not
thread-safe; calling it from a thread-pool worker races with the event loop.
`notify_new_work()` in sheet logging uses the same pattern.

## Provider error diagnostics

The call log stores the first 300 characters of the HTTP response body on any
non-2xx provider response. When a provider has a bad API key, exhausted quota,
or a billing block, the error text surfaces in the admin UI without requiring
the operator to dig through server logs.

## Parallelism cap

No cap on concurrent job processing. At typical load the realistic backlog is
tens of jobs; a bounded semaphore would add complexity without providing
meaningful protection.

## Auto-attach events

When a job is processed, the pipeline checks for an active event whose date range
covers the receipt's purchase timestamp and automatically attaches it to every
expense from that receipt. This means vacation or travel events tag all purchases
without requiring the user to manually attach them in the review flow.

## LLM provider dispatch

Provider selection, failover, rate-limit tracking, and storage implementations
are covered in [llm-providers.md](llm-providers.md).

## Classification rules attach to chains, not stores

A rule learned at one branch of a retail chain applies to all branches. Storing
rules at store granularity would waste LLM calls and leave new branches without
coverage until they accumulated their own history. See
[stores.md](stores.md) for the store/chain data model.
