"""Rules API: /api/rules/*"""

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from dinary.api.controllers.correction_ratings import record_correction_ratings
from dinary.api.controllers.rules import (
    approve_rule_category,
    build_rules_feed,
    confirm_rules_bulk,
)
from dinary.db.storage import get_db

router = APIRouter()


@router.get("/api/rules/feed")
def rules_feed(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    doubtful_only: bool = Query(default=True),
    con: sqlite3.Connection = Depends(get_db),  # noqa: B008
) -> dict:
    return build_rules_feed(con, page, page_size, doubtful_only=doubtful_only)


class ConfirmAllRequest(BaseModel):
    rule_ids: list[int]


@router.post("/api/rules/confirm-all")
def rules_confirm_all(
    body: ConfirmAllRequest,
    con: sqlite3.Connection = Depends(get_db),  # noqa: B008
) -> dict:
    confirmed = confirm_rules_bulk(con, body.rule_ids)
    return {"confirmed": confirmed}


class ApproveCategoryRequest(BaseModel):
    category_id: int


@router.patch("/api/rules/{rule_id}/category")
async def approve_rule_category_route(
    rule_id: int,
    body: ApproveCategoryRequest,
    request: Request,
    con: sqlite3.Connection = Depends(get_db),  # noqa: B008
) -> dict:
    pending_ratings: list[tuple[str, float]] = []
    # Offload the blocking sqlite work to a thread so this async handler does not
    # run it on the event loop; every other DB endpoint is sync `def` and gets
    # the same threadpool offload from Starlette automatically.
    result = await asyncio.to_thread(
        approve_rule_category,
        rule_id,
        body.category_id,
        con,
        pending_ratings,
    )
    await record_correction_ratings(request.app.state.llms, pending_ratings)
    return result
