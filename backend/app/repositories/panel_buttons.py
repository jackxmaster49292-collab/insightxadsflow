"""Custom button labels, keyed by their built-in default text."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PanelButton


async def get_map(session: AsyncSession) -> dict[str, str]:
    """Renames only.

    A row can exist for an icon or a colour alone, and those carry the built-in
    label. Reporting them as renames put ``📣 Ads → 📣 Ads`` on the buttons
    screen and counted it among the renamed — a rename to the identical words.
    """
    result = await session.execute(select(PanelButton))
    return {
        row.default_text: row.custom_text
        for row in result.scalars().all()
        if row.custom_text != row.default_text
    }


async def get_styles(session: AsyncSession) -> dict[str, str]:
    """Button colours an operator chose, keyed by default text."""
    result = await session.execute(select(PanelButton))
    return {row.default_text: row.style for row in result.scalars().all() if row.style}


async def set_style(session: AsyncSession, *, default_text: str, style: str | None) -> None:
    row = await session.get(PanelButton, default_text)
    if row is None:
        # A colour with no rename still needs a row, and the label it carries
        # is the built-in one so nothing appears to change.
        row = PanelButton(default_text=default_text, custom_text=default_text)
        session.add(row)
    row.style = style
    await session.flush()


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


async def set_icon(session: AsyncSession, *, default_text: str, icon_id: str | None) -> None:
    """Pin one button's icon, or ``None`` to go back to the automatic one.

    Same shape as :func:`set_style`: a row created for an icon alone carries
    the built-in label, so pinning an icon never looks like a rename.
    """
    row = await session.get(PanelButton, default_text)
    if row is None:
        if icon_id is None:
            return
        row = PanelButton(default_text=default_text, custom_text=default_text)
        session.add(row)
    row.icon_custom_emoji_id = icon_id
    await session.flush()


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
    """Back to the built-in words, keeping any icon or colour chosen for it.

    Deleting the row was the obvious way and it quietly threw away two other
    settings: an operator who had picked a red button and then reset its label
    lost the red as well, with nothing on screen to say so.
    """
    row = await session.get(PanelButton, default_text)
    if row is None:
        return False
    if row.icon_custom_emoji_id is None and row.style is None:
        await session.execute(delete(PanelButton).where(PanelButton.default_text == default_text))
        await session.flush()
        return True
    row.custom_text = default_text
    await session.flush()
    return True
