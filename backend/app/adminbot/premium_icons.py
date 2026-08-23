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

_lock = threading.Lock()
_map: dict[str, str] = {}
_suspended = False

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
