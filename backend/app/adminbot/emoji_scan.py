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
from typing import Any

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


#: Every module whose strings reach a person, in the order they are read.
#:
#: The screens and prompts are the bulk of it, but they are not all of it: an
#: alert and the warning shown before a login code are messages too. Leaving
#: those out meant the one message that arrives *unasked* was the one still
#: carrying a plain icon, on a panel where everything else was premium.
#:
#: Deliberately not every module. ``premium_icons`` and this file both spell
#: emoji out in their own documentation, and a ``👨‍👩‍👧`` used to explain what a
#: ZWJ sequence is would sit in the operator's "no premium version" list for
#: ever, describing nothing on any screen.
_SOURCES: tuple[str, ...] = (
    "app.adminbot.views",
    "app.adminbot.handlers",
    "app.adminbot.notifier",
    "app.adminbot.secrets",
    "app.services.archive",
)

#: Module-level names holding button labels. Their strings become the text of a
#: button without passing through an ``InlineKeyboardButton(...)`` call, so the
#: call-site scan alone would file them as message text.
_BUTTON_LABEL_NAMES = frozenset({"RENAMEABLE_BUTTONS", "BUTTON_STYLES"})

#: Where an emoji is drawn. A single one is usually both.
TEXT = "text"
BUTTON = "button"


def _sources() -> list[tuple[str, str]]:
    import importlib
    import inspect

    out = []
    for name in _SOURCES:
        module = importlib.import_module(name)
        out.append((name, inspect.getsource(module)))
    return out


def panel_emoji() -> tuple[str, ...]:
    """Every emoji this bot draws for a person, anywhere.

    Read from the source rather than by rendering every screen: cheaper, and it
    catches the ones inside branches a test would have to contrive to reach.
    Matching over the raw text rather than over parsed strings is the generous
    choice on purpose — an extra lookup for an emoji that only appears in a
    comment costs half a second once, and a missed one is a plain icon sitting
    among premium ones for ever.
    """
    return tuple(emoji_in("".join(source for _name, source in _sources())))


def _button_label_nodes(tree: Any) -> set[int]:
    """Ids of the string nodes that end up as a button's label.

    Two ways a string gets there: passed as ``text=`` to an
    ``InlineKeyboardButton``, or held in one of the module-level tuples of
    labels. Both are matched syntactically, which is why this reads the tree
    rather than the text.
    """
    import ast

    marked: set[int] = set()

    def mark(node: Any) -> None:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                marked.add(id(inner))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "InlineKeyboardButton":
                for keyword in node.keywords:
                    if keyword.arg == "text":
                        mark(keyword.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name) and node.target.id in _BUTTON_LABEL_NAMES:
                mark(node.value)
        elif isinstance(node, ast.Assign):
            named = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if named & _BUTTON_LABEL_NAMES:
                mark(node.value)

    return marked


def emoji_places() -> dict[str, frozenset[str]]:
    """Where each emoji is drawn — in message text, on a button, or both.

    Only for telling an operator apart what they are looking at. Nothing about
    the transform depends on it: an emoji is replaced wherever it appears, and
    a wrong label here would be a wrong caption, not a wrong icon.

    Read from string *literals* rather than the raw text, so a comment cannot
    claim an emoji appears somewhere it does not.
    """
    import ast

    places: dict[str, set[str]] = {}
    for _name, source in _sources():
        tree = ast.parse(source)
        marked = _button_label_nodes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            where = BUTTON if id(node) in marked else TEXT
            for emoji in emoji_in(node.value):
                places.setdefault(emoji, set()).add(where)
    return {emoji: frozenset(where) for emoji, where in places.items()}
