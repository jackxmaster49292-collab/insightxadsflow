"""Every emoji this panel actually draws, found by reading its own source.

A hand-written list was the obvious way and the wrong one: it drifts the moment
someone adds a screen, and the drift is invisible — the new button simply never
gets a premium icon and nobody notices for weeks. Reading the source cannot
drift, because the source is the thing being described.

Matching is deliberately generous. A few extra lookups for an emoji that only
appears in a comment costs half a second once; a missed one costs a plain icon
sitting among premium ones forever.
"""

from __future__ import annotations

import re

#: One emoji, including the forms that are several codepoints:
#:
#: * a base character from the pictographic blocks, or one of the older symbol
#:   blocks that Telegram treats as emoji (⏸ ⚡ ℹ ➡ ⬅ ✅ ❌ ⚠);
#: * an optional variation selector — ``⚠`` and ``⚠️`` are different strings
#:   and Telegram indexes them differently, so the selector is kept;
#: * optional ZWJ continuations, which is how ``👨‍👩‍👧`` is one emoji.
_BASE = (
    r"[\U0001F000-\U0001FAFF"
    r"\U00002600-\U000027BF"
    r"\U00002190-\U000021FF"
    r"\U00002B00-\U00002BFF"
    r"\U00002300-\U000023FF"
    # Geometric shapes — ▶ ◀ ⏹ live here, and missing the block cost the
    # Resume button its icon while everything around it had one.
    r"\U000025A0-\U000025FF"
    r"\U00002100-\U0000214F"
    r"\U0001F1E6-\U0001F1FF]"
)
_EMOJI = re.compile(rf"{_BASE}️?(?:‍{_BASE}️?)*")

#: Characters the ranges above sweep up that are not emoji in any useful sense.
#: ``™`` and ``№`` live in the letterlike block; the arrows block holds several
#: mathematical symbols. Searching Telegram for a premium ``™`` is a wasted
#: lookup and a confusing entry in the "not found" list.
_NOT_EMOJI = frozenset("™№℞℡⅍←↑→↓↔↕↖↗↘↙∀∃")


def leading_emoji(text: str) -> str | None:
    """The emoji ``text`` starts with, if it starts with one.

    Used where a button is about to carry a premium icon: the icon is drawn
    *before* the label, so the plain emoji still sitting at the front of that
    label would be the same picture twice.
    """
    match = _EMOJI.match(text)
    if match is None:
        return None
    emoji = match.group(0)
    if emoji in _NOT_EMOJI or emoji.rstrip("️") in _NOT_EMOJI:
        return None
    return emoji


def emoji_in(text: str) -> list[str]:
    """Every distinct emoji in ``text``, in the order it first appears."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _EMOJI.finditer(text):
        emoji = match.group(0)
        if emoji in _NOT_EMOJI or emoji.rstrip("️") in _NOT_EMOJI:
            continue
        if emoji not in seen:
            seen.add(emoji)
            found.append(emoji)
    return found


def panel_emoji() -> tuple[str, ...]:
    """Every emoji the panel's screens and prompts contain.

    Read from the two modules that hold all of them. Reading the source rather
    than rendering every screen is the cheaper half of the same idea, and it
    also catches the ones inside branches a test would have to contrive to
    reach.
    """
    import inspect

    from app.adminbot import handlers, views

    text = "".join(inspect.getsource(module) for module in (views, handlers))
    return tuple(emoji_in(text))
