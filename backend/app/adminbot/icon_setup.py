"""Fetching the panel's premium icons from Telegram.

Custom emoji ids are Telegram *documents* — there is no table to ship and
nothing to hardcode, so they have to be asked for. This module is the asking,
shared by the startup task and the operator's button so the two cannot drift.

Whose account does the asking matters. Only an **operator's** connection is
used: the panel's own decoration is the deployment's business, and reaching for
some customer's account to fetch it would be using their Telegram session for
something they never asked for.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import TelegramAdapter
from app.adminbot import premium_icons, views
from app.config import get_settings
from app.db.models import ConnectionKind, ConnectionStatus, TelegramConnection, User

log = structlog.get_logger(__name__)

#: Pause between searches. ~40 lookups in a tight burst is the kind of thing a
#: rate limiter notices, and this costs four seconds once.
_SEARCH_GAP_S = 0.1


async def fetch_icons(adapter: TelegramAdapter) -> dict[str, str]:
    """Find a custom emoji for each icon the panel draws.

    Two sources, cheapest and richest first. The account's **own packs** are
    read in one pass and cover most of a panel, because every document in a
    pack carries the plain emoji it stands in for. Only what that misses is
    then *searched* one emoticon at a time — which is what ran before, and on
    a real account returned 1 icon out of 43: searching surfaces what Telegram
    suggests, while the packs are what the account actually has.

    An emoji with no premium version anywhere is left out and stays plain. A
    partial map is the normal outcome, not a failure.
    """
    found: dict[str, str] = {}

    owned = await adapter.installed_custom_emoji()
    for emoticon in views.PANEL_EMOJI:
        for candidate in (emoticon, emoticon.rstrip("️")):
            if candidate in owned:
                found[emoticon] = owned[candidate]
                break

    for emoticon in views.PANEL_EMOJI:
        if emoticon in found:
            continue
        ids = await adapter.custom_emoji_ids(emoticon)
        if not ids and emoticon.endswith("️"):
            # Some emoji are indexed without their variation selector.
            ids = await adapter.custom_emoji_ids(emoticon.rstrip("️"))
        if ids:
            found[emoticon] = ids[0]
        await asyncio.sleep(_SEARCH_GAP_S)
    return found


async def operator_connection(session: AsyncSession) -> TelegramConnection | None:
    """An operator's own active Telegram account, or None."""
    settings = get_settings()
    if not settings.admin_ids:
        return None
    result = await session.execute(
        select(TelegramConnection)
        .join(User, User.id == TelegramConnection.user_id)
        .where(
            User.telegram_user_id.in_(settings.admin_ids),
            TelegramConnection.kind == ConnectionKind.user,
            TelegramConnection.status == ConnectionStatus.active,
        )
        .order_by(TelegramConnection.created_at)
    )
    return result.scalars().first()


async def ensure_icons() -> None:
    """Fetch and store the icons if they have never been fetched.

    Runs once, in the background, at panel startup. The operator asked not to
    have to do this by hand, and there is nothing here a human adds: the ids
    come from Telegram either way. It is skipped entirely when a map already
    exists, so a restart is not forty needless lookups.

    Best-effort throughout. Icons are decoration; a failure here must never be
    the reason the panel does not start.
    """
    from app.db.session import session_scope
    from app.repositories import panel_emoji as panel_emoji_repo
    from app.services import connections as connection_service

    try:
        async with session_scope() as session:
            if await panel_emoji_repo.get_map(session):
                return
            connection = await operator_connection(session)
            if connection is None:
                log.info(
                    "panel_icons_skipped",
                    reason="no active operator account to ask Telegram with",
                )
                return
            adapter = await connection_service.adapter_for(session, connection)

        try:
            found = await fetch_icons(adapter)
        finally:
            if get_settings().live_telegram:
                with contextlib.suppress(Exception):
                    await adapter.disconnect()

        if not found:
            log.warning("panel_icons_empty", searched=len(views.PANEL_EMOJI))
            return

        async with session_scope() as session:
            await panel_emoji_repo.replace(session, mapping=found)
        premium_icons.set_map(found)
        log.info("panel_icons_ready", matched=len(found), of=len(views.PANEL_EMOJI))
    except Exception as exc:  # pragma: no cover - network path
        log.warning("panel_icons_failed", error=str(exc))
