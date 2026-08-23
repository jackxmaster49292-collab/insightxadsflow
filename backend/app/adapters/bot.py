"""Bot API adapter (aiogram 3.30, Bot API 10.2).

Notable real constraints, handled honestly rather than papered over:

* **A bot cannot enumerate its own chats.** The Bot API has no "list my chats"
  method. Chats are therefore discovered from updates as they arrive
  (``my_chat_member``, ``message``, ``channel_post``) and the UI says so.
* A bot receives all messages from channels where it is a member, but in groups
  it only sees commands and replies unless it is an admin or privacy mode is off.
* ``getFile`` caps downloads at 20 MB.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from app.adapters.base import (
    AccessReport,
    AmbiguousDeliveryError,
    Capabilities,
    ChatRef,
    ConnectionState,
    DeliveryReceipt,
    DiscoveredChat,
    HealthReport,
    InboundMessage,
    MediaType,
    PeerKind,
    TextEntity,
)
from app.adapters.capabilities import capabilities_for
from app.adapters.errors import AdapterError, ClassifiedError, ErrorClass, classify_error
from app.domain import reasons

log = structlog.get_logger(__name__)

ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "my_chat_member",
]

#: Administrator statuses that imply the bot can act in a chat.
_ADMIN_STATUSES = {"administrator", "creator"}
_PRESENT_STATUSES = _ADMIN_STATUSES | {"member", "restricted"}


def _chat_kind(telegram_type: str) -> str:
    return {
        "private": "private",
        "group": "group",
        "supergroup": "supergroup",
        "channel": "channel",
    }.get(telegram_type, "other")


def _peer_kind(telegram_type: str) -> PeerKind:
    if telegram_type == "private":
        return PeerKind.user
    if telegram_type == "group":
        return PeerKind.chat
    return PeerKind.channel


def detect_media_type(message: Any) -> MediaType:
    """Map a Bot API message onto our media vocabulary."""
    for attribute, media in (
        ("photo", MediaType.photo),
        ("video", MediaType.video),
        ("animation", MediaType.animation),
        ("audio", MediaType.audio),
        ("voice", MediaType.voice),
        ("document", MediaType.document),
        ("poll", MediaType.poll),
        ("sticker", MediaType.sticker),
        ("location", MediaType.location),
        ("contact", MediaType.contact),
    ):
        if getattr(message, attribute, None) is not None:
            return media
    if getattr(message, "text", None):
        return MediaType.text
    # Service messages (joins, pins, title changes) carry none of the above.
    return MediaType.service


def _to_bot_entities(entities: Sequence[TextEntity]) -> list[Any]:
    """Our neutral entities as Bot API ones.

    Offsets are passed through untouched: both sides count in UTF-16 code
    units, so recomputing them would be the bug rather than the fix.
    """
    from aiogram.types import MessageEntity

    return [
        MessageEntity(
            type=entity.type,
            offset=entity.offset,
            length=entity.length,
            url=entity.url,
            custom_emoji_id=entity.custom_emoji_id,
            language=entity.language,
        )
        for entity in entities
    ]


class BotAdapter:
    kind = "bot"

    def __init__(self, token: str, *, poll_timeout_s: int = 25) -> None:
        self._bot = Bot(token=token, default=DefaultBotProperties())
        self._poll_timeout_s = poll_timeout_s
        self._offset: int | None = None
        self._account_id: int | None = None
        #: Chats learned from updates, since the Bot API cannot enumerate them.
        self._discovered: dict[tuple[str, int], DiscoveredChat] = {}

    # --- lifecycle ------------------------------------------------------- #
    async def connect(self) -> ConnectionState:
        me = await self._bot.get_me()
        self._account_id = me.id
        return ConnectionState(status="active", account_id=me.id, username=me.username)

    async def disconnect(self, *, revoke: bool = False) -> None:
        if revoke:
            # Drops any webhook and abandons the pending update queue.
            await self._bot.delete_webhook(drop_pending_updates=True)
        await self._bot.session.close()

    async def health_check(self) -> HealthReport:
        try:
            me = await self._bot.get_me()
        except Exception as exc:
            return HealthReport(healthy=False, reason_code=classify_error(exc).code)
        return HealthReport(healthy=True, account_id=me.id, username=me.username)

    # --- discovery ------------------------------------------------------- #
    async def list_available_chats(self) -> list[DiscoveredChat]:
        """Only what updates have revealed. See the module docstring."""
        return list(self._discovered.values())

    def remember_chat(self, chat: Any) -> DiscoveredChat:
        ref = ChatRef(_peer_kind(getattr(chat, "type", "")), int(chat.id))
        discovered = DiscoveredChat(
            ref=ref,
            title=getattr(chat, "title", None)
            or getattr(chat, "full_name", None)
            or str(ref.peer_id),
            chat_kind=_chat_kind(getattr(chat, "type", "")),
            username=getattr(chat, "username", None),
            is_public=bool(getattr(chat, "username", None)),
            has_protected_content=getattr(chat, "has_protected_content", None),
        )
        self._discovered[ref.key] = discovered
        return discovered

    async def _member_status(self, ref: ChatRef) -> str | None:
        if self._account_id is None:
            await self.connect()
        assert self._account_id is not None
        member = await self._bot.get_chat_member(ref.peer_id, self._account_id)
        return getattr(member, "status", None)

    async def check_source_access(self, ref: ChatRef) -> AccessReport:
        try:
            chat = await self._bot.get_chat(ref.peer_id)
            self.remember_chat(chat)
            status = await self._member_status(ref)
        except Exception as exc:
            return AccessReport.denied(classify_error(exc).code)

        if status not in _PRESENT_STATUSES:
            return AccessReport.denied(reasons.NOT_A_MEMBER)

        chat_type = getattr(chat, "type", "")
        if chat_type == "channel":
            # A bot receives all messages from channels where it is a member.
            return AccessReport.ok()
        if chat_type in ("group", "supergroup"):
            # Otherwise it only sees commands and replies.
            if status in _ADMIN_STATUSES:
                return AccessReport.ok()
            return AccessReport.denied(reasons.PRIVACY_MODE_ENABLED)
        return AccessReport.denied(reasons.NOT_A_MEMBER)

    async def check_destination_access(self, ref: ChatRef) -> AccessReport:
        try:
            chat = await self._bot.get_chat(ref.peer_id)
            self.remember_chat(chat)
            status = await self._member_status(ref)
        except Exception as exc:
            return AccessReport.denied(classify_error(exc).code)

        chat_type = getattr(chat, "type", "")
        if status not in _PRESENT_STATUSES:
            return AccessReport.denied(reasons.NOT_A_MEMBER)
        if chat_type == "channel" and status not in _ADMIN_STATUSES:
            return AccessReport.denied(reasons.BOT_NOT_ADMIN)
        if status == "restricted" and not getattr(status, "can_send_messages", True):
            return AccessReport.denied(reasons.WRITE_FORBIDDEN)
        return AccessReport.ok()

    # --- intake ---------------------------------------------------------- #
    async def receive_new_messages(self) -> AsyncIterator[InboundMessage]:
        """Persistent long polling. The caller commits the offset durably before
        acknowledging, so a crash re-reads rather than loses."""
        while True:
            updates = await self._bot.get_updates(
                offset=self._offset,
                timeout=self._poll_timeout_s,
                allowed_updates=ALLOWED_UPDATES,
            )
            for update in updates:
                self._offset = update.update_id + 1
                message = getattr(update, "message", None) or getattr(update, "channel_post", None)
                if message is None:
                    member_update = getattr(update, "my_chat_member", None)
                    if member_update is not None:
                        self.remember_chat(member_update.chat)
                    continue

                chat = message.chat
                self.remember_chat(chat)
                yield InboundMessage(
                    source=ChatRef(_peer_kind(chat.type), int(chat.id)),
                    message_ids=[int(message.message_id)],
                    media_type=detect_media_type(message),
                    text=message.text or message.caption or "",
                    has_protected_content=bool(getattr(chat, "has_protected_content", False)),
                    grouped_id=(
                        int(message.media_group_id)
                        if getattr(message, "media_group_id", None)
                        else None
                    ),
                )

    @property
    def offset(self) -> int | None:
        return self._offset

    @offset.setter
    def offset(self, value: int | None) -> None:
        self._offset = value

    # --- delivery -------------------------------------------------------- #
    async def forward_message(
        self,
        source: ChatRef,
        message_ids: list[int],
        destination: ChatRef,
        *,
        random_id: int | None = None,
    ) -> DeliveryReceipt:
        # random_id is an MTProto concept; the Bot API offers no idempotency
        # token, which is exactly why an ambiguous timeout fails closed below.
        try:
            if len(message_ids) == 1:
                sent = await self._bot.forward_message(
                    chat_id=destination.peer_id,
                    from_chat_id=source.peer_id,
                    message_id=message_ids[0],
                )
                return DeliveryReceipt(destination_message_id=int(sent.message_id))

            results = await self._bot.forward_messages(
                chat_id=destination.peer_id,
                from_chat_id=source.peer_id,
                # Telegram requires strictly increasing identifiers.
                message_ids=sorted(message_ids),
            )
            first = results[0] if results else None
            return DeliveryReceipt(destination_message_id=int(first.message_id) if first else None)
        except TimeoutError as exc:
            raise AmbiguousDeliveryError(reasons.AMBIGUOUS_TIMEOUT) from exc

    async def send_supported_content(
        self,
        source: ChatRef,
        message_ids: list[int],
        destination: ChatRef,
        *,
        preserve_caption: bool = True,
    ) -> DeliveryReceipt:
        """Copy mode. Deliberately refuses grouped media, which ``copyMessage``
        cannot reproduce faithfully as a single album."""
        if len(message_ids) > 1:
            raise AdapterError(reasons.UNCOPYABLE_MESSAGE, ErrorClass.PERMANENT_CONTENT)
        try:
            sent = await self._bot.copy_message(
                chat_id=destination.peer_id,
                from_chat_id=source.peer_id,
                message_id=message_ids[0],
            )
            return DeliveryReceipt(destination_message_id=int(sent.message_id))
        except TimeoutError as exc:
            raise AmbiguousDeliveryError(reasons.AMBIGUOUS_TIMEOUT) from exc

    async def custom_emoji_ids(self, emoticon: str) -> list[str]:
        # The Bot API has no emoji search. Saying so is better than guessing.
        return []

    async def send_text(
        self,
        destination: ChatRef,
        text: str,
        *,
        entities: Sequence[TextEntity] = (),
        random_id: int | None = None,
    ) -> DeliveryReceipt:
        # random_id is accepted for interface symmetry and ignored: the Bot API
        # has no idempotency token, which is why a timeout fails closed here.
        try:
            sent = await self._bot.send_message(
                chat_id=destination.peer_id,
                text=text,
                # Entities and parse_mode are mutually exclusive on the Bot API
                # too; passing the text verbatim is the whole point.
                entities=_to_bot_entities(entities) or None,
                parse_mode=None,
            )
            return DeliveryReceipt(destination_message_id=int(sent.message_id))
        except TimeoutError as exc:
            raise AmbiguousDeliveryError(reasons.AMBIGUOUS_TIMEOUT) from exc

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
        from aiogram.types import BufferedInputFile

        try:
            sent = await self._bot.send_photo(
                chat_id=destination.peer_id,
                photo=BufferedInputFile(photo, filename=filename),
                caption=caption or None,
                caption_entities=_to_bot_entities(caption_entities) or None,
                parse_mode=None,
            )
            return DeliveryReceipt(destination_message_id=int(sent.message_id))
        except TimeoutError as exc:
            raise AmbiguousDeliveryError(reasons.AMBIGUOUS_TIMEOUT) from exc

    # --- misc ------------------------------------------------------------ #
    def classify_error(self, exc: BaseException) -> ClassifiedError:
        return classify_error(exc)

    def capabilities(self) -> Capabilities:
        return capabilities_for(self.kind)
