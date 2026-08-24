"""Custom button labels, keyed by their built-in default text."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PanelButton


async def get_map(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(select(PanelButton))
    return {row.default_text: row.custom_text for row in result.scalars().all()}


async def get_icons(session: AsyncSession) -> dict[str, str]:
    """Custom-emoji ids an operator set explicitly, keyed by default text.

    Separate from the automatic icon map: this one is a deliberate choice for
    one button and must win over whatever the emoji-to-icon pass would infer.
    """
    result = await session.execute(select(PanelButton))
    return {
        row.default_text: row.icon_custom_emoji_id
        for row in result.scalars().all()
        if row.icon_custom_emoji_id
    }


async def set_label(
    session: AsyncSession,
    *,
    default_text: str,
    custom_text: str,
    icon_id: str | None = None,
) -> None:
    row = await session.get(PanelButton, default_text)
    if row is None:
        session.add(
            PanelButton(
                default_text=default_text,
                custom_text=custom_text,
                icon_custom_emoji_id=icon_id,
            )
        )
        await session.flush()
        return
    row.custom_text = custom_text
    # Only overwritten when one was actually sent, so renaming a button does
    # not silently drop the icon it already had.
    if icon_id is not None:
        row.icon_custom_emoji_id = icon_id
    await session.flush()


async def reset_label(session: AsyncSession, *, default_text: str) -> bool:
    result = await session.execute(
        delete(PanelButton)
        .where(PanelButton.default_text == default_text)
        .returning(PanelButton.default_text)
    )
    return bool(result.all())
