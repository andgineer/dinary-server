# Google Sheets Integration

The sheets layer provides runtime sheet logging — an ongoing append-only drain that
writes new expenses to the spreadsheet as they are classified. Enabled when the
logging spreadsheet is configured.

## Year-aware row matching

The logging spreadsheet accumulates expenses across multiple years but column G
stores only the month number. When reading back display values over the Sheets
API, the year is dropped from formatted date strings. Column A stores the full
date as a serial value, which must be fetched with the raw-value format to
recover the year. All row-matching helpers operate on year+month pairs, with a
month-only fallback for unit tests and single-year diagnostics.

## Map tab as 3D→2D resolver

Each expense has three classifying dimensions: category, event, and tags.
The destination spreadsheet is organised in 2D (category rows, envelope columns).
The `map` worksheet resolves this: each row specifies a (category, event, tags)
match pattern and the target sheet category and envelope. Rows are evaluated
top-to-bottom; the first match wins for each output dimension.

Fallback when no row matches: use the raw category name as the sheet category
and an empty envelope.

## Atomic reload with error protection

When the map tab is reloaded (e.g. after the operator edits it), the derived DB
tables are swapped atomically. A parse error in the new map tab leaves the
previous tables in place — the system continues operating with the last known
good mapping rather than failing open with an empty or corrupt one.

## Logging column layout

Each row in the logging spreadsheet covers one expense. Columns:

| Col | Content | Notes |
|-----|---------|-------|
| A | First day of the expense's month (`YYYY-MM-DD`, `USER_ENTERED`) | Google displays as `Apr-1`; underlying date serial retains the year |
| B | RSD amount (sum formula extended per append) | If `currency_original == "RSD"`, `amount_original` is used verbatim; otherwise `amount` (accounting currency) is converted to RSD at the NBS rate for the expense date |
| C | EUR conversion formula `=IF(H{r}="","",B{r}/H{r})` | Sheet-side approximation; not reconciled against `expenses.amount` |
| D | `sheet_category` — resolved by the drain via `sheet_mapping` | Fallback: `categories.name` |
| E | `sheet_group` (envelope) — resolved by the drain via `sheet_mapping` | Fallback: empty string |
| F | Free-text comment | Semicolon-separated when multiple expenses share a row |
| G | Month number 1–12 (literal) | Used for fast month-block scans; year-aware matching uses column A |
| H | Manual EUR↔RSD rate cell | Written only when empty (set-if-missing) |
| J | Idempotency marker — see below | |

Column B is always RSD regardless of the accounting currency, so switching `accounting_currency` does not invalidate historical sheet values.

## Read quota is the drain's binding constraint

The Sheets API caps read requests per minute per service account, and a single receipt
expands into one queue row per item. The drain therefore reads the grid once per sweep and
reuses that snapshot for every row, rather than re-reading per row: read volume scales with
the number of sweeps, not with the number of expenses. The drain is the spreadsheet's only
writer, so the snapshot can only go stale on a manual edit made mid-sweep.

## Transient vs permanent failures

A rate limit is a client error by HTTP status but self-heals with time, so it takes the
circuit-breaker backoff path, never the poison path. This distinction matters because
poisoning is terminal: nothing retries a poisoned row, and the expense stays out of the
spreadsheet until an operator requeues it.

Poisoned rows are therefore reported by the healthcheck as a whole-queue count. Watching
only the newest expense would let the alert clear itself as soon as any later expense logged
successfully, hiding the stuck rows permanently.

## Idempotency — column J

The append path is at-least-once: a Sheets API call may succeed on the server even if the response is never received (network timeout). Column J holds the `client_expense_id` UUID of the most recent expense appended to that row. Before each append the drain reads J; if it already equals the incoming UUID, the write is skipped entirely.

**Last-key-only**: each successful append overwrites the previous J value with the new UUID. The cell size stays bounded (one UUID regardless of how many expenses share the row) at the cost of not recovering the full contributor list from the sheet — the SQLite ledger is the source of truth for that.

A queue row whose expense has `client_expense_id = NULL` is marked `poisoned` rather than falling back to a synthetic marker. Writing a non-UUID value into J would corrupt duplicate detection for all subsequent appends to the same row. A `NULL` UUID in the drain is always a producer bug, not a normal state. See `src/dinary/background/sheet_logging/` for the implementation.
