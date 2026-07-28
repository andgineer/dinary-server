-- Schema side of the llmbroker 0.0.11 -> 1.3.0 upgrade: drop the legacy LLM
-- tables, then let classification rules remember the model that created them.
--
-- There is a single dinary installation and no llmbroker data survives the
-- upgrade to llmbroker 1.3.0. Dropping the tables here — before the broker is
-- constructed in the app lifespan — lets the new llmbroker recreate its own
-- (llmbroker_-prefixed) schema from scratch on first use: the provider list
-- rebuilds from .deploy/llms.toml, API keys re-seed from .deploy/.env,
-- telemetry and quality windows start empty.
--
-- Going forward llmbroker owns and migrates its own tables; this is a
-- one-time legacy cleanup, not a pattern.

DROP TABLE IF EXISTS llmbroker_registry;
DROP TABLE IF EXISTS llmbroker_calls;
DROP TABLE IF EXISTS llmbroker_secrets;
DROP TABLE IF EXISTS llmbroker_state;

-- The last two predate the llmbroker package — dinary owned provider management
-- itself back then, keeping API keys in plaintext. llmbroker 1.3.0 never reads
-- them, so on a database old enough to have them they would linger as dead rows.
DROP TABLE IF EXISTS llmbroker_providers;
DROP TABLE IF EXISTS llmbroker_call_log;

-- llmbroker's sqlite backend tracks its schema version in the file-global
-- PRAGMA user_version. Version 0.0.11 stamped it to 1; llmbroker 1.3.0 accepts
-- only 0 or its own version and otherwise raises "schema version 1 found …".
-- DROP TABLE does not clear a header value, so reset it here to hand a fresh
-- database to the new llmbroker, which re-stamps it when it recreates its schema.
PRAGMA user_version = 0;

-- Remember which model created each llm-sourced classification rule, so a later
-- user correction can rate that model's quality (delayed negative feedback).
-- Nullable: existing rows and user-created rules stay NULL and are never rated.
ALTER TABLE classification_rules ADD COLUMN llm_name TEXT;
