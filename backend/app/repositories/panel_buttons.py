"""Custom button labels, keyed by their built-in default text."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PanelButton


async def get_map(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(select(PanelButton))
    return {row.default_text: row.custom_text for row in result.scalars().all()}


async def set_label(session: AsyncSession, *, default_text: str, custom_text: str) -> None:
    row = await session.get(PanelButton, default_text)
    if row is None:
        session.add(PanelButton(default_text=default_text, custom_text=custom_text))
    else:
        row.custom_text = custom_text
    await session.flush()


async def reset_label(session: AsyncSession, *, default_text: str) -> bool:
    result = await session.execute(
        delete(PanelButton)
        .where(PanelButton.default_text == default_text)
        .returning(PanelButton.default_text)
    )
    return bool(result.all())
