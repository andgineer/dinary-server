"""Tests for the ``_drain_one_job`` per-row contract. Sibling files cover derive
(``test_sheet_logging_derive.py``), drain happy-path (``test_sheet_logging_drain.py``),
and idempotency/circuit-breaker (``test_sheet_logging.py``)."""

from unittest.mock import patch

import allure
import pytest

from dinary.db import storage
from dinary.background.sheet_logging import sheet_logging
from dinary.background.sheet_logging.logging_jobs import list_logging_jobs

from _sheet_logging_helpers import (  # noqa: F401  (autouse + fixtures)
    _reset_backoff,
    data_dir,
    setup,
    sheet_view,
)


@allure.epic("Sheets Sync")
@allure.feature("Sheet logging")
@allure.story("Drain one job")
class TestDrainOneJobReturnContract:
    @patch("dinary.background.sheet_logging.sheet_logging.get_rate", return_value="117.0")
    @patch("dinary.background.sheet_logging.sheet_logging.ensure_category_row")
    @patch(
        "dinary.background.sheet_logging.sheet_logging.append_expense_atomic",
        side_effect=RuntimeError("simulated sheet failure"),
    )
    def test_append_failure_re_raises_and_releases_claim(
        self,
        _aea,
        mock_ecr,
        _gr,
        setup,
        sheet_view,
    ):
        """``_drain_one_job`` re-raises on append failure so
        ``drain_pending`` can classify the error as transient/permanent.
        The claim must be released so the next sweep can retry."""
        mock_ecr.return_value = (3, sheet_view.all_values)

        expense_pk = setup
        with pytest.raises(RuntimeError, match="simulated sheet failure"):
            sheet_logging._drain_one_job(expense_pk, view=sheet_view)

        # Queue row remains ``pending`` (claim released) so the next
        # sweep retries.
        con = storage.get_connection()
        try:
            assert list_logging_jobs(con) == [expense_pk]
        finally:
            con.close()


@allure.epic("Sheets Sync")
@allure.feature("Sheet logging")
@allure.story("Drain one job")
class TestDrainOneJobClaimStolen:
    """After an already-appended row's claim is stolen, must force-delete the
    queue row and surface ``RECOVERED_WITH_DUPLICATE`` (distinct from ``FAILED``,
    so the sweep summary distinguishes "audit the sheet" from "retry pending")."""

    @patch("dinary.background.sheet_logging.sheet_logging.get_rate", return_value="117.0")
    @patch("dinary.background.sheet_logging.sheet_logging.ensure_category_row")
    @patch("dinary.background.sheet_logging.sheet_logging.append_expense_atomic", return_value=True)
    def test_force_delete_after_stolen_claim(
        self,
        _aea,
        mock_ecr,
        _gr,
        setup,
        sheet_view,
    ):
        mock_ecr.return_value = (3, sheet_view.all_values)

        expense_pk = setup
        with patch(
            "dinary.background.sheet_logging.sheet_logging.clear_logging_job", return_value=False
        ):
            result = sheet_logging._drain_one_job(expense_pk, view=sheet_view)

        assert result is sheet_logging.DrainResult.RECOVERED_WITH_DUPLICATE
        con = storage.get_connection()
        try:
            assert list_logging_jobs(con) == []
        finally:
            con.close()

    @patch("dinary.background.sheet_logging.sheet_logging.get_rate", return_value="117.0")
    @patch("dinary.background.sheet_logging.sheet_logging.ensure_category_row")
    @patch("dinary.background.sheet_logging.sheet_logging.append_expense_atomic", return_value=True)
    def test_recovered_when_row_already_gone(
        self,
        _aea,
        mock_ecr,
        _gr,
        setup,
        sheet_view,
    ):
        """Even if the row was operator-wiped (not stolen), still surfaces
        ``RECOVERED_WITH_DUPLICATE`` — over-warning is safer than silently
        leaking a duplicate."""
        mock_ecr.return_value = (3, sheet_view.all_values)

        expense_pk = setup
        with (
            patch(
                "dinary.background.sheet_logging.sheet_logging.clear_logging_job",
                return_value=False,
            ),
            patch(
                "dinary.background.sheet_logging.sheet_logging.force_clear_logging_job",
                return_value=False,
            ),
        ):
            result = sheet_logging._drain_one_job(expense_pk, view=sheet_view)

        assert result is sheet_logging.DrainResult.RECOVERED_WITH_DUPLICATE
