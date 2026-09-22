"""The Telegram adapter boundary.

No Telethon or aiogram type crosses this interface, and nothing outside
``app/adapters/`` may import those libraries. Everything below is our own
vocabulary, which is what lets the entire system be tested against
:class:`~app.adapters.mock.MockAdapter`.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.adapters.errors import ClassifiedError


class PeerKind(enum.StrEnum):
    user = "user"
    chat = "chat"
    channel = "channel"


class MediaType(enum.StrEnum):
    text = "text"
    photo = "photo"
    video = "video"
    document = "document"
    audio = "audio"
    voice = "voice"
    poll = "poll"
    sticker = "sticker"
    animation = "animation"
    location = "location"
    contact = "contact"
    service = "service"
    other = "other"


#: Types the MVP will not deliver, with the reason surfaced to the customer.
UNSUPPORTED_MEDIA: frozenset[MediaType] = frozenset({MediaType.service})


@dataclass(frozen=True, slots=True)
class ChatRef:
    """A Telegram peer.

    ``peer_id`` is never used alone: Telegram's peer documentation states the
    64-bit id sequences of users, chats and channels overlap.
    """

    peer_type: PeerKind
    peer_id: int
    access_hash: int | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.peer_type.value, self.peer_id)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.peer_type.value}:{self.peer_id}"


@dataclass(slots=True)
class DiscoveredChat:
    ref: ChatRef
    title: str
    chat_kind: str
    username: str | None = None
    is_public: bool | None = None
    has_protected_content: bool | None = None

    #: What the discovery pass could already tell about rights, if anything.
    #:
    #: Telegram puts an account's posting rights on the chat object itself, so
    #: a listing that returns chat objects has already been told. ``None`` means
    #: this adapter could not say and the check has to be made per chat — one
    #: network round trip each, which is what 735 groups made unaffordable.
    posting: AccessReport | None = None
    reading: AccessReport | None = None


@dataclass(frozen=True, slots=True)
class AccessReport:
    """Result of an eligibility check. ``reason_code`` is a closed vocabulary."""

    allowed: bool
    reason_code: str

    @classmethod
    def ok(cls) -> AccessReport:
        return cls(True, "ok")

    @classmethod
    def denied(cls, reason_code: str) -> AccessReport:
        return cls(False, reason_code)


@dataclass(slots=True)
class InboundMessage:
    """One logical source message. An album arrives as a single instance."""

    source: ChatRef
    message_ids: list[int]
    media_type: MediaType
    text: str = ""
    has_protected_content: bool = False
    grouped_id: int | None = None
    partial_album: bool = False

    #: Addresses that are in the message but not in its text — a hyperlink
    #: behind words. A promotional post very often reads "👉 Join here" with
    #: the address only in the entity, so text alone misses exactly the
    #: messages a link scan is for. URLs only; no other part of an entity is
    #: carried across this boundary.
    entity_urls: list[str] = field(default_factory=list)

    @property
    def primary_id(self) -> int:
        return min(self.message_ids)


@dataclass(slots=True)
class DeliveryReceipt:
    destination_message_id: int | None = None


@dataclass(slots=True)
class HealthReport:
    healthy: bool
    reason_code: str = "ok"
    account_id: int | None = None
    username: str | None = None
    #: Telegram Premium. Only a premium account may send custom emoji, so this
    #: decides whether an ad written with them will render as intended.
    premium: bool = False


@dataclass(frozen=True, slots=True)
class ChatDetails:
    """What a chat publishes about itself.

    ``member_count`` is a *number* Telegram already publishes, not a roster —
    the difference between describing a group and collecting its people.
    """

    description: str | None = None
    member_count: int | None = None


@dataclass(frozen=True, slots=True)
class CodeDelivery:
    """Where Telegram actually sent the login code.

    Not a detail. Telegram sends the code to the **Telegram app** on any other
    device that account is already signed in on, and only falls back to SMS
    when there is none — so someone watching their text messages can wait for a
    code that was delivered minutes ago, to an app two inches away. Saying
    "a code has been sent" without saying where is the difference between a
    thirty-second step and a dead end.

    ``channel`` is one of ``app``, ``sms``, ``call``, ``missed_call``,
    ``fragment``, ``email`` or ``unknown``. ``next_channel`` is what Telegram
    would use if asked again, which is the way out when the first one cannot
    be reached.
    """

    phone_code_hash: str
    channel: str = "unknown"
    next_channel: str | None = None


@dataclass(frozen=True, slots=True)
class LinkPreview:
    """What Telegram will tell anyone holding a link, before they open it.

    Exactly the preview a person sees on tapping one: a title, a size, and what
    kind of thing it is. For an invite link that is all Telegram publishes
    without joining, and this asks for no more than that — nothing here opens
    a private chat or reads a word inside one.

    ``chat_kind`` may come back ``user``, which is how a ``t.me/name`` that
    turns out to be a person is recognised and dropped.
    """

    title: str | None = None
    member_count: int | None = None
    chat_kind: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class TextEntity:
    """One piece of formatting in a message.

    Our own shape, because a Telegram entity type must not cross this boundary.
    ``offset`` and ``length`` are in **UTF-16 code units**, which is what both
    the Bot API and MTProto use — passing them through unchanged is therefore
    correct, and recomputing them in Python's code points would silently
    corrupt any message containing an emoji.

    ``custom_emoji_id`` carries a premium emoji. Sending one requires Telegram
    Premium on the account doing the sending; Telegram rejects it otherwise,
    which is surfaced rather than silently dropped.
    """

    type: str
    offset: int
    length: int
    url: str | None = None
    custom_emoji_id: str | None = None
    language: str | None = None

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type, "offset": self.offset, "length": self.length}
        for name in ("url", "custom_emoji_id", "language"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TextEntity:
        return cls(
            type=str(data["type"]),
            offset=int(data["offset"]),
            length=int(data["length"]),
            url=data.get("url"),
            custom_emoji_id=data.get("custom_emoji_id"),
            language=data.get("language"),
        )


@dataclass(slots=True)
class ConnectionState:
    status: str
    account_id: int | None = None
    username: str | None = None
    #: New session material to persist, if the flow produced any.
    session_string: str | None = None
    #: Telegram Premium — see HealthReport.premium.
    premium: bool = False


@dataclass(slots=True)
class Capabilities:
    can_read_subscribed_channels: bool
    can_read_group_messages: bool
    can_read_history: bool
    max_download_bytes: int | None
    notes: list[str] = field(default_factory=list)


class AmbiguousDeliveryError(Exception):
    """The request timed out and the delivery may or may not have happened.

    Raised only where the provider offers no idempotency token (the Bot API).
    The worker fails closed on this — see docs/OPERATIONS.md §4.
    """


@runtime_checkable
class TelegramAdapter(Protocol):
    """Implemented by BotAdapter, UserAdapter and MockAdapter."""

    kind: str

    async def connect(self) -> ConnectionState: ...

    async def disconnect(self, *, revoke: bool = False) -> None: ...

    async def health_check(self) -> HealthReport: ...

    async def list_available_chats(self) -> list[DiscoveredChat]: ...

    async def check_source_access(self, ref: ChatRef) -> AccessReport: ...

    async def check_destination_access(self, ref: ChatRef) -> AccessReport: ...

    async def custom_emoji_ids(self, emoticon: str) -> list[str]:
        """Custom-emoji document ids Telegram offers for one unicode emoji.

        Empty when the provider cannot search (the Bot API has no such method)
        or when nothing matches. Ids are Telegram documents, so extraction is
        the only honest source — there is no table to ship.
        """
        ...

    async def chat_details(self, ref: ChatRef) -> ChatDetails:
        """A chat's own description and size — metadata, never its people.

        Deliberately not a member list: this asks Telegram what the *chat*
        says about itself, which is what identifies a private group again
        months later when its title alone does not.
        """
        ...

    async def preview_link(self, kind: str, key: str) -> LinkPreview:
        """The title and size behind a ``t.me`` link, without opening it.

        ``kind`` is ``public`` (``key`` is a username) or ``invite`` (``key``
        is the hash). Both return only what Telegram already shows anyone who
        taps the link — nothing is joined, and nothing inside is read. Empty
        when the provider cannot look one up.
        """
        ...

    async def installed_custom_emoji(self) -> dict[str, str]:
        """Every custom emoji this account owns, keyed by its fallback emoji.

        A different and much richer source than searching one emoticon at a
        time: each document in an installed emoji pack carries the plain emoji
        it stands in for, so one pass over the packs maps most of a panel at
        once. Empty when the provider cannot read packs.
        """
        ...

    def receive_new_messages(self) -> AsyncIterator[InboundMessage]: ...

    async def forward_message(
        self,
        source: ChatRef,
        message_ids: list[int],
        destination: ChatRef,
        *,
        random_id: int | None = None,
    ) -> DeliveryReceipt: ...

    async def send_supported_content(
        self,
        source: ChatRef,
        message_ids: list[int],
        destination: ChatRef,
        *,
        preserve_caption: bool = True,
    ) -> DeliveryReceipt: ...

    async def send_text(
        self,
        destination: ChatRef,
        text: str,
        *,
        entities: Sequence[TextEntity] = (),
        random_id: int | None = None,
    ) -> DeliveryReceipt:
        """Send an original message the customer wrote.

        Unlike the forward/copy pair above, nothing here originates from another
        chat, so there is no source peer and no content-protection question.

        ``entities`` reproduces the bold, links and premium emoji exactly as the
        customer typed them. Passed as data rather than re-parsed from markup:
        round-tripping through Markdown would mangle any text that happens to
        contain an asterisk or an underscore.
        """
        ...

    async def send_photo(
        self,
        destination: ChatRef,
        photo: bytes,
        *,
        caption: str = "",
        caption_entities: Sequence[TextEntity] = (),
        filename: str = "image.jpg",
        random_id: int | None = None,
    ) -> DeliveryReceipt:
        """Send an image the customer uploaded, with an optional caption.

        Takes bytes rather than a file identifier on purpose: a Telegram
        ``file_id`` is scoped to the bot that received it, so the id the admin
        bot sees is meaningless to the connection doing the sending.
        """
        ...

    def classify_error(self, exc: BaseException) -> ClassifiedError: ...

    def capabilities(self) -> Capabilities: ...
