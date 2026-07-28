# Problem: LLM key resolution for analytics on a separate machine

No solution chosen yet. This file states the problem only.

## The problem

`inv analytics` resolves LLM keys from two files on the machine it runs on: `.deploy/llms.toml`
for each provider's `api_key_ref`, and `.deploy/.env` for the value stored under that ref
(`tasks/analytics.py:40-67` — the values come from the env *file* via `dotenv_values`, not from
the process environment). If either file is missing the task exits with a clear error.

That works while analytics runs on the machine that holds the deploy config. It breaks when
analytics runs somewhere that has only the analytics package installed — the operator has to
reproduce two deploy files, one of which carries secrets, on every such machine.

## Constraints any solution has to respect

- **Analytics never calls the running dinary server** — a stated architectural decision
  (`specs/reference/analytics-ai.md`, "LLM strategy"). Fetching keys from a dinary endpoint is
  not a free option; it would need that decision revisited first.
- **The server's own keys are not reachable from analytics.** Since the llmbroker 1.3.0 upgrade
  the broker owns its key storage inside its own `llmbroker_`-prefixed schema; dinary's former
  `llmbroker_secrets` table was dropped (`0002_llmbroker_1_3_0_upgrade.sql`). There is no dinary
  table to read keys from, and no established way to export values out of llmbroker.
- **Anything that moves key values over the network needs an auth layer** the app does not have
  today.

## Status

Deferred until analytics actually has to run on a separate machine. Until then the two-file
requirement is acceptable.
