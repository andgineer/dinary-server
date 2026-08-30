"""Shared types for fiscal-receipt parsing (country-agnostic).

The Serbian and Montenegrin parsers both produce these structures and raise
this error taxonomy; the dispatch layer and the DB layer consume them. Kept in
a dependency-free leaf module so the parsers and the dispatcher can import it
without an import cycle.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


class ParserRequestError(Exception):
    """Raised when a receipt fetch fails due to a network or HTTP error (transient)."""


class ParserParseError(Exception):
    """Raised when a receipt cannot be parsed due to unexpected content (permanent)."""


class ParserNotIndexedError(Exception):
    """Raised when the fiscal service returns no receipt data — the receipt is
    likely not indexed yet (transient; resolves once the service processes it)."""


@dataclass(slots=True)
class ReceiptItem:
    name_raw: str
    unit_price: float
    quantity: float
    total_price: float
    tax_label: str


@dataclass(slots=True)
class ParsedReceipt:
    store_name: str
    store_pib: str
    total_amount: float
    invoice_number: str
    items: list[ReceiptItem]
    items_total: float
    total_ok: bool
    used_journal_fallback: bool = False
    purchase_datetime: str | None = None
    journal_validation_errors: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class QrPayload:
    amount: Decimal
    purchase_datetime: datetime  # tz-aware
