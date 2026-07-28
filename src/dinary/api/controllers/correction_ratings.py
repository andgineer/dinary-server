"""Delayed model-quality ratings derived from user category corrections."""

import json
import logging
import sqlite3

import llmbroker

from dinary.background.classification.receipt_classifier import CLASSIFICATION_OPERATION

logger = logging.getLogger(__name__)

#: A model's classification rule corrected to one of its own proposed
#: alternatives earns partial credit; any other target is a full negative.
_PARTIAL_CREDIT_SCORE = 0.5
_FULL_NEGATIVE_SCORE = 0.0


def _rating_for_rule_row(
    row: sqlite3.Row | None,
    corrected_to_category_id: int,
) -> tuple[str, float] | None:
    """Rate the model that created an llm-sourced rule now being corrected.

    Returns ``(llm_name, score)`` when the rule is ``source='llm'`` with a known
    model: partial credit if the corrected-to category was one of that model's
    own proposed alternatives, else a full negative. Returns ``None`` for
    user-sourced rules, rules with no recorded model, or a correction that just
    re-affirms the model's own primary category (a confirmation, not a miss) —
    those are never rated. Must be read before the write flips the rule to
    ``source='user_correction'`` (which is what dedups repeated corrections).
    """
    if row is None or row["source"] != "llm" or not row["llm_name"]:
        return None
    if corrected_to_category_id == row["category_id"]:
        return None
    alternatives: list[int] = []
    if row["alternative_category_ids"]:
        try:
            alternatives = [int(a) for a in json.loads(row["alternative_category_ids"])]
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning(
                "corrupt alternative_category_ids for rule on %r",
                row["item_name_normalized"],
            )
    score = (
        _PARTIAL_CREDIT_SCORE if corrected_to_category_id in alternatives else _FULL_NEGATIVE_SCORE
    )
    return str(row["llm_name"]), score


def pending_rating_for_item(
    con: sqlite3.Connection,
    chain_id: int | None,
    item_name_normalized: str,
    corrected_to_category_id: int,
) -> tuple[str, float] | None:
    row = con.execute(
        """
        SELECT item_name_normalized, source, llm_name, category_id, alternative_category_ids
          FROM classification_rules
         WHERE (chain_id IS ? OR (chain_id IS NULL AND ? IS NULL))
           AND item_name_normalized = ?
        """,
        [chain_id, chain_id, item_name_normalized],
    ).fetchone()
    return _rating_for_rule_row(row, corrected_to_category_id)


def pending_rating_for_rule(
    con: sqlite3.Connection,
    rule_id: int,
    corrected_to_category_id: int,
) -> tuple[str, float] | None:
    row = con.execute(
        """
        SELECT item_name_normalized, source, llm_name, category_id, alternative_category_ids
          FROM classification_rules
         WHERE id = ?
        """,
        [rule_id],
    ).fetchone()
    return _rating_for_rule_row(row, corrected_to_category_id)


async def record_correction_ratings(
    broker: llmbroker.AsyncBroker | None,
    pending_ratings: list[tuple[str, float]],
) -> None:
    """Record delayed quality ratings after the correction transaction commits.

    A rating failure must never fail the correction — log and continue.
    """
    if broker is None:
        return
    for llm_name, score in pending_ratings:
        try:
            await broker.record_quality(llm_name, CLASSIFICATION_OPERATION, score)
        except Exception:
            logger.exception(
                "record_quality failed for llm_name=%s score=%s — continuing",
                llm_name,
                score,
            )
