"""Demo data for local development.

Refuses to run against a live Telegram provider: the chats it creates are
fictional, and pointing real forwarding rules at them would be meaningless.
"""

from __future__ import annotations

import asyncio
import secrets

import structlog

from app.adapters.base import DiscoveredChat
from app.adapters.factory import mock_script_for
from app.config import get_settings
from app.db.models import ConnectionKind, ConnectionStatus, ControlTaskKind, RuleStatus
from app.db.session import dispose_engine, session_scope
from app.logging_setup import configure_logging
from app.repositories import connections as connection_repo
from app.repositories import jobs as job_repo
from app.repositories import users as user_repo
from app.services import rules as rule_service
from app.services import sync as sync_service
from app.services.connections import adapter_for

log = structlog.get_logger(__name__)

DEMO_EMAIL = "demo@insight.local"
SOURCE_PEER = -1001999000001
DESTINATION_PEERS = (-1001999000101, -1001999000102, -1001999000103)


def _chat(peer_id: int, title: str, kind: str) -> DiscoveredChat:
    from app.adapters.base import ChatRef, PeerKind

    return DiscoveredChat(
        ref=ChatRef(PeerKind.channel, peer_id),
        title=title,
        chat_kind=kind,
        username=None,
        is_public=False,
        has_protected_content=False,
    )


async def seed() -> None:
    settings = get_settings()
    configure_logging(json_output=False)

    if settings.live_telegram:
        raise SystemExit(
            "Refusing to seed with TELEGRAM_PROVIDER=live. The demo chats are "
            "fictional; set TELEGRAM_PROVIDER=mock to seed."
        )

    password = secrets.token_urlsafe(12)

    async with session_scope() as session:
        if await user_repo.get_by_email(session, DEMO_EMAIL) is not None:
            log.info("seed_skipped", reason="demo user already exists", email=DEMO_EMAIL)
            return

        user = await user_repo.create(session, email=DEMO_EMAIL, password=password)

        connection = await connection_repo.create(
            session,
            user_id=user.id,
            kind=ConnectionKind.bot,
            label="Demo bot (mock)",
            status=ConnectionStatus.active,
        )
        connection_repo.store_bot_token(connection, "000000000:DEMO-MOCK-TOKEN-NOT-REAL-000000000")
        connection.telegram_account_id = 999_000_001
        connection.telegram_username = "demo_mock_bot"
        await session.flush()

        script = mock_script_for(connection.id)
        script.chats = [
            _chat(SOURCE_PEER, "Demo announcements", "channel"),
            *[
                _chat(peer, f"Demo partner {i + 1}", "supergroup")
                for i, peer in enumerate(DESTINATION_PEERS)
            ],
        ]

        adapter = await adapter_for(session, connection)
        report = await sync_service.synchronize(session, connection=connection, adapter=adapter)

        from sqlalchemy import select

        from app.db.models import TelegramChat

        chats = (
            (
                await session.execute(
                    select(TelegramChat).where(TelegramChat.connection_id == connection.id)
                )
            )
            .scalars()
            .all()
        )
        source = next(c for c in chats if c.peer_id == SOURCE_PEER)
        destinations = [c for c in chats if c.peer_id in DESTINATION_PEERS]

        rule = await rule_service.create(
            session,
            user_id=user.id,
            connection=connection,
            payload=rule_service.RuleInput(
                name="Demo: announcements to partners",
                connection_id=connection.id,
                source_chat_ids=[source.id],
                destination_chat_ids=[d.id for d in destinations],
                keyword_exclude=["internal"],
                delay_ms=1000,
            ),
        )
        rule.status = RuleStatus.draft

        await job_repo.enqueue_control(
            session,
            user_id=user.id,
            kind=ControlTaskKind.health_check,
            connection_id=connection.id,
        )

    print("\n" + "=" * 68)
    print("  Demo data ready (mock Telegram provider — nothing real is contacted)")
    print("=" * 68)
    print("  Panel     http://localhost:8080")
    print(f"  Email     {DEMO_EMAIL}")
    print(f"  Password  {password}")
    print(
        f"\n  1 connection, {report.discovered} chats, 1 draft rule "
        f"({len(DESTINATION_PEERS)} destinations)."
    )
    print("  Activate the rule in the panel, then run `make worker` to process jobs.")
    print("=" * 68 + "\n")

    await dispose_engine()


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
