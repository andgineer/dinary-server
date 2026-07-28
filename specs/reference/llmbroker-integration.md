# llmbroker integration

dinary uses `llmbroker` as an external PyPI dependency for all LLM access. One
`AsyncBroker` instance lives for the full FastAPI application lifetime; a short-lived
synchronous `Broker` serves the analytics chat, which reads providers straight from
the preset file and never touches the server's database.

## Broker design

The broker knows nothing about receipts or categories. It accepts OpenAI-style
messages and returns a reply. All receipt and category business logic lives in the
classification layer above it — see
[classification-pipeline.md](classification-pipeline.md) for how the pipeline uses
the broker.

The server process backs the broker with SQLite: the provider registry, call
telemetry, API-key secrets, the persistent user-disable latch, and the model-quality
learning window all live in `llmbroker_`-prefixed tables that the package creates and
migrates itself. dinary never issues SQL against those tables.

## Provider catalog: preset file is the single source of truth

`.deploy/llms.toml` is the one place the provider list is defined. On every startup —
at the same point where dinary's own schema is migrated — the running registry is
reconciled to the file: providers added, changed, or removed in the file appear,
change, or disappear in the pool. There is no other write path: no API and no UI can
add, edit, or delete a provider.

`.deploy/llms.toml` is the local operator config (gitignored); `.deploy.example/llms.toml`
is the committed template. Generate it from a curated preset rather than authoring it
by hand:

```bash
llmbroker preset freetier > .deploy/llms.toml
llmbroker env .deploy/llms.toml >> .deploy/.env
```

The second command appends the env-var stubs the preset needs; fill in the actual keys
before starting the server.

## API keys

For the server, API keys live in the database. `.deploy/.env` is only the bootstrap
source: on startup, a key that is not yet resolvable in the database is seeded from its
env var, and from then on the database copy is authoritative — later env changes do not
overwrite it. Changing an already-seeded key is a manual database operation.

The analytics chat is the exception: it never touches the server's database, so its
broker resolves keys from its own process environment. The dashboard launcher exports
every key it can resolve from `.deploy/.env` under the exact ref names the preset file
declares. A key changed only in the database therefore does not reach analytics; its
broker state (cooldowns, quality, the user disable) is likewise separate from the
server's, so a provider disabled on the LLM screen stays available to the chat.

A provider whose key cannot be resolved is reported on the LLM screen with the
onboarding hint carried in the preset (how and where to get the key).

## Admin screen: read-only status plus a user disable

The LLM screen is read-only except for one control. For every provider it shows
availability (available, cooling down, no key, or disabled by the user), usage counters
and the last call status, and the model's quality of work — whether it is demoted for
receipt classification, plus a numeric quality indicator once ratings exist.

The one mutation is a persistent disable: the user can disable a provider and re-enable
it later. The verdict is stored by llmbroker, survives restarts and preset reloads, and
excludes the provider from routing until it is explicitly re-enabled.

## Model quality learning

Quality feedback for receipt classification flows back to the model that did the work:

- A classification reply that dinary accepts counts immediately as a positive rating.
- A malformed reply counts as a negative rating.
- A user category correction rates the model that created the corrected rule, whenever
  the correction happens — days later, across restarts. The rule remembers which model
  created it. Re-selecting the category the model already chose confirms it and is not
  rated at all. Correcting to one of the alternatives that model itself proposed earns
  partial credit; any other target is a full negative. Because the correction flips the
  rule away from its llm origin, a second correction of the same rule rates nothing.

Rules created directly from user corrections carry no model and are never rated.

Rating is a side effect, never a precondition: a failure to record one is logged and
the correction stands.

## Version pinning

dinary pins `llmbroker` with an exact version (`==`). The package evolves actively and
breaking API changes between minor versions are expected. Bump deliberately: update the
pin, run the full test suite, commit.
