#!/bin/bash
# Pre-install the DuckDB extensions the analytics code auto-loads at runtime.
#
# `ATTACH ... (TYPE sqlite)` makes DuckDB fetch `sqlite_scanner` from
# extensions.duckdb.org the first time it runs on a machine. When that
# download stalls it blocks inside the ATTACH, the 60s pytest-timeout fires,
# and the interrupted query surfaces as an unrelated-looking
# `RuntimeError: Query interrupted` in whichever analytics test ran first.
# Installing the extension up front keeps the network out of the test run.
#
# Idempotent and non-interactive: safe to re-run any time.
#
# Usage:  bash scripts/install-duckdb-extensions.sh
set -euo pipefail

cd "$(dirname "$0")/.."

python_cmd="./.venv/bin/python"
[ -x "$python_cmd" ] || python_cmd="python"

timeout_cmd=()
command -v timeout >/dev/null 2>&1 && timeout_cmd=(timeout 120)

for attempt in 1 2 3; do
  if "${timeout_cmd[@]}" "$python_cmd" -c \
    "import duckdb; con = duckdb.connect(':memory:'); con.execute('INSTALL sqlite'); con.execute('LOAD sqlite')"; then
    echo "install-duckdb-extensions: sqlite extension ready"
    exit 0
  fi
  echo "install-duckdb-extensions: attempt $attempt failed, retrying in 5s" >&2
  sleep 5
done

echo "install-duckdb-extensions: could not install the DuckDB sqlite extension" >&2
exit 1
