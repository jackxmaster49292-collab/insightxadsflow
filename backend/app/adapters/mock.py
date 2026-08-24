"""Scriptable in-memory adapter. The default provider everywhere except
``TELEGRAM_PROVIDER=live``.

Every automated test runs against this — no test ever contacts Telegram.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

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
    TextEntity,
)
from app.adapters.capabilities import capabilities_for
from app.adapters.errors import ClassifiedError, classify_error


@dataclass
class MockCall:
    method: str
    args: tuple[object, ...]
    kwargs: dict[str, object]


@dataclass
class MockScript:
    """Shared scripting surface so a test can steer adapter behaviour."""

    chats: list[DiscoveredChat] = field(default_factory=list)
    source_allowed: dict[tuple[str, int], AccessReport] = field(default_factory=dict)
    destination_allowed: dict[tuple[str, int], AccessReport] = field(default_factory=dict)
    inbound: list[InboundMessage] = field(default_factory=list)
    #: Errors keyed by destination. Popped left on each delivery attempt, so a
    #: test can script "fail once, then succeed".
    delivery_errors: dict[tuple[str, int], list[BaseException]] = field(default_factory=dict)
    calls: list[MockCall] = field(default_factory=list)
    healthy: bool = True
    account_id: int = 777_000_111
    username: str = "mock_account"
    premium: bool = False
    next_destination_message_id: int = 1000
    connect_error: BaseException | None = None
    #: Custom-emoji search results, keyed by emoticon.
    custom_emoji: dict[str, list[str]] = field(default_factory=dict)
    #: Emoji the account "owns", keyed by the plain emoji they stand in for.
    installed_emoji: dict[str, str] = field(default_factory=dict)
    #: Errors keyed by method name, raised on the next call to that method.
    #: `delivery_errors` is keyed by destination, which discovery and health
    #: checks do not have — so a test could not script a failure in either.
    method_errors: dict[str, BaseException] = field(default_factory=dict)

    def record(self, method: str, *args: object, **kwargs: object) -> None:
        self.calls.append(MockCall(method, args, kwargs))

    def calls_to(self, method: str) -> list[MockCall]:
        return [c for c in self.calls if c.method == method]

    def fail_method(self, method: str, error: BaseException) -> None:
        self.method_errors[method] = error

    def fail_delivery(self, ref: ChatRef, *errors: BaseException) -> None:
        self.delivery_errors.setdefault(ref.key, []).extend(errors)

    def allow_source(self, ref: ChatRef, allowed: bool = True, reason: str = "ok") -> None:
        self.source_allowed[ref.key] = AccessReport(allowed, reason if not allowed else "ok")

    def allow_destination(self, ref: ChatRef, allowed: bool = True, reason: str = "ok") -> None:
        self.destination_allowed[ref.key] = AccessReport(allowed, reason if not allowed else "ok")


class MockAdapter:
    """Implements :class:`~app.adapters.base.TelegramAdapter`."""

    def __init__(self, script: MockScript | None = None, *, kind: str = "bot") -> None:
        self.script = script or MockScript()
        self.kind = kind
        self.connected = False

    # --- lifecycle ------------------------------------------------------- #
    async def connect(self) -> ConnectionState:
        self.script.record("connect")
        if self.script.connect_error is not None:
            raise self.script.connect_error
        self.connected = True
        return ConnectionState(
            status="active",
            account_id=self.script.account_id,
            username=self.script.username,
            session_string="mock-session-string" if self.kind == "user" else None,
            premium=self.script.premium,
        )

    async def disconnect(self, *, revoke: bool = False) -> None:
        self.script.record("disconnect", revoke=revoke)
        self.connected = False

    async def health_check(self) -> HealthReport:
        self.script.record("health_check")
        self._maybe_fail("health_check")
        return HealthReport(
            healthy=self.script.healthy,
            reason_code="ok" if self.script.healthy else "unauthorized",
            account_id=self.script.account_id,
            username=self.script.username,
            premium=self.script.premium,
        )

    # --- discovery ------------------------------------------------------- #
    async def list_available_chats(self) -> list[DiscoveredChat]:
        self.script.record("list_available_chats")
        self._maybe_fail("list_available_chats")
        return list(self.script.chats)

    async def check_source_access(self, ref: ChatRef) -> AccessReport:
        self.script.record("check_source_access", ref)
        return self.script.source_allowed.get(ref.key, AccessReport.ok())

    async def custom_emoji_ids(self, emoticon: str) -> list[str]:
        self.script.record("custom_emoji_ids", emoticon)
        self._maybe_fail("custom_emoji_ids")
        return list(self.script.custom_emoji.get(emoticon, []))

    async def installed_custom_emoji(self) -> dict[str, str]:
        self.script.record("installed_custom_emoji")
        self._maybe_fail("installed_custom_emoji")
        return dict(self.script.installed_emoji)

    async def check_destination_access(self, ref: ChatRef) -> AccessReport:
        self.script.record("check_destination_access", ref)
        return self.script.destination_allowed.get(ref.key, AccessReport.ok())

    # --- intake ---------------------------------------------------------- #
    async def receive_new_messages(self) -> AsyncIterator[InboundMessage]:
        self.script.record("receive_new_messages")
        for message in list(self.script.inbound):
            yield message
            await asyncio.sleep(0)

    def _maybe_fail(self, method: str) -> None:
        error = self.script.method_errors.get(method)
        if error is not None:
            raise error

    # --- delivery -------------------------------------------------------- #
    def _maybe_raise(self, destination: ChatRef) -> None:
        queued = self.script.delivery_errors.get(destination.key)
        if queued:
            raise queued.pop(0)

    async def forward_message(
        self,
        source: ChatRef,
        message_ids: list[int],
        destination: ChatRef,
        *,
        random_id: int | None = None,
    ) -> DeliveryReceipt:
        self.script.record("forward_message", source, message_ids, destination, random_id=random_id)
        self._maybe_raise(destination)
        self.script.next_destination_message_id += 1
        return DeliveryReceipt(destination_message_id=self.script.next_destination_message_id)

    async def send_supported_content(
        self,
        source: ChatRef,
        message_ids: list[int],
        destination: ChatRef,
        *,
        preserve_caption: bool = True,
    ) -> DeliveryReceipt:
        self.script.record(
            "send_supported_content",
            source,
            message_ids,
            destination,
            preserve_caption=preserve_caption,
        )
        self._maybe_raise(destination)
        self.script.next_destination_message_id += 1
        return DeliveryReceipt(destination_message_id=self.script.next_destination_message_id)

    async def send_text(
        self,
        destination: ChatRef,
        text: str,
        *,
        entities: Sequence[TextEntity] = (),
        random_id: int | None = None,
    ) -> DeliveryReceipt:
        self.script.record(
            "send_text", destination, text, entities=list(entities), random_id=random_id
        )
        self._maybe_raise(destination)
        self.script.next_destination_message_id += 1
        return DeliveryReceipt(destination_message_id=self.script.next_destination_message_id)

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
        # Records the byte count, not the bytes: a failing test should print a
        # readable diff, not a megabyte of binary.
        self.script.record(
            "send_photo",
            destination,
            len(photo),
            caption=caption,
            caption_entities=list(caption_entities),
            filename=filename,
            random_id=random_id,
        )
        self._maybe_raise(destination)
        self.script.next_destination_message_id += 1
        return DeliveryReceipt(destination_message_id=self.script.next_destination_message_id)

    # --- misc ------------------------------------------------------------ #
    def classify_error(self, exc: BaseException) -> ClassifiedError:
        return classify_error(exc)

    def capabilities(self) -> Capabilities:
        return capabilities_for(self.kind)


__all__ = ["MockAdapter", "MockScript", "MockCall", "AmbiguousDeliveryError"]
