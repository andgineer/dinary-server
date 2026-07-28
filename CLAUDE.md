# Dinary — Codebase Guide

## Project overview

Expense-tracking app for Serbia: scan fiscal receipts via QR code, classify items with an LLM, and sync to Google Sheets. Consists of a FastAPI backend and a Vue 3 PWA frontend.

For architecture, system layout, technology decisions, data model, deployment, and configuration see [specs/reference/architecture.md](specs/reference/architecture.md).

---

## Key commands

| Task | Command |
|---|---|
| Lint + type-check + format | `uv run inv pre` |
| Python tests | `uv run pytest` |
| Dev server (auto-reload) | `uv run inv dev` |
| Build Vue PWA into `_static/` | `uv run inv build-static` |
| Apply DB migrations (local) | `uv run inv migrate` |
| Frontend tests | `cd webapp && npm test` |
| List all tasks | `uv run inv --list` |

**Never call ruff directly.** Always use `inv pre` — it runs ruff, ruff-format, pyrefly, and pre-commit hygiene hooks in the correct order.

---

## Environment setup

A bare `uv sync` is **not** enough to reach a green suite. The full test run and `inv pre` also need:

1. **All dependency groups**: `uv sync --all-groups` — the `analytics` group (duckdb, lmdb, marimo, mcp, altair, polars) is required, or `tests/analytics/` fails to collect and pyrefly reports missing-import errors.
2. **`zstd` and `sqlite3` CLIs**: the backup/restore tasks and tests shell out to them (`apt-get install -y zstd sqlite3`).

Both are wrapped in the tracked, idempotent script **`scripts/setup-test-env.sh`** — run it once on a fresh session (or whenever deps/binaries are missing) before anything else; never work around the gap by skipping tests. (`.claude/` is git-ignored, so a SessionStart hook can't be committed — this script is the tracked source of truth.)

---

## Non-negotiable done gate

Before claiming anything is done, both must be green:

1. `uv run inv pre` → "All checks passed!" + `0 errors` from pyrefly
2. `uv run pytest` → `N passed` with zero failures or errors

Run `inv pre` after each discrete batch of changes, not only at the end.

**Never leave a failing test.** Every session starts from green (main is green). There is no "pre-existing failing test" to ignore: red is either something you broke or a test that rotted — most often a **flaky or date-dependent** test (e.g. a hardcoded date aged out of a rolling `datetime('now', …)` window). Fix the root cause so it is deterministic; do not skip, `xfail`, or defer it. If the cause is a missing dependency or binary, fix the environment (see above), not the assertion.

---

## Code conventions

These rules come from `AGENTS.md` and supplement the defaults in this file.

### Language
- All comments, docstrings, plan files, and in-repo docs: **English only**.
- Data literals (category names, sheet headers, envelope names like `"командировка"`) stay in their **original script** (Cyrillic, etc.) — do not transliterate.
- Reply to the user in whichever language they used.

### Imports
- **No local (in-function) imports.** All imports at module top level, always.
- **No `from __future__ import annotations`** — the project targets Python 3.13+.
- **No re-export patterns** — when a symbol moves, update importers to point at the new module instead of leaving a shim re-export. This does not forbid importing a shared helper; never duplicate logic across modules to "avoid" an import.

### Plan files
- **Location: `specs/plans/` — always.** Before creating a plan file, check the path starts with `specs/plans/`. Never `.plans/`, never the repo root, never any hidden or git-ignored directory. A plan outside `specs/plans/` is a bug: move it there in the same session.
- **Language: English — always.** Headings, tables, and prose included. A plan written while the conversation is in Russian (or any other language) is still written in English; only the reply to the user follows the user's language.
- A plan that is fully implemented and merged is deleted, not archived. Before deleting, move anything spec-worthy (architectural decisions, business requirements) into `specs/` — never implementation details.
- Never reference plan files or step numbers from plans inside code comments or docstrings. Plans are ephemeral; the code is the source of truth.

### Spec files
- Specs in `specs/` capture architectural decisions and business requirements only — not implementation details.
- Correct spec content: "every expense created from a receipt must have a matching rule", "retry every 15 minutes on day 1, then once a day indefinitely."
- Never put function signatures, argument lists, field names, or internal class structure in specs. The code is the source of truth for those.
- Specs describe **current state only**. Motivation, experiments, and rationale are welcome. Never track implementation changes ("previously X, now Y", "approach Z was removed") — state only the current rule. Git history records the evolution.
- **Specs must never link to plan files.** `specs/reference/` and `specs/ui/` may only link to other spec files.

### Tests
- Every new function needs tests in the same session. Never skip.

### Linting
- `inv pre` is the only gate. Never run ruff directly; never bypass hooks with `--no-verify`.
