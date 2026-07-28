"""Expense corrections API: PATCH /api/expenses/{id}/category"""

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, Request

from dinary.api.controllers.correction_ratings import record_correction_ratings
from dinary.api.controllers.expense_corrections import (
    CategoryCorrectionRequest,
    CategoryCorrectionResponse,
    correct_category_sync,
)
from dinary.db.storage import get_db

router = APIRouter()


@router.patch("/api/expenses/{expense_id}/category", response_model=CategoryCorrectionResponse)
async def correct_expense_category(
    expense_id: int,
    req: CategoryCorrectionRequest,
    request: Request,
    con: sqlite3.Connection = Depends(get_db),  # noqa: B008
) -> CategoryCorrectionResponse:
    pending_ratings: list[tuple[str, float]] = []
    # Offload the blocking sqlite work to a thread so this async handler does not
    # run it on the event loop; every other DB endpoint is sync `def` and gets
    # the same threadpool offload from Starlette automatically.
    resp = await asyncio.to_thread(
        correct_category_sync,
        expense_id,
        req,
        con,
        pending_ratings=pending_ratings,
    )
    await record_correction_ratings(request.app.state.llms, pending_ratings)
    return resp
