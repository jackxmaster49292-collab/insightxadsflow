"""MTProto adapter (Telethon 1.44).

Session material is held as a ``StringSession`` so nothing is written to disk;
the caller persists it envelope-encrypted.

``access_hash`` is account-specific, so it is returned with each discovered chat
and stored per connection. Without it, a private channel cannot be addressed at
all after a process restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import structlog
from telethon import TelegramClient, events, functions, types, utils
from telethon.sessions import StringSession

from app.adapters.base import (
    AccessReport,
    Capabilities,
    ChatRef,
    ConnectionState,
    DeliveryReceipt,
    DiscoveredChat,
    HealthReport,
    InboundMessage,
    MediaType,
    PeerKind,
    QrLogin,
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

    # --- lifecycle ------------------------------------------------------- #
    async def connect(self) -> ConnectionState:
        await self._client.connect()
        if not await self._client.is_user_authorized():
            return ConnectionState(status="awaiting_code", session_string=self.session_string)
        me = await self._client.get_me()
        return ConnectionState(
            status="active",
            account_id=int(me.id),
            username=getattr(me, "username", None),
            session_string=self.session_string,
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
        await self._client.connect()
        sent = await self._client.send_code_request(phone)
        return str(sent.phone_code_hash)

    async def start_qr_login(self) -> QrLogin:
        """Begin a QR sign-in.

        Telegram's own device-linking flow, and the only sign-in that works from
        inside a chat: nothing secret is ever typed, so there is no code for
        Telegram to cancel. The customer scans from an app they are already
        signed in to, which is a stronger proof than a code they could be talked
        into forwarding to someone else.
        """
        await self._client.connect()
        login = await self._client.qr_login()
        return QrLogin(url=str(login.url), handle=login)

    async def wait_for_qr(self, login: QrLogin, *, timeout_s: float) -> ConnectionState:
        """Block until the QR is scanned, or the wait runs out.

        A timeout is not a failure — the token simply expired and can be
        refreshed. Only ``TwoFactorRequired`` means the scan succeeded and a
        password is still needed.
        """
        from telethon.errors import SessionPasswordNeededError

        try:
            await login.handle.wait(timeout=timeout_s)
        except SessionPasswordNeededError as exc:
            raise TwoFactorRequired from exc
        me = await self._client.get_me()
        return ConnectionState(
            status="active",
            account_id=int(me.id),
            username=getattr(me, "username", None),
            session_string=self.session_string,
        )

    async def refresh_qr(self, login: QrLogin) -> QrLogin:
        """Telegram's QR tokens expire in well under a minute; this issues a new
        one on the same client, so the sign-in continues rather than restarting."""
        await login.handle.recreate()
        return QrLogin(url=str(login.handle.url), handle=login.handle)

    async def complete_login(self, phone: str, code: str, phone_code_hash: str) -> ConnectionState:
        from telethon.errors import SessionPasswordNeededError

        await self._client.connect()
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
        )

    async def complete_2fa(self, password: str) -> ConnectionState:
        """The password is used once, here, and never persisted in any form."""
        await self._client.connect()
        await self._client.sign_in(password=password)
        me = await self._client.get_me()
        return ConnectionState(
            status="active",
            account_id=int(me.id),
            username=getattr(me, "username", None),
            session_string=self.session_string,
        )

    async def disconnect(self, *, revoke: bool = False) -> None:
        if revoke and self._client.is_connected():
            # Invalidates the session on Telegram's side, not just locally.
            await self._client(functions.auth.LogOutRequest())
        await self._client.disconnect()

    async def health_check(self) -> HealthReport:
        try:
            if not self._client.is_connected():
                await self._client.connect()
            if not await self._client.is_user_authorized():
                return HealthReport(healthy=False, reason_code="session_revoked")
            me = await self._client.get_me()
        except Exception as exc:
            return HealthReport(healthy=False, reason_code=classify_error(exc).code)
        return HealthReport(
            healthy=True, account_id=int(me.id), username=getattr(me, "username", None)
        )

    # --- discovery ------------------------------------------------------- #
    async def list_available_chats(self) -> list[DiscoveredChat]:
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
                )
            )
        return discovered

    async def _entity(self, ref: ChatRef) -> Any:
        if ref.access_hash is not None and ref.peer_type is PeerKind.channel:
            real_id, _ = utils.resolve_id(ref.peer_id)
            return types.InputPeerChannel(channel_id=real_id, access_hash=ref.access_hash)
        return await self._client.get_input_entity(ref.peer_id)

    async def check_source_access(self, ref: ChatRef) -> AccessReport:
        try:
            entity = await self._entity(ref)
            # One message is enough to prove the account can read the chat.
            await self._client.get_messages(entity, limit=1)
        except Exception as exc:
            return AccessReport.denied(classify_error(exc).code)
        return AccessReport.ok()

    async def check_destination_access(self, ref: ChatRef) -> AccessReport:
        try:
            entity = await self._client.get_entity(await self._entity(ref))
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
        except Exception as exc:
            return AccessReport.denied(classify_error(exc).code)
        return AccessReport.ok()

    # --- intake ---------------------------------------------------------- #
    async def receive_new_messages(self) -> AsyncIterator[InboundMessage]:
        """Persistent update connection with ``catch_up`` so a restart resumes."""

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
        random_id: int | None = None,
    ) -> DeliveryReceipt:
        """MTProto carries a ``random_id``, so a retry after an ambiguous
        timeout is deduplicated by Telegram rather than by us guessing."""
        sent = await self._client.send_message(
            await self._entity(destination),
            message=text,
        )
        return DeliveryReceipt(destination_message_id=int(sent.id))

    async def send_photo(
        self,
        destination: ChatRef,
        photo: bytes,
        *,
        caption: str = "",
        filename: str = "image.jpg",
        random_id: int | None = None,
    ) -> DeliveryReceipt:
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
        )
        return DeliveryReceipt(destination_message_id=int(sent.id))

    # --- misc ------------------------------------------------------------ #
    def classify_error(self, exc: BaseException) -> ClassifiedError:
        return classify_error(exc)

    def capabilities(self) -> Capabilities:
        return capabilities_for(self.kind)
