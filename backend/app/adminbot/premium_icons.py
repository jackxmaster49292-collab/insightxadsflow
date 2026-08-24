"""Premium icons for the panel's own screens.

A custom emoji in MarkdownV2 is ``![🔥](tg://emoji?id=<document_id>)`` — the
plain emoji stays embedded as the fallback, and Telegram swaps in the premium
drawing when it may. The ids are Telegram documents, so they are extracted from
a connected account's emoji search by an operator, never hardcoded.

Two Telegram restrictions shape everything here:

* **Button labels cannot carry entities at all.** The syntax would show as
  literal text, so buttons keep their plain unicode icons — there is no way
  around this, for any bot.
* **Only a bot with a Fragment username may send custom emoji.** For any other
  bot Telegram rejects the message outright. That is why the transform is
  applied at the last moment and stripped on rejection: the panel must degrade
  to plain icons, never to a blank screen.

Applied centrally in the send path rather than inside every view, so coverage
is every screen at once and the fallback is one place instead of forty.
"""

from __future__ import annotations

import re
import threading
from typing import Any

_lock = threading.Lock()
_map: dict[str, str] = {}
_suspended = False

#: Operator-customized button labels, keyed by the built-in default text.
_labels: dict[str, str] = {}
#: Icons chosen explicitly for one button, keyed the same way. A deliberate
#: choice, so it beats whatever the emoji-to-icon pass would infer.
_button_icons: dict[str, str] = {}
#: Button colours, same key again.
_button_styles: dict[str, str] = {}

#: One custom-emoji token, for stripping a rejected message back to plain.
_TOKEN = re.compile(r"!\[([^\]]+)\]\(tg://emoji\?id=\d+\)")


def set_map(mapping: dict[str, str]) -> None:
    global _suspended
    with _lock:
        _map.clear()
        _map.update(mapping)
        _suspended = False


def suspend() -> None:
    """Telegram rejected a premium message — stop trying for this process.

    The extracted map stays in the database untouched, so the operator screen
    can still say what was extracted and why it is not showing. A restart, or a
    fresh extraction, tries again — cheap, and correct the day the bot gains a
    Fragment username.
    """
    global _suspended
    with _lock:
        _suspended = True


def suspended() -> bool:
    return _suspended


def get_map() -> dict[str, str]:
    with _lock:
        return dict(_map)


def enabled() -> bool:
    return bool(_map) and not _suspended


def apply(text: str) -> str:
    """Rewrite every mapped emoji in ``text`` as a custom-emoji token.

    One pass with one alternation, longest emoticon first. One pass matters as
    much as the ordering: replacing key by key would find the shorter form of
    an emoji *inside* a token the longer form just produced — ``⚠`` inside
    ``![⚠️](…)`` — and wrap it twice. A single sweep never revisits its own
    output. Runs after escaping — emoji are never escapable characters, and the
    token itself must not be escaped.
    """
    if not _map or _suspended:
        return text
    with _lock:
        if not _map:
            return text
        mapping = dict(_map)
    pattern = re.compile("|".join(re.escape(e) for e in sorted(mapping, key=len, reverse=True)))
    return pattern.sub(lambda m: f"![{m.group(0)}](tg://emoji?id={mapping[m.group(0)]})", text)


def strip(text: str) -> str:
    """Back to plain emoji — the exact inverse of :func:`apply`."""
    return _TOKEN.sub(r"\1", text)


#: ``![🔥](tg://emoji?id=123)`` — Telegram's own MarkdownV2 spelling for an
#: inline custom emoji. Accepted as *input* wherever text is taken, so an id
#: from anywhere can be used without owning the pack.
CUSTOM_EMOJI_MARKUP = re.compile(r"!\[([^\]]+)\]\(tg://emoji\?id=(\d+)\)")

#: A bare custom-emoji document id. Telegram's are 18-19 digits; anything
#: shorter is far more likely to be a price, a count or a year, and reading
#: those as emoji ids would silently mangle ordinary text.
BARE_EMOJI_ID = re.compile(r"\b(\d{15,20})\b")


