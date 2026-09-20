"""Telegram chat links found in message text.

Only links to *chats* — a group or a channel someone posted about. Everything
else a message contains is ignored and nothing about the sender is looked at,
because the point is to notice which chats keep being mentioned, not who
mentions them.

Four spellings reach the same place, and Telegram treats them as one:

* ``t.me/name`` — a public chat, addressable by username;
* ``t.me/name/451`` — one message inside it, so the chat is the part before;
* ``t.me/+hash`` and ``t.me/joinchat/hash`` — an invite to a private chat,
  where the hash *is* the address and there is no username to be had;
* ``tg://resolve?domain=name`` — the same as the first, in Telegram's own
  scheme, which is what a button or a forwarded post often carries.

Normalizing them onto one key is the whole job here. Without it ``t.me/Deals``,
``t.me/deals.`` at the end of a sentence, and ``telegram.me/deals`` count as
three chats, and the count is the entire signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: Usernames Telegram will not let anyone hold, or that address something that
#: is not a chat. ``joinchat`` and ``addstickers`` are paths, not names, and
#: ``share``/``proxy``/``socks`` are Telegram's own utility links.
_RESERVED = frozenset(
    {
        "joinchat",
        "addstickers",
        "addemoji",
        "addtheme",
        "share",
        "proxy",
        "socks",
        "login",
        "confirmphone",
        "setlanguage",
        "bg",
        "invoice",
        "giftcode",
        "boost",
        "c",
    }
)

#: Telegram's own rule: 5–32 characters, letters, digits and underscores, and
#: it may not start with a digit. Matching loosely here would turn every
#: ``t.me/`` typo into a chat nobody can open.
_USERNAME = r"[A-Za-z][A-Za-z0-9_]{3,31}"

#: An invite hash. Telegram's are base64url-ish and around 16 characters; the
#: bound is generous because the format has changed before.
_HASH = r"[A-Za-z0-9_-]{8,64}"

_PATTERNS = (
    re.compile(rf"(?:https?://)?(?:t|telegram)\.me/\+({_HASH})", re.IGNORECASE),
    re.compile(rf"(?:https?://)?(?:t|telegram)\.me/joinchat/({_HASH})", re.IGNORECASE),
    re.compile(rf"(?:https?://)?(?:t|telegram)\.me/({_USERNAME})", re.IGNORECASE),
    re.compile(rf"tg://resolve\?domain=({_USERNAME})", re.IGNORECASE),
    re.compile(rf"tg://join\?invite=({_HASH})", re.IGNORECASE),
)

#: Which patterns above produce an invite hash rather than a username.
_INVITE_PATTERNS = {0, 1, 4}


class LinkKind(StrEnum):
    #: Has a username, so it can be looked up and opened by anyone.
    public = "public"
    #: An invite hash. Telegram will show its title to anyone holding the link,
    #: and nothing more without joining.
    invite = "invite"


@dataclass(frozen=True, slots=True)
class ChatLink:
    kind: LinkKind
    #: The username or the invite hash, lowercased for ``public`` so that one
    #: chat written three ways counts once. An invite hash is case-sensitive
    #: and is left exactly as it was.
    key: str

    @property
    def url(self) -> str:
        if self.kind is LinkKind.invite:
            return f"https://t.me/+{self.key}"
        return f"https://t.me/{self.key}"


def links_in(text: str) -> list[ChatLink]:
    """Every distinct chat link in ``text``, in the order it first appears.

    Order is kept because the first link in a promotional message is usually
    the one being promoted; the rest are the poster's other chats.
    """
    found: list[ChatLink] = []
    seen: set[tuple[str, str]] = set()

    for index, pattern in enumerate(_PATTERNS):
        for match in pattern.finditer(text or ""):
            raw = match.group(1)
            if index in _INVITE_PATTERNS:
                link = ChatLink(LinkKind.invite, raw)
            else:
                name = raw.lower()
                if name in _RESERVED:
                    continue
                link = ChatLink(LinkKind.public, name)
            identity = (link.kind.value, link.key)
            if identity not in seen:
                seen.add(identity)
                found.append(link)

    # Ordered by where each first appeared, not by which pattern matched it:
    # the patterns run invite-first so that ``t.me/+hash`` is not read as the
    # username ``+hash``, and that order says nothing about the message.
    positions: dict[tuple[str, str], int] = {}
    for link in found:
        needle = f"+{link.key}" if link.kind is LinkKind.invite else link.key
        positions[(link.kind.value, link.key)] = (text or "").lower().find(needle.lower())
    return sorted(found, key=lambda link: positions[(link.kind.value, link.key)])


def links_in_message(text: str, entity_urls: list[str] | None = None) -> list[ChatLink]:
    """Links in the visible text *and* in any hidden behind it.

    A promotional message very often reads ``👉 Join here`` with the address
    only in the entity, so text alone misses exactly the messages this is for.
    """
    combined = "\n".join([text or "", *(entity_urls or [])])
    return links_in(combined)
