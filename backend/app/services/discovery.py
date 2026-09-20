"""Noticing which chats keep being mentioned in your groups.

Two halves, kept apart because they cost different things.

**Counting** happens on every message the listener already receives. It is pure
string work on text that is already in memory — no network call, no extra
query beyond the upsert — so it can afford to run on everything.

**Resolving** — turning ``t.me/somegroup`` into a title and a member count — is
a network call each, so it runs only for the links about to appear on a screen.
That is the same rule the chat-details screen follows: learn about what is
being looked at, and nothing else.

Nothing here joins anything, and nothing reads a member list. A link is a thing
to look at and decide about; the deciding stays with the operator.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import InboundMessage, TelegramAdapter
from app.db.models import DiscoveredLink
from app.domain.links import links_in_message
from app.repositories import chats as chat_repo
from app.repositories import discovered_links as link_repo

log = structlog.get_logger(__name__)

#: How many links one message may contribute. A promotional post lists a
#: handful; a hundred is a link-farm dump, and counting all of it would let one
#: message decide the whole ranking.
MAX_LINKS_PER_MESSAGE = 12


async def note_links(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    message: InboundMessage,
) -> int:
    """Count the chat links in one incoming message. Returns how many.

    Silent about everything else in the message. The text is read, the links
    are taken, and neither the text nor the sender is stored — the question
    being answered is "which chats keep coming up", and neither is needed for
    it.
    """
    links = links_in_message(message.text, message.entity_urls)
    if not links:
        return 0

    source_chat = await chat_repo.find_by_peer(
        session, connection_id=connection_id, ref=message.source
    )
    if source_chat is None:
        # A chat that has never been synchronized is one we cannot name on the
        # screen, and "seen in some group" is not worth a row.
        return 0

    return await link_repo.record(
        session,
        connection_id=connection_id,
        chat_id=source_chat.id,
        links=links[:MAX_LINKS_PER_MESSAGE],
    )


async def resolve(
    session: AsyncSession,
    *,
    links: list[DiscoveredLink],
    adapter: TelegramAdapter,
) -> int:
    """Look up what each link points at. One network call each, so keep it short.

    A failure is stored as a failure rather than retried on every open: a link
    to a chat that was deleted would otherwise cost a call every time the
    screen is drawn, for ever.
    """
    resolved = 0
    for link in links:
        try:
            preview = await adapter.preview_link(link.kind, link.link_key)
        except Exception as exc:
            log.warning("link_preview_failed", link=link.link_key, error=str(exc))
            continue

        await link_repo.store_resolution(
            session,
            link=link,
            title=preview.title,
            member_count=preview.member_count,
            chat_kind=preview.chat_kind,
            error_code=preview.reason_code,
        )
        resolved += 1
    return resolved
