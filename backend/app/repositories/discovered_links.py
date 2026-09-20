"""Chat links noticed in incoming messages.

Counting, and nothing else. What a link *is* — its title, its size — is looked
up separately and only for the links about to be shown, because resolving is a
network call and there is no point spending one on the four hundredth entry
nobody will scroll to.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DiscoveredLink,
    DiscoveredLinkSource,
    TelegramChat,
    TelegramConnection,
)
from app.domain.links import ChatLink


@dataclass(slots=True)
class LinkRow:
    """One link, ready to render: the row plus the two numbers it is ranked by."""

    link: DiscoveredLink
    group_count: int
    titles: list[str]


async def record(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    chat_id: uuid.UUID,
    links: Sequence[ChatLink],
) -> int:
    """Count these links as seen once each, in this chat.

    Upserted rather than read-then-written: a listener processes messages from
    many chats concurrently, and two arriving with the same link would
    otherwise both read zero and both write one.
    """
    if not links:
        return 0

    now = datetime.now(UTC)
    for link in links:
        result = await session.execute(
            pg_insert(DiscoveredLink)
            .values(
                id=uuid.uuid4(),
                connection_id=connection_id,
                kind=link.kind.value,
                link_key=link.key,
                times_seen=1,
                first_seen_at=now,
                last_seen_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_discovered_links_connection_key",
                set_={
                    "times_seen": DiscoveredLink.times_seen + 1,
                    "last_seen_at": now,
                },
            )
            .returning(DiscoveredLink.id)
        )
        link_id = result.scalar_one()

        await session.execute(
            pg_insert(DiscoveredLinkSource)
            .values(
                id=uuid.uuid4(),
                link_id=link_id,
                chat_id=chat_id,
                times_seen=1,
                last_seen_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_discovered_link_sources_link_chat",
                set_={
                    "times_seen": DiscoveredLinkSource.times_seen + 1,
                    "last_seen_at": now,
                },
            )
        )
    return len(links)


async def known_usernames(session: AsyncSession, *, user_id: uuid.UUID) -> set[str]:
    """Usernames of chats this user's connections are already in.

    A link to a group you are already in is not a discovery, and with 735 of
    them most of the list would be that. Lowercased, because a username is
    case-insensitive to Telegram and this is compared against a lowercased key.
    """
    result = await session.execute(
        select(TelegramChat.username)
        .join(TelegramConnection, TelegramConnection.id == TelegramChat.connection_id)
        .where(
            TelegramConnection.user_id == user_id,
            TelegramChat.username.is_not(None),
            TelegramChat.is_active.is_(True),
        )
    )
    return {str(name).lower() for name in result.scalars().all() if name}


async def listing(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int = 200,
) -> list[LinkRow]:
    """Links worth looking at, best first.

    Ranked by how many *different* groups carried it, then by the raw count.
    One person posting a link fifty times in one chat is one person; six groups
    carrying it is several communities overlapping with that chat, which is the
    thing worth acting on.

    Hidden links, links to chats already joined, and anything that resolved to
    a person are all left out — each of them is an entry that could only ever
    be scrolled past.
    """
    groups = (
        select(
            DiscoveredLinkSource.link_id.label("link_id"),
            func.count().label("group_count"),
        )
        .group_by(DiscoveredLinkSource.link_id)
        .subquery()
    )

    result = await session.execute(
        select(DiscoveredLink, groups.c.group_count)
        .join(TelegramConnection, TelegramConnection.id == DiscoveredLink.connection_id)
        .join(groups, groups.c.link_id == DiscoveredLink.id)
        .where(
            TelegramConnection.user_id == user_id,
            DiscoveredLink.hidden_at.is_(None),
            DiscoveredLink.resolved_kind.is_distinct_from("user"),
        )
        .order_by(groups.c.group_count.desc(), DiscoveredLink.times_seen.desc())
        .limit(limit)
    )
    rows = [LinkRow(link=link, group_count=int(count), titles=[]) for link, count in result.all()]

    known = await known_usernames(session, user_id=user_id)
    rows = [r for r in rows if not (r.link.kind == "public" and r.link.link_key in known)]

    if rows:
        titles = await _titles_for(session, link_ids=[r.link.id for r in rows])
        for row in rows:
            row.titles = titles.get(row.link.id, [])
    return rows


async def _titles_for(
    session: AsyncSession, *, link_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Which of your groups each link appeared in, by title.

    One query for the whole page: naming the groups is most of why the screen
    is useful, and a query per row is how a list of forty stops opening.
    """
    result = await session.execute(
        select(DiscoveredLinkSource.link_id, TelegramChat.title, DiscoveredLinkSource.times_seen)
        .join(TelegramChat, TelegramChat.id == DiscoveredLinkSource.chat_id)
        .where(DiscoveredLinkSource.link_id.in_(link_ids))
        .order_by(DiscoveredLinkSource.times_seen.desc())
    )
    titles: dict[uuid.UUID, list[str]] = {}
    for link_id, title, _count in result.all():
        titles.setdefault(link_id, []).append(title)
    return titles


async def get(
    session: AsyncSession, *, user_id: uuid.UUID, link_id: uuid.UUID
) -> DiscoveredLink | None:
    result = await session.execute(
        select(DiscoveredLink)
        .join(TelegramConnection, TelegramConnection.id == DiscoveredLink.connection_id)
        .where(DiscoveredLink.id == link_id, TelegramConnection.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def hide(session: AsyncSession, *, user_id: uuid.UUID, link_id: uuid.UUID) -> bool:
    link = await get(session, user_id=user_id, link_id=link_id)
    if link is None:
        return False
    link.hidden_at = datetime.now(UTC)
    await session.flush()
    return True


async def hide_seen_once(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Everything that turned up in exactly one group, one time.

    That is what referral spam and a stray forward look like, and it is most of
    the noise. Anything posted twice, or in two groups, is left alone.
    """
    rows = await listing(session, user_id=user_id, limit=1000)
    hidden = 0
    for row in rows:
        if row.group_count == 1 and row.link.times_seen == 1:
            row.link.hidden_at = datetime.now(UTC)
            hidden += 1
    await session.flush()
    return hidden


async def unresolved(
    session: AsyncSession, *, link_ids: Sequence[uuid.UUID]
) -> list[DiscoveredLink]:
    if not link_ids:
        return []
    result = await session.execute(
        select(DiscoveredLink).where(
            DiscoveredLink.id.in_(link_ids), DiscoveredLink.resolved_at.is_(None)
        )
    )
    return list(result.scalars().all())


async def store_resolution(
    session: AsyncSession,
    *,
    link: DiscoveredLink,
    title: str | None,
    member_count: int | None,
    chat_kind: str | None,
    error_code: str | None = None,
) -> None:
    link.resolved_at = datetime.now(UTC)
    link.resolved_title = (title or None) and title[:255]
    link.resolved_member_count = member_count
    link.resolved_kind = chat_kind
    link.resolve_error_code = error_code
    await session.flush()
