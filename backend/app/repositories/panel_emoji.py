"""The panel's premium-icon map.

Tiny by design: one row per unicode emoji the panel draws, holding the custom
emoji document id an operator extracted for it. Presence of rows is the whole
on/off state — no flag to drift out of sync with the data it describes.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PanelEmoji


async def get_map(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(select(PanelEmoji))
    return {row.emoticon: row.custom_emoji_id for row in result.scalars().all()}


async def replace(session: AsyncSession, *, mapping: dict[str, str]) -> int:
    """Swap the whole map atomically. Extraction is all-or-nothing per run."""
    await session.execute(delete(PanelEmoji))
    for emoticon, custom_emoji_id in mapping.items():
        session.add(PanelEmoji(emoticon=emoticon, custom_emoji_id=custom_emoji_id))
    await session.flush()
    return len(mapping)


async def clear(session: AsyncSession) -> int:
    result = await session.execute(delete(PanelEmoji).returning(PanelEmoji.emoticon))
    return len(result.all())
