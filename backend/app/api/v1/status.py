from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.api.deps import CurrentUser, SessionDep
from app.config import get_settings
from app.domain import reasons
from app.repositories import events as event_repo
from app.schemas import (
    AuditEventResponse,
    EventResponse,
    HealthResponse,
    UsageSummaryResponse,
)
from app.security.ratelimit import get_redis

router = APIRouter(tags=["status"])

PERIOD_HOURS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}


@router.get("/health")
async def health(session: SessionDep) -> HealthResponse:
    database = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        database = False

    redis_ok = True
    try:
        await get_redis().ping()
    except Exception:
        redis_ok = False

    return HealthResponse(
        status="ok" if database and redis_ok else "degraded",
        database=database,
        redis=redis_ok,
        telegram_provider=get_settings().telegram_provider,
    )


@router.get("/activity")
async def activity(
    user: CurrentUser,
    session: SessionDep,
    outcome: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[EventResponse]:
    rows = await event_repo.list_recent_for_user(
        session, user_id=user.id, outcome=outcome, limit=limit, offset=offset
    )
    result = []
    for row in rows:
        payload = EventResponse.model_validate(row)
        payload.reason_text = reasons.describe(row.reason_code)
        result.append(payload)
    return result


@router.get("/usage/summary")
async def usage_summary(
    user: CurrentUser,
    session: SessionDep,
    period: str = Query(default="24h", pattern="^(24h|7d|30d)$"),
) -> UsageSummaryResponse:
    """Operational counters only. This product has no quotas to account for."""
    counts = await event_repo.summary(session, user_id=user.id, period_hours=PERIOD_HOURS[period])
    return UsageSummaryResponse(
        period=period,
        forwarded=counts.get("forwarded", 0),
        skipped=counts.get("skipped", 0),
        failed=counts.get("failed", 0),
        retry_scheduled=counts.get("retry_scheduled", 0),
        paused=counts.get("paused", 0),
    )


@router.get("/audit-events")
async def audit_events(
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[AuditEventResponse]:
    rows = await event_repo.list_audit(session, user_id=user.id, limit=limit, offset=offset)
    return [AuditEventResponse.model_validate(row) for row in rows]
