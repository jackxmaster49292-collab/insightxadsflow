from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    ForwardingRule,
    ForwardingRuleDestination,
    ForwardingRuleSource,
    RuleStatus,
    TelegramChat,
)


async def list_for_user(session: AsyncSession, *, user_id: uuid.UUID) -> Sequence[ForwardingRule]:
    result = await session.execute(
        select(ForwardingRule)
        .where(ForwardingRule.user_id == user_id)
        .options(selectinload(ForwardingRule.sources), selectinload(ForwardingRule.destinations))
        .order_by(ForwardingRule.created_at.desc())
    )
    return result.scalars().all()


async def get(
    session: AsyncSession, *, user_id: uuid.UUID, rule_id: uuid.UUID
) -> ForwardingRule | None:
    result = await session.execute(
        select(ForwardingRule)
        .where(ForwardingRule.id == rule_id, ForwardingRule.user_id == user_id)
        .options(selectinload(ForwardingRule.sources), selectinload(ForwardingRule.destinations))
    )
    return result.scalar_one_or_none()


async def get_unscoped_for_worker(
    session: AsyncSession, *, rule_id: uuid.UUID
) -> ForwardingRule | None:
    """System-scoped. Never reachable from an HTTP handler."""
    result = await session.execute(
        select(ForwardingRule)
        .where(ForwardingRule.id == rule_id)
        .options(selectinload(ForwardingRule.sources), selectinload(ForwardingRule.destinations))
    )
    return result.scalar_one_or_none()


async def list_active_with_source(
    session: AsyncSession, *, connection_id: uuid.UUID, chat_id: uuid.UUID
) -> Sequence[ForwardingRule]:
    """Active rules that read from a given chat. Used by the dispatcher."""
    result = await session.execute(
        select(ForwardingRule)
        .join(ForwardingRuleSource, ForwardingRuleSource.rule_id == ForwardingRule.id)
        .where(
            ForwardingRule.connection_id == connection_id,
            ForwardingRuleSource.chat_id == chat_id,
            ForwardingRule.status == RuleStatus.active,
        )
        .options(selectinload(ForwardingRule.sources), selectinload(ForwardingRule.destinations))
    )
    return result.scalars().unique().all()


async def source_chats(session: AsyncSession, *, rule: ForwardingRule) -> list[TelegramChat]:
    ids = [s.chat_id for s in rule.sources]
    if not ids:
        return []
    result = await session.execute(
        select(TelegramChat)
        .where(TelegramChat.id.in_(ids))
        .options(selectinload(TelegramChat.access))
    )
    return list(result.scalars().all())


async def destination_chats(session: AsyncSession, *, rule: ForwardingRule) -> list[TelegramChat]:
    ordered = sorted(rule.destinations, key=lambda d: d.position)
    ids = [d.chat_id for d in ordered]
    if not ids:
        return []
    result = await session.execute(
        select(TelegramChat)
        .where(TelegramChat.id.in_(ids))
        .options(selectinload(TelegramChat.access))
    )
    by_id = {c.id: c for c in result.scalars().all()}
    return [by_id[i] for i in ids if i in by_id]


async def replace_sources(
    session: AsyncSession, *, rule: ForwardingRule, chat_ids: Sequence[uuid.UUID]
) -> None:
    """Delete-then-insert via SQL rather than mutating the ORM collection.

    Touching ``rule.sources`` on a freshly flushed instance triggers a lazy load,
    which raises ``MissingGreenlet`` under asyncio.
    """
    await session.execute(
        delete(ForwardingRuleSource).where(ForwardingRuleSource.rule_id == rule.id)
    )
    for chat_id in dict.fromkeys(chat_ids):
        session.add(ForwardingRuleSource(rule_id=rule.id, chat_id=chat_id))
    await session.flush()


async def replace_destinations(
    session: AsyncSession, *, rule: ForwardingRule, chat_ids: Sequence[uuid.UUID]
) -> None:
    await session.execute(
        delete(ForwardingRuleDestination).where(ForwardingRuleDestination.rule_id == rule.id)
    )
    for position, chat_id in enumerate(dict.fromkeys(chat_ids)):
        session.add(ForwardingRuleDestination(rule_id=rule.id, chat_id=chat_id, position=position))
    await session.flush()


async def reload(session: AsyncSession, rule: ForwardingRule) -> ForwardingRule:
    """Re-read a rule with its collections eagerly loaded after replacement."""
    await session.refresh(rule)
    result = await session.execute(
        select(ForwardingRule)
        .where(ForwardingRule.id == rule.id)
        .options(selectinload(ForwardingRule.sources), selectinload(ForwardingRule.destinations))
    )
    return result.scalar_one()


async def disable_for_connection(
    session: AsyncSession, *, connection_id: uuid.UUID
) -> Sequence[ForwardingRule]:
    """Deleting or disconnecting a connection must disable its dependent rules."""
    result = await session.execute(
        select(ForwardingRule).where(ForwardingRule.connection_id == connection_id)
    )
    rules = result.scalars().all()
    for rule in rules:
        rule.status = RuleStatus.disconnected
    return rules
