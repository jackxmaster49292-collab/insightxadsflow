"""Links to a message this account posted.

Three cases, and the differences matter to anyone keeping an archive:

* **A public supergroup or channel** has a username, so
  ``t.me/<username>/<id>`` resolves for anybody, forever. This is the only form
  that outlives the account that posted it.
* **A private supergroup** has no username, but Telegram still addresses it as
  ``t.me/c/<internal id>/<id>``. That link opens only for *members* of the
  group — which is the whole point of it being private, and also its limit as a
  backup: it is worthless from an account that is no longer in the group.
* **A basic group** has neither. Telegram publishes no message-link form for
  them at all, so there is nothing to build and saying so is the only honest
  option.

``peer_id`` is stored in Bot-API form, where a supergroup is ``-100`` followed
by its internal id. That prefix is what ``t.me/c/`` wants removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Telegram's prefix for supergroup and channel ids in Bot-API numbering.
_CHANNEL_PREFIX = "-100"


class LinkKind(StrEnum):
    #: Resolves for anyone, and keeps resolving after the poster is gone.
    public = "public"
    #: Resolves for members of the group only.
    members_only = "members_only"
    #: Telegram has no link form for this chat.
    none = "none"


@dataclass(frozen=True, slots=True)
class MessageLink:
    kind: LinkKind
    url: str | None

    @property
    def durable(self) -> bool:
        """Would this still work if the posting account disappeared?"""
        return self.kind is LinkKind.public


def chat_link(*, chat_kind: str, peer_id: int, username: str | None) -> MessageLink:
    """A link to the chat itself.

    Same three cases as a message link and the same caveats: a username opens
    for anyone, ``t.me/c/`` opens for members, and a basic group has no form at
    all.
    """
    if username:
        return MessageLink(LinkKind.public, f"https://t.me/{username}")

    if chat_kind in ("supergroup", "channel"):
        internal = str(peer_id).removeprefix(_CHANNEL_PREFIX)
        if internal != str(peer_id) and internal.isdigit():
            return MessageLink(LinkKind.members_only, f"https://t.me/c/{internal}")

    return MessageLink(LinkKind.none, None)


def link_for(
    *,
    chat_kind: str,
    peer_id: int,
    username: str | None,
    message_id: int | None,
) -> MessageLink:
    """The best link to one posted message, and how far it can be trusted."""
    if not message_id:
        return MessageLink(LinkKind.none, None)

    if username:
        return MessageLink(LinkKind.public, f"https://t.me/{username}/{message_id}")

    if chat_kind in ("supergroup", "channel"):
        internal = str(peer_id).removeprefix(_CHANNEL_PREFIX)
        # A supergroup id that does not carry the prefix is not one this form
        # can address; guessing would produce a link to some other chat.
        if internal != str(peer_id) and internal.isdigit():
            return MessageLink(LinkKind.members_only, f"https://t.me/c/{internal}/{message_id}")

    return MessageLink(LinkKind.none, None)
