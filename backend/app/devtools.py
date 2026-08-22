"""Local development helpers for the mock Telegram provider.

The mock never receives real updates, so there is no way to exercise forwarding
end to end locally without a way to inject a message. This does that through the
*real* dispatch path — the same code the listener calls — so what you observe is
the genuine pipeline, not a simulation of it.

Refuses to run against a live provider.

    python -m app.devtools emit <connection_id> --text "Public launch is live"
    python -m app.devtools ad <connection_id> --text "Our October offer"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select

from app.adapters.base import ChatRef, InboundMessage, MediaType, PeerKind
from app.config import get_settings
from app.db.models import ConnectionChatAccess, TelegramChat, TelegramConnection
from app.db.session import dispose_engine, session_scope
from app.logging_setup import configure_logging
from app.services.dispatch import dispatch_inbound


async def emit(connection_id: uuid.UUID, *, text: str, message_id: int, source_title: str) -> int:
    async with session_scope() as session:
        connection = (
            await session.execute(
                select(TelegramConnection).where(TelegramConnection.id == connection_id)
            )
        ).scalar_one_or_none()
        if connection is None:
            print(f"No such connection: {connection_id}", file=sys.stderr)
            return 2

        chat = (
            await session.execute(
                select(TelegramChat).where(
                    TelegramChat.connection_id == connection_id,
                    TelegramChat.title == source_title,
                )
            )
        ).scalar_one_or_none()
        if chat is None:
            print(
                f"No chat titled {source_title!r} on this connection. "
                "Synchronize the connection first.",
                file=sys.stderr,
            )
            return 2

        message = InboundMessage(
            source=ChatRef(PeerKind(chat.peer_type.value), chat.peer_id),
            message_ids=[message_id],
            media_type=MediaType.text,
            text=text,
        )
        result = await dispatch_inbound(session, connection_id=connection_id, message=message)

    print(
        f"Dispatched message {message_id} from {source_title!r}: "
        f"{len(result.created_job_ids)} job(s) created, "
        f"{result.suppressed} duplicate(s) suppressed, "
        f"{result.matched_rules} active rule(s) matched"
        + (f", skipped: {result.skipped_reason}" if result.skipped_reason else "")
    )
    return 0


async def ad(connection_id: uuid.UUID, *, text: str, name: str, delay_ms: int) -> int:
    """Compose and queue a broadcast to every group the connection can post in.

    Goes through the real service, so the worker delivers it exactly as it would
    one composed in the bot — including pacing, the pre-send permission check and
    the per-group result rows.
    """
    from app.db.models import Broadcast
    from app.repositories import broadcasts as broadcast_repo
    from app.services import broadcast as broadcast_service

    async with session_scope() as session:
        connection = (
            await session.execute(
                select(TelegramConnection).where(TelegramConnection.id == connection_id)
            )
        ).scalar_one_or_none()
        if connection is None:
            print(f"No such connection: {connection_id}", file=sys.stderr)
            return 2

        chats = (
            (
                await session.execute(
                    select(TelegramChat)
                    .join(ConnectionChatAccess, ConnectionChatAccess.chat_id == TelegramChat.id)
                    .where(
                        TelegramChat.connection_id == connection_id,
                        TelegramChat.is_active.is_(True),
                        ConnectionChatAccess.can_post_destination.is_(True),
                    )
                    .order_by(TelegramChat.title)
                )
            )
            .scalars()
            .all()
        )
        if not chats:
            print(
                "No group on this connection accepts posts. Synchronize it first.",
                file=sys.stderr,
            )
            return 2

        broadcast: Broadcast = await broadcast_repo.create(
            session,
            user_id=connection.user_id,
            connection_id=connection_id,
            name=name,
            delay_ms=delay_ms,
        )
        broadcast.body_text = text
        await broadcast_repo.replace_targets(
            session, broadcast=broadcast, chat_ids=[c.id for c in chats]
        )
        try:
            queued = await broadcast_service.queue(session, broadcast=broadcast)
        except broadcast_service.BroadcastValidationError as exc:
            print(f"Refused: {exc.message}", file=sys.stderr)
            return 2
        broadcast_id = broadcast.id

    print(
        f"Queued broadcast {broadcast_id} to {queued} group(s): "
        + ", ".join(c.title for c in chats[:5])
        + (f" and {len(chats) - 5} more" if len(chats) > 5 else "")
    )
    print("Watch it deliver with:  docker compose logs -f worker")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.devtools", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    emit_parser = sub.add_parser("emit", help="Inject a mock source message")
    emit_parser.add_argument("connection_id")
    emit_parser.add_argument("--text", default="Public launch is live")
    emit_parser.add_argument("--message-id", type=int, default=1)
    emit_parser.add_argument("--source-title", default="Demo announcements")

    ad_parser = sub.add_parser("ad", help="Compose and queue a broadcast to every postable group")
    ad_parser.add_argument("connection_id")
    ad_parser.add_argument("--text", default="Our October offer is live")
    ad_parser.add_argument("--name", default="Devtools ad")
    ad_parser.add_argument("--delay-ms", type=int, default=0)

    args = parser.parse_args()
    configure_logging(json_output=False)

    if get_settings().live_telegram:
        raise SystemExit(
            "Refusing to run: TELEGRAM_PROVIDER=live. These tools only make "
            "sense against the mock provider."
        )

    async def run() -> int:
        # The engine is bound to the running loop, so it must be disposed inside
        # the same one — disposing from a second asyncio.run() closes it twice.
        try:
            if args.command == "ad":
                return await ad(
                    uuid.UUID(args.connection_id),
                    text=args.text,
                    name=args.name,
                    delay_ms=args.delay_ms,
                )
            return await emit(
                uuid.UUID(args.connection_id),
                text=args.text,
                message_id=args.message_id,
                source_title=args.source_title,
            )
        finally:
            await dispose_engine()

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