def parse_entities(text: str) -> tuple[str, list[dict[str, object]]]:
    """Turn ``![🔥](tg://emoji?id=N)`` markup into text plus real entities.

    Returns the text with each token replaced by its fallback emoji, and the
    entities describing where the custom emoji go. Offsets are counted in
    **UTF-16 code units**, which is Telegram's unit and not Python's: an emoji
    is a surrogate pair there, so counting characters would place every entity
    after the first one wrongly.
    """
    out: list[str] = []
    entities: list[dict[str, object]] = []
    units = 0  # UTF-16 code units written so far
    cursor = 0

    for match in CUSTOM_EMOJI_MARKUP.finditer(text):
        plain = text[cursor : match.start()]
        out.append(plain)
        units += len(plain.encode("utf-16-le")) // 2

        fallback = match.group(1)
        length = len(fallback.encode("utf-16-le")) // 2
        entities.append(
            {
                "type": "custom_emoji",
                "offset": units,
                "length": length,
                "custom_emoji_id": match.group(2),
            }
        )
        out.append(fallback)
        units += length
        cursor = match.end()

    out.append(text[cursor:])
    return "".join(out), entities


def set_labels(
    mapping: dict[str, str],
    icons: dict[str, str] | None = None,
    styles: dict[str, str] | None = None,
) -> None:
    with _lock:
        _labels.clear()
        _labels.update(mapping)
        _button_icons.clear()
        _button_icons.update(icons or {})
        _button_styles.clear()
        _button_styles.update(styles or {})


def get_styles() -> dict[str, str]:
    with _lock:
        return dict(_button_styles)


def get_button_icons() -> dict[str, str]:
    with _lock:
        return dict(_button_icons)


def get_labels() -> dict[str, str]:
    with _lock:
        return dict(_labels)


def apply_labels(markup: Any) -> Any:
    """A copy of ``markup`` with the operator's custom button labels.

    Exact match on the built-in default text — the one identity a button keeps
    across screens. Runs *before* the premium-icon pass, so a label key is
    always the plain default the operator saw when renaming, never a half-
    transformed one. Custom labels are plain text straight from the database;
    there is no failure mode to fall back from.
    """
    if markup is None or not (_labels or _button_icons or _button_styles):
        return markup
    from aiogram.types import InlineKeyboardMarkup

    with _lock:
        labels = dict(_labels)
        icons = dict(_button_icons)
        styles = dict(_button_styles)

    changed = False
    rows = []
    for row in markup.inline_keyboard:
        buttons = []
        for button in row:
            update: dict[str, str] = {}
            if button.text in labels:
                update["text"] = labels[button.text]
            if button.text in icons:
                update["icon_custom_emoji_id"] = icons[button.text]
            if button.text in styles:
                update["style"] = styles[button.text]
            if not update:
                buttons.append(button)
                continue
            changed = True
            buttons.append(button.model_copy(update=update))
        rows.append(buttons)
    if not changed:
        return markup
    return InlineKeyboardMarkup(inline_keyboard=rows)


def apply_keyboard(markup: Any) -> Any:
    """A copy of ``markup`` with each button's leading emoji as a premium icon.

    Bot API 10.2 gave buttons ``icon_custom_emoji_id`` — an icon drawn *before*
    the label. A button whose label starts with a mapped emoji gets the icon
    and loses the leading emoji from its text, so it is not drawn twice. Works
    per Telegram's rule when the bot owns a Fragment username **or** when the
    bot's owner has Telegram Premium and the message is sent directly by the
    bot — which is exactly what every panel screen is.

    Returns the original object untouched when there is nothing to do, so the
    send path can cheaply tell whether a fallback would even differ.
    """
    if markup is None or not enabled():
        return markup
    from aiogram.types import InlineKeyboardMarkup

    with _lock:
        mapping = dict(_map)

    changed = False
    rows = []
    for row in markup.inline_keyboard:
        buttons = []
        for button in row:
            emoticon = next(
                (e for e in sorted(mapping, key=len, reverse=True) if button.text.startswith(e)),
                None,
            )
            remainder = button.text[len(emoticon) :].strip() if emoticon else ""
            if button.icon_custom_emoji_id is not None:
                # Already carries an icon an operator chose by hand.
                buttons.append(button)
                continue
            if emoticon is None or not remainder:
                # No mapped emoji, or nothing but the emoji: a button must keep
                # visible text, so it stays exactly as designed.
                buttons.append(button)
                continue
            changed = True
            buttons.append(
                button.model_copy(
                    update={
                        "text": remainder,
                        "icon_custom_emoji_id": mapping[emoticon],
                    }
                )
            )
        rows.append(buttons)
    if not changed:
        return markup
    return InlineKeyboardMarkup(inline_keyboard=rows)
