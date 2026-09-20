"""MTProto adapter (Telethon 1.44).

Session material is held as a ``StringSession`` so nothing is written to disk;
the caller persists it envelope-encrypted.

``access_hash`` is account-specific, so it is returned with each discovered chat
and stored per connection. Without it, a private channel cannot be addressed at
all after a process restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import structlog
from telethon import TelegramClient, events, functions, types, utils
from telethon.sessions import StringSession

from app.adapters.base import (
    AccessReport,
    Capabilities,
    ChatDetails,
    ChatRef,
    ConnectionState,
    DeliveryReceipt,
    DiscoveredChat,
    HealthReport,
    InboundMessage,
    LinkPreview,
    MediaType,
    PeerKind,
    TextEntity,
)
from app.adapters.capabilities import capabilities_for
from app.adapters.errors import AdapterError, ClassifiedError, ErrorClass, classify_error
from app.domain import reasons

log = structlog.get_logger(__name__)


class LoginCodeRequired(Exception):
    def __init__(self, phone_code_hash: str) -> None:
        super().__init__("login_code_required")
        self.phone_code_hash = phone_code_hash


class TwoFactorRequired(Exception):
    pass


def _peer_kind(entity: Any) -> PeerKind:
    if isinstance(entity, types.User):
        return PeerKind.user
    if isinstance(entity, types.Chat):
        return PeerKind.chat
    return PeerKind.channel


def _chat_kind(entity: Any) -> str:
    if isinstance(entity, types.User):
        return "private"
    if isinstance(entity, types.Chat):
        return "group"
    if isinstance(entity, types.Channel):
        return "channel" if getattr(entity, "broadcast", False) else "supergroup"
    return "other"


def _entity_urls(message: Any) -> list[str]:
    """Addresses hidden behind the words of a message.

    ``MessageEntityTextUrl`` is the one that matters — a hyperlink whose text
    says something else entirely, which is how nearly every promotional post
    is written. Nothing else about an entity crosses this boundary.
    """
    urls: list[str] = []
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            urls.append(str(url))
    return urls


def posting_verdict(entity: Any) -> AccessReport:
    """Whether this account may post in a chat, read off the chat itself.

    Telegram carries an account's rights on the chat object — ``left``, the
    ban it applies to everyone, the ban it applies to you, and your admin
    rights if any. So the answer arrives with the chat and no question needs
    asking.

    One implementation, shared by the discovery pass and the per-chat check.
    Two would eventually disagree about what "can post" means, and the
    disagreement would show as a group the picker offers and every delivery
    refuses.
    """
    if isinstance(entity, types.Channel):
        if getattr(entity, "left", False):
            return AccessReport.denied(reasons.NOT_A_MEMBER)
        banned = getattr(entity, "banned_rights", None)
        if banned is not None and getattr(banned, "send_messages", False):
            return AccessReport.denied(reasons.WRITE_FORBIDDEN)
        if getattr(entity, "broadcast", False):
            rights = getattr(entity, "admin_rights", None)
            if rights is None or not getattr(rights, "post_messages", False):
                return AccessReport.denied(reasons.ADMIN_REQUIRED)
    default_banned = getattr(entity, "default_banned_rights", None)
    if default_banned is not None and getattr(default_banned, "send_messages", False):
        admin = getattr(entity, "admin_rights", None)
        if admin is None:
            return AccessReport.denied(reasons.WRITE_FORBIDDEN)
    return AccessReport.ok()


def reading_verdict(entity: Any) -> AccessReport:
    """Whether this account can read a chat, read off the chat itself.

    Membership is the whole of it: a chat in the dialog list is one the
    account is in, and being in it is what lets it read. The old proof was to
    fetch one message, which is true but costs a round trip per chat to learn
    something the listing already implied — and a chat left behind still says
    so on the object.

    Content protection is *not* decided here. It is a property of the chat
    rather than of this account, it is recorded separately, and the caller
    applies it to both this verdict and the stored one.
    """
    if getattr(entity, "left", False):
        return AccessReport.denied(reasons.NOT_A_MEMBER)
    return AccessReport.ok()


def detect_media_type(message: Any) -> MediaType:
    media = getattr(message, "media", None)
    if media is None:
        return MediaType.text if getattr(message, "message", None) else MediaType.service
    if isinstance(media, types.MessageMediaPhoto):
        return MediaType.photo
    if isinstance(media, types.MessageMediaPoll):
        return MediaType.poll
    if isinstance(media, types.MessageMediaGeo):
        return MediaType.location
    if isinstance(media, types.MessageMediaContact):
        return MediaType.contact
    if isinstance(media, types.MessageMediaDocument):
        document = getattr(media, "document", None)
        for attribute in getattr(document, "attributes", []) or []:
            if isinstance(attribute, types.DocumentAttributeVideo):
                return MediaType.animation if attribute.round_message else MediaType.video
            if isinstance(attribute, types.DocumentAttributeAudio):
                return MediaType.voice if attribute.voice else MediaType.audio
            if isinstance(attribute, types.DocumentAttributeSticker):
                return MediaType.sticker
            if isinstance(attribute, types.DocumentAttributeAnimated):
                return MediaType.animation
        return MediaType.document
    return MediaType.other


#: Bot API entity names mapped to the MTProto classes that carry them. The two
#: APIs describe the same formatting with different vocabularies, and this is
#: the only place that has to know both.
_MTPROTO_ENTITIES: dict[str, str] = {
    "bold": "MessageEntityBold",
    "italic": "MessageEntityItalic",
    "underline": "MessageEntityUnderline",
    "strikethrough": "MessageEntityStrike",
    "spoiler": "MessageEntitySpoiler",
    "code": "MessageEntityCode",
    "blockquote": "MessageEntityBlockquote",
    "url": "MessageEntityUrl",
    "email": "MessageEntityEmail",
    "phone_number": "MessageEntityPhone",
    "mention": "MessageEntityMention",
    "hashtag": "MessageEntityHashtag",
    "cashtag": "MessageEntityCashtag",
    "bot_command": "MessageEntityBotCommand",
}


def _to_mtproto_entities(entities: Sequence[TextEntity]) -> list[Any]:
    """Our neutral entities as MTProto ones.

    Offsets pass through untouched — both APIs count UTF-16 code units.

    An entity type we do not recognise is dropped rather than guessed at: losing
    one piece of formatting is a far better outcome than Telegram rejecting the
    whole message and the ad not being posted at all.
    """
    built: list[Any] = []
    for entity in entities:
        if entity.type == "custom_emoji" and entity.custom_emoji_id:
            built.append(
                types.MessageEntityCustomEmoji(
                    offset=entity.offset,
                    length=entity.length,
                    document_id=int(entity.custom_emoji_id),
                )
            )
        elif entity.type == "text_link" and entity.url:
            built.append(
                types.MessageEntityTextUrl(
                    offset=entity.offset, length=entity.length, url=entity.url
                )
            )
        elif entity.type == "pre":
            built.append(
                types.MessageEntityPre(
                    offset=entity.offset, length=entity.length, language=entity.language or ""
                )
            )
        elif entity.type in _MTPROTO_ENTITIES:
            factory = getattr(types, _MTPROTO_ENTITIES[entity.type], None)
            if factory is not None:
                built.append(factory(offset=entity.offset, length=entity.length))
    return built


class UserAdapter:
    kind = "user"

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        session_string: str | None = None,
        album_buffer_ms: int = 2000,
    ) -> None:
        self._client = TelegramClient(StringSession(session_string), api_id, api_hash)
        self._album_buffer_s = album_buffer_ms / 1000
        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._albums: dict[int, list[object]] = {}

    @property
    def session_string(self) -> str:
        return str(self._client.session.save())

    async def _ready(self) -> None:
        """Connect if we are not already.

        Called at the top of every method that talks to Telegram. A fresh
        adapter is built per worker task, so the client starts disconnected each
        time and Telethon refuses requests in that state. Cheap when already
        connected — it is a flag check, not a round trip.
        """
        if not self._client.is_connected():
            await self._client.connect()

    # --- lifecycle ------------------------------------------------------- #
    async def connect(self) -> ConnectionState:
        await self._ready()
        if not await self._client.is_user_authorized():
            return ConnectionState(status="awaiting_code", session_string=self.session_string)
        me = await self._client.get_me()
        return ConnectionState(
            status="active",
            account_id=int(me.id),
            username=getattr(me, "username", None),
            session_string=self.session_string,
            premium=bool(getattr(me, "premium", False)),
        )

    async def start_login(self, phone: str) -> str:
        """Sends the login code. Returns the ``phone_code_hash`` to carry forward.

        Worth knowing before choosing this over QR: Telegram cancels any login
        code it sees an account send inside a Telegram chat. Completing a
        sign-in by typing the code into a bot therefore fails with
        ``PhoneCodeInvalid`` even when the digits are correct — the code was
        burned in transit. That protection is deliberate and is not worked
        around here.
        """
        await self._ready()
        sent = await self._client.send_code_request(phone)
        return str(sent.phone_code_hash)

    async def complete_login(self, phone: str, code: str, phone_code_hash: str) -> ConnectionState:
        from telethon.errors import SessionPasswordNeededError

        await self._ready()
        try:
            await self._client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError as exc:
            raise TwoFactorRequired from exc
        me = await self._client.get_me()
        return ConnectionState(
            status="active",
            account_id=int(me.id),
            username=getattr(me, "username", None),
            session_string=self.session_string,
            premium=bool(getattr(me, "premium", False)),
        )

    async def complete_2fa(self, password: str) -> ConnectionState:
        """The password is used once, here, and never persisted in any form."""
        await self._ready()
        await self._client.sign_in(password=password)
        me = await self._client.get_me()
        return ConnectionState(
            status="active",
            account_id=int(me.id),
            username=getattr(me, "username", None),
            session_string=self.session_string,
            premium=bool(getattr(me, "premium", False)),
        )

    async def disconnect(self, *, revoke: bool = False) -> None:
        if revoke and self._client.is_connected():
            # Invalidates the session on Telegram's side, not just locally.
            await self._client(functions.auth.LogOutRequest())
        await self._client.disconnect()

    async def health_check(self) -> HealthReport:
        try:
            await self._ready()
            if not await self._client.is_user_authorized():
                return HealthReport(healthy=False, reason_code="session_revoked")
            me = await self._client.get_me()
        except Exception as exc:
            return HealthReport(healthy=False, reason_code=classify_error(exc).code)
        return HealthReport(
            healthy=True,
            account_id=int(me.id),
            username=getattr(me, "username", None),
            premium=bool(getattr(me, "premium", False)),
        )

    # --- discovery ------------------------------------------------------- #
    async def list_available_chats(self) -> list[DiscoveredChat]:
        """Every chat the account is in, with its rights already worked out.

        The rights come back on the dialog objects, so reading them here costs
        nothing. Asking per chat instead cost two round trips each — a
        ``get_entity`` and a one-message read — which on an account in 735
        groups is around fifteen hundred calls and a quarter of an hour, for
        answers Telegram had already sent.
        """
        await self._ready()
        discovered: list[DiscoveredChat] = []
        async for dialog in self._client.iter_dialogs():
            entity = dialog.entity
            discovered.append(
                DiscoveredChat(
                    ref=ChatRef(
                        _peer_kind(entity),
                        int(utils.get_peer_id(entity)),
                        access_hash=getattr(entity, "access_hash", None),
                    ),
                    title=utils.get_display_name(entity) or str(entity.id),
                    chat_kind=_chat_kind(entity),
                    username=getattr(entity, "username", None),
                    is_public=bool(getattr(entity, "username", None)),
                    has_protected_content=bool(getattr(entity, "noforwards", False)),
                    posting=posting_verdict(entity),
                    reading=reading_verdict(entity),
                )
            )
        return discovered

    async def _entity(self, ref: ChatRef) -> Any:
        await self._ready()
        if ref.access_hash is not None and ref.peer_type is PeerKind.channel:
            real_id, _ = utils.resolve_id(ref.peer_id)
            return types.InputPeerChannel(channel_id=real_id, access_hash=ref.access_hash)
        return await self._client.get_input_entity(ref.peer_id)

    async def check_source_access(self, ref: ChatRef) -> AccessReport:
        await self._ready()
        try:
            entity = await self._entity(ref)
            # One message is enough to prove the account can read the chat.
            await self._client.get_messages(entity, limit=1)
        except Exception as exc:
            return AccessReport.denied(classify_error(exc).code)
        return AccessReport.ok()

    async def custom_emoji_ids(self, emoticon: str) -> list[str]:
        await self._ready()
        from telethon.tl.functions.messages import SearchCustomEmojiRequest

        result = await self._client(SearchCustomEmojiRequest(emoticon=emoticon, hash=0))
        # EmojiListNotModified carries no ids; with hash=0 it should not occur,
        # but "should not" is not a parser.
        ids = getattr(result, "document_id", None) or []
        return [str(document_id) for document_id in ids]

    async def preview_link(self, kind: str, key: str) -> LinkPreview:
        """What Telegram shows on the "Join?" screen, and not one field more.

        A public username resolves to the chat itself. An invite hash goes
        through ``checkChatInvite``, which is the call Telegram's own clients
        make to draw that screen: it returns a title and a member count for a
        link you hold, and refuses everything else. Nothing joins, and no
        message inside is read.
        """
        await self._ready()
        from telethon.tl.functions.messages import CheckChatInviteRequest

        try:
            if kind == "invite":
                invite = await self._client(CheckChatInviteRequest(hash=key))
                chat = getattr(invite, "chat", None)
                if chat is not None:
                    # Already a member: Telegram answers with the chat itself.
                    return LinkPreview(
                        title=utils.get_display_name(chat) or None,
                        member_count=getattr(chat, "participants_count", None),
                        chat_kind=_chat_kind(chat),
                    )
                return LinkPreview(
                    title=getattr(invite, "title", None),
                    member_count=getattr(invite, "participants_count", None),
                    chat_kind="channel" if getattr(invite, "broadcast", False) else "supergroup",
                )

            entity = await self._client.get_entity(key)
            count = None
            if not isinstance(entity, types.User):
                details = await self.chat_details(
                    ChatRef(
                        _peer_kind(entity),
                        int(utils.get_peer_id(entity)),
                        access_hash=getattr(entity, "access_hash", None),
                    )
                )
                count = details.member_count
            return LinkPreview(
                title=utils.get_display_name(entity) or None,
                member_count=count,
                chat_kind="user" if isinstance(entity, types.User) else _chat_kind(entity),
            )
        except Exception as exc:
            return LinkPreview(reason_code=classify_error(exc).code)

    async def chat_details(self, ref: ChatRef) -> ChatDetails:
        """What the chat says about itself, from Telegram's ``full`` view.

        ``participants_count`` is a number Telegram publishes on the chat, not
        a roster — nothing here enumerates anyone, and nothing may. It is
        included because "12,000 members" is often the detail that identifies
        a private group again when its title alone does not.
        """
        await self._ready()
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest

        entity = await self._entity(ref)
        if ref.peer_type is PeerKind.channel:
            result = await self._client(GetFullChannelRequest(channel=entity))
        else:
            real_id, _ = utils.resolve_id(ref.peer_id)
            result = await self._client(GetFullChatRequest(chat_id=real_id))

        full = getattr(result, "full_chat", None)
        about = (getattr(full, "about", None) or "").strip() or None
        count = getattr(full, "participants_count", None)
        return ChatDetails(description=about, member_count=int(count) if count else None)

    async def installed_custom_emoji(self) -> dict[str, str]:
        """Walk the account's own emoji packs, keyed by fallback emoji.

        Far richer than searching one emoticon at a time: every document in a
        pack carries an ``alt`` — the plain emoji it is drawn in place of — so
        one pass maps most of a panel at once. Searching returned almost
        nothing for this account, which is the difference between "what
        Telegram suggests" and "what you actually own".

        Earlier packs win: Telegram returns them most-recently-used first, so
        the first hit for an emoji is the one the account reaches for.
        """
        await self._ready()
        from telethon.tl.functions.messages import GetEmojiStickersRequest, GetStickerSetRequest
        from telethon.tl.types import InputStickerSetID

        found: dict[str, str] = {}
        packs = await self._client(GetEmojiStickersRequest(hash=0))
        for pack in getattr(packs, "sets", []) or []:
            full = await self._client(
                GetStickerSetRequest(
                    stickerset=InputStickerSetID(id=pack.id, access_hash=pack.access_hash),
                    hash=0,
                )
            )
            for document in getattr(full, "documents", []) or []:
                for attribute in getattr(document, "attributes", []) or []:
                    alt = getattr(attribute, "alt", None)
                    if alt:
                        found.setdefault(alt, str(document.id))
                        break
        return found

    async def check_destination_access(self, ref: ChatRef) -> AccessReport:
        await self._ready()
        try:
            entity = await self._client.get_entity(await self._entity(ref))
        except Exception as exc:
            return AccessReport.denied(classify_error(exc).code)
        return posting_verdict(entity)

    # --- intake ---------------------------------------------------------- #
    async def receive_new_messages(self) -> AsyncIterator[InboundMessage]:
        """Persistent update connection with ``catch_up`` so a restart resumes."""

        await self._ready()

        @self._client.on(events.NewMessage(incoming=True))
        async def _on_message(event: Any) -> None:  # pragma: no cover - live path
            await self._queue.put(self._to_inbound(event.message, event.chat_id))

        @self._client.on(events.Album())
        async def _on_album(event: Any) -> None:  # pragma: no cover - live path
            messages = list(event.messages)
            if not messages:
                return
            inbound = self._to_inbound(messages[0], event.chat_id)
            inbound.message_ids = sorted(int(m.id) for m in messages)
            inbound.grouped_id = int(getattr(messages[0], "grouped_id", 0)) or None
            await self._queue.put(inbound)

        await self._client.catch_up()
        while True:
            yield await self._queue.get()

    def _to_inbound(self, message: Any, chat_id: int) -> InboundMessage:
        chat = getattr(message, "chat", None)
        return InboundMessage(
            source=ChatRef(
                _peer_kind(chat) if chat is not None else PeerKind.channel,
                int(chat_id),
                access_hash=getattr(chat, "access_hash", None),
            ),
            message_ids=[int(message.id)],
            media_type=detect_media_type(message),
            text=getattr(message, "message", "") or "",
            has_protected_content=bool(getattr(chat, "noforwards", False)),
            grouped_id=getattr(message, "grouped_id", None),
            entity_urls=_entity_urls(message),
        )

    # --- delivery -------------------------------------------------------- #
    async def forward_message(
        self,
        source: ChatRef,
        message_ids: list[int],
        destination: ChatRef,
        *,
        random_id: int | None = None,
    ) -> DeliveryReceipt:
        await self._ready()
        await self._ready()
        await self._ready()
        """``random_id`` is passed through so Telegram deduplicates a retry
        server-side after an ambiguous timeout."""
        from_peer = await self._entity(source)
        to_peer = await self._entity(destination)
        ordered = sorted(message_ids)

        result = await self._client(
            functions.messages.ForwardMessagesRequest(
                from_peer=from_peer,
                id=ordered,
                to_peer=to_peer,
                random_id=(
                    [random_id + index for index in range(len(ordered))]
                    if random_id is not None
                    else [utils.generate_random_long() for _ in ordered]
                ),
            )
        )
        delivered_id: int | None = None
        for update in getattr(result, "updates", []) or []:
            candidate = getattr(update, "id", None) or getattr(
                getattr(update, "message", None), "id", None
            )
            if candidate:
                delivered_id = int(candidate)
                break
        return DeliveryReceipt(destination_message_id=delivered_id)

    async def send_supported_content(
        self,
        source: ChatRef,
        message_ids: list[int],
        destination: ChatRef,
        *,
        preserve_caption: bool = True,
    ) -> DeliveryReceipt:
        await self._ready()
        if len(message_ids) > 1:
            raise AdapterError(reasons.UNCOPYABLE_MESSAGE, ErrorClass.PERMANENT_CONTENT)
        from_peer = await self._entity(source)
        messages = await self._client.get_messages(from_peer, ids=message_ids)
        message = messages[0] if isinstance(messages, list) else messages
        if message is None:
            raise AdapterError(reasons.MESSAGE_UNAVAILABLE, ErrorClass.PERMANENT_CONTENT)

        sent = await self._client.send_message(
            await self._entity(destination),
            message=message.message if preserve_caption else "",
            file=message.media,
        )
        return DeliveryReceipt(destination_message_id=int(sent.id))

    async def send_text(
        self,
        destination: ChatRef,
        text: str,
        *,
        entities: Sequence[TextEntity] = (),
        random_id: int | None = None,
    ) -> DeliveryReceipt:
        """MTProto carries a ``random_id``, so a retry after an ambiguous
        timeout is deduplicated by Telegram rather than by us guessing."""
        await self._ready()
        sent = await self._client.send_message(
            await self._entity(destination),
            message=text,
            # Always a list, never None, and parse_mode off. Telethon reads
            # `formatting_entities is None` as "parse this text as Markdown",
            # so an ad with no formatting would have had its asterisks and
            # underscores eaten as markup.
            formatting_entities=_to_mtproto_entities(entities),
            parse_mode=None,
        )
        return DeliveryReceipt(destination_message_id=int(sent.id))

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
        await self._ready()
        import io

        # Telethon infers the type from the name, so the buffer is named rather
        # than passed as anonymous bytes — otherwise the image is delivered as a
        # generic document.
        buffer = io.BytesIO(photo)
        buffer.name = filename

        sent = await self._client.send_file(
            await self._entity(destination),
            file=buffer,
            caption=caption or None,
            # See send_text: an empty list must stay a list.
            formatting_entities=_to_mtproto_entities(caption_entities),
            parse_mode=None,
        )
        return DeliveryReceipt(destination_message_id=int(sent.id))

    # --- misc ------------------------------------------------------------ #
    def classify_error(self, exc: BaseException) -> ClassifiedError:
        return classify_error(exc)

    def capabilities(self) -> Capabilities:
        return capabilities_for(self.kind)
