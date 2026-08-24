"""Answer people who message the connected account first.

This is the other half of a broadcast: someone reads an ad in a group, opens a
private chat with the account, and gets a reply without waiting for a human.

The constraint that shapes the whole module is that it must never be able to
send an unsolicited message. Three things enforce it, and each one alone would
be enough:

* the only entry point is :func:`handle_incoming`, which is called with a
  message that already arrived — there is no recipient list anywhere;
* a non-private chat is refused, so this cannot answer into a group;
* a per-person cooldown, held in the database, means one answer per person per
  window even across restarts.

There is deliberately no way to trigger a reply from the panel, no import, and
no "reply to everyone who ever wrote" action.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ChatRef, PeerKind, TelegramAdapter
from app.adapters.errors import classify_error
from app.db.models import AutoReply, ConnectionStatus, PeerType, TelegramConnection
from app.domain import reasons
from app.repositories import autoreply as autoreply_repo

log = structlog.get_logger(__name__)


class ReplyDecision:
    """Why a reply was or was not sent. Returned rather than raised: not
    replying is a normal outcome, not an error."""

    __slots__ = ("sent", "reason_code")

    def __init__(self, sent: bool, reason_code: str) -> None:
        self.sent = sent
        self.reason_code = reason_code

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ReplyDecision(sent={self.sent}, reason_code={self.reason_code!r})"


async def _is_advertising(session: AsyncSession, *, user_id: uuid.UUID) -> bool:
    """Is this account advertising — now, or recently enough to be answering?

    A repeating ad stays ``sending`` between rounds, so it counts throughout.
    A one-shot ad is over in a minute, which is why the window after it matters
    more than the round itself.
    """
    from datetime import UTC, datetime, timedelta

    from app.config import get_settings
    from app.db.models import Broadcast, BroadcastStatus

    since = datetime.now(UTC) - timedelta(hours=get_settings().auto_reply_after_ad_hours)
    result = await session.execute(
        select(func.count())
        .select_from(Broadcast)
        .where(
            Broadcast.user_id == user_id,
            (Broadcast.status == BroadcastStatus.sending)
            | (Broadcast.completed_at.isnot(None) & (Broadcast.completed_at >= since)),
        )
    )
    return bool(result.scalar_one())


async def handle_incoming(
    session: AsyncSession,
    *,
    connection: TelegramConnection,
    adapter: TelegramAdapter,
    sender: ChatRef,
    settings_cooldown_s: int | None = None,
) -> ReplyDecision:
    """Decide and, if appropriate, send one automatic reply.

    ``sender`` is the peer the incoming message came from. It is the only
    address this function can ever send to.
    """
    if connection.status is not ConnectionStatus.active:
        return ReplyDecision(False, reasons.CONNECTION_DISCONNECTED)

    # A group message must never produce a reply. Auto-reply exists for private
    # conversations someone else started; answering in a group would be posting
    # somewhere nobody asked us to.
    if sender.peer_type is not PeerKind.user:
        return ReplyDecision(False, reasons.AUTO_REPLY_NOT_PRIVATE)

    reply: AutoReply | None = await autoreply_repo.get_for_connection(
        session, connection_id=connection.id
    )
    if reply is None or not reply.enabled:
        return ReplyDecision(False, reasons.AUTO_REPLY_DISABLED)

    body = reply.body_text.strip()
    if not body:
        return ReplyDecision(False, reasons.AUTO_REPLY_DISABLED)

    # Tied to advertising, deliberately. An account that answers strangers
    # every hour of every day is behaving like a bot; one that answers while it
    # is advertising is answering the people who saw the ad. It also narrows
    # the blast radius of a mistake — a wrong reply text can only reach people
    # who wrote while an ad was running.
    if not await _is_advertising(session, user_id=connection.user_id):
        return ReplyDecision(False, reasons.AUTO_REPLY_NOT_ADVERTISING)

    cooldown = settings_cooldown_s if settings_cooldown_s is not None else reply.cooldown_s

    # Claim before sending. Two listeners can see the same incoming message; the
    # claim is a single statement so exactly one of them wins and the other
    # backs off, instead of both reading "no recent reply" and both answering.
    claimed = await autoreply_repo.claim_reply_slot(
        session,
        connection_id=connection.id,
        peer_id=sender.peer_id,
        peer_type=PeerType.user,
        cooldown_s=cooldown,
    )
    if not claimed:
        return ReplyDecision(False, reasons.AUTO_REPLY_COOLDOWN)

    try:
        await adapter.send_text(sender, body)
    except Exception as exc:
        # Give the slot back: a failed send must not cost this person their
        # whole cooldown for an answer that never arrived.
        await autoreply_repo.release_reply_slot(
            session, connection_id=connection.id, peer_id=sender.peer_id, peer_type=PeerType.user
        )
        classified = classify_error(exc)
        log.warning(
            "auto_reply_failed",
            connection_id=str(connection.id),
            code=classified.code,
        )
        return ReplyDecision(False, classified.code)

    reply.sent_count += 1
    log.info("auto_reply_sent", connection_id=str(connection.id))
    return ReplyDecision(True, reasons.AUTO_REPLY_SENT)
