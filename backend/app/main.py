from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.v1 import auth, chats, connections, rules
from app.api.v1 import status as status_routes
from app.config import get_settings
from app.db.session import dispose_engine
from app.logging_setup import configure_logging, correlation_id
from app.security.ratelimit import close_redis

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(json_output=settings.environment != "local")
    log.info(
        "api_starting",
        environment=settings.environment,
        telegram_provider=settings.telegram_provider,
    )
    yield
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="InsightAdFlow — Telegram Forwarding Bot",
        version="0.1.0",
        description=(
            "Automatic Telegram forwarding between authorized chats. "
            "No quotas, no subscription limits — and no evasion of Telegram's own limits."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.panel_origin],  # never a wildcard with credentials
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
    )

    @app.middleware("http")
    async def correlation_and_headers(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        token = request.headers.get("X-Correlation-Id") or uuid.uuid4().hex
        reset = correlation_id.set(token)
        try:
            response = await call_next(request)
        finally:
            correlation_id.reset(reset)

        response.headers["X-Correlation-Id"] = token
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response

    register_exception_handlers(app)

    for router in (
        auth.router,
        connections.router,
        chats.router,
        rules.router,
        status_routes.router,
    ):
        app.include_router(router, prefix="/api/v1")

    return app


app = create_app()
