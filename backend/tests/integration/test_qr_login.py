"""QR sign-in, and getting out of a sign-in that failed.

The phone/code flow is unreliable when the panel *is* a Telegram chat: Telegram
cancels any login code it sees an account send, so the code is burned before it
is used and the sign-in fails with "the code was previously shared by your
account". Two things follow, and both are tested here:

* QR is the sign-in that has no code to leak, so it is the default;
* a failed attempt must be clearable, or the partial unique index refuses every
  retry with "already in progress" and there is no way out of the panel.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.adminbot import handlers, views
from app.adminbot import qr as qr_flow
from app.db.models import ConnectionKind, ConnectionStatus, TelegramConnection
from app.domain import reasons
from app.repositories import connections as connection_repo
from app.services import connections as connection_service
from tests.conftest import script_for

CHAT = 900_100_200


class Sent:
    photos: ClassVar[list[tuple[bytes, str]]] = []
    messages: ClassVar[list[str]] = []

    @classmethod
    def reset(cls) -> None:
        cls.photos = []
        cls.messages = []


class BotMessage(Message):
    async def answer(self, text: str = "", **_kw: Any) -> Any:
        Sent.messages.append(text)
        return self

    async def edit_text(self, text: str = "", **_kw: Any) -> Any:
        Sent.messages.append(text)
        return self

    async def delete(self, **_kw: Any) -> bool:
        return True


class BotCallback(CallbackQuery):
    alerts: ClassVar[list[str]] = []

    async def answer(self, text: str | None = None, **_kw: Any) -> Any:
        if text:
            BotCallback.alerts.append(text)
        return True


class FakeBot:
    """Captures what the QR flow would send, without a network."""

    def __init__(self) -> None:
        self.id = 1

    async def send_photo(self, chat_id: int, photo: Any, caption: str = "", **_kw: Any) -> Any:
        Sent.photos.append((photo.data, caption))
        return BotMessage(
            message_id=len(Sent.photos),
            date=datetime(2026, 1, 1, tzinfo=UTC),
            chat=Chat(id=chat_id, type="private"),
        )

    async def send_message(self, chat_id: int, text: str = "", **_kw: Any) -> Any:
        Sent.messages.append(text)
        return None


def a_message(text: str = "") -> BotMessage:
    message = BotMessage(
        message_id=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(id=CHAT, type="private"),
        from_user=TgUser(id=CHAT, is_bot=False, first_name="Op"),
        text=text,
    )
    # aiogram resolves `message.bot` from a context var; injecting one keeps the
    # handler on its real path instead of an early return.
    return message.as_(FakeBot())  # type: ignore[arg-type]


def a_callback(data: str) -> BotCallback:
    return BotCallback(
        id="1",
        from_user=TgUser(id=CHAT, is_bot=False, first_name="Op"),
        chat_instance="ci",
        data=data,
        message=a_message(),
    )


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CHAT, user_id=CHAT))


@pytest.fixture(autouse=True)
def _reset():
    Sent.reset()
    BotCallback.alerts = []
    yield


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_the_qr_is_a_real_png_of_the_login_url():
    png = qr_flow.render("tg://login?token=AQIDBAUGBwgJ")
    assert png.startswith(b"\x89PNG\r\n\x1a\n"), "Telegram must receive a real image"
    assert len(png) > 200


def test_the_caption_explains_where_to_scan_and_why():
    caption = qr_flow.caption()
    assert "Link Desktop Device" in caption, "the exact menu path, not 'scan it somewhere'"
    assert "no login code to cancel" in caption


def test_a_refreshed_code_says_the_previous_one_expired():
    """A second QR appearing with no explanation looks like a bug."""
    assert "expired" in qr_flow.caption(attempt=2)


def test_the_cancel_button_fits_telegram_s_callback_limit():
    markup = qr_flow.cancel_keyboard(uuid.uuid4())
    for row in markup.inline_keyboard:
        for button in row:
            assert len((button.callback_data or "").encode()) <= 64


# --------------------------------------------------------------------------- #
# The flow
# --------------------------------------------------------------------------- #
async def test_add_account_starts_a_qr_sign_in(client, actor, state, session):
    user_id = uuid.UUID(actor.id)

    await handlers.add_account(a_callback("add:user"), state=state)
    assert "QR code" in Sent.messages[-1]

    await handlers.account_label(a_message("Main"), user_id=user_id, state=state)

    connection = (
        await session.execute(select(TelegramConnection).where(TelegramConnection.label == "Main"))
    ).scalar_one()
    assert connection.kind is ConnectionKind.user
    assert connection.status is ConnectionStatus.awaiting_code
    assert Sent.photos, "a QR image must have been sent"
    assert script_for(str(connection.id)).calls_to("start_qr_login")


async def test_no_login_code_is_ever_requested_on_the_qr_path(client, actor, state):
    """The whole point: nothing secret enters the conversation, so Telegram has
    nothing to cancel.

    Asserted on the conversation *state*, not on the words — the prompts do
    mention login codes, to explain why this route avoids them.
    """
    await handlers.add_account(a_callback("add:user"), state=state)
    await handlers.account_label(a_message("Main"), user_id=uuid.UUID(actor.id), state=state)

    assert await state.get_state() is None, (
        "the QR path must not leave the conversation waiting for anything typed"
    )

    # And no prompt asks for one.
    for text in Sent.messages:
        assert "send the code" not in text.lower()
        assert "send the phone number" not in text.lower()


async def test_scanning_completes_the_sign_in(client, actor, session):
    user_id = uuid.UUID(actor.id)
    connection, _qr = await connection_service.start_qr_connection(
        session, user_id=user_id, label="Main"
    )
    await session.commit()

    status = await connection_service.await_qr_scan(session, connection=connection, timeout_s=1)
    await session.commit()

    assert status is ConnectionStatus.active
    assert connection.status is ConnectionStatus.active
    assert connection.telegram_account_id is not None


async def test_an_expired_token_is_refreshed_rather_than_restarting(client, actor, session):
    """Telegram's QR tokens expire in seconds. Making someone start over each
    time would make the flow unusable."""
    user_id = uuid.UUID(actor.id)
    connection, qr = await connection_service.start_qr_connection(
        session, user_id=user_id, label="Main"
    )
    await session.commit()

    script_for(str(connection.id)).qr_expires = True
    with pytest.raises(connection_service.QrExpired):
        await connection_service.await_qr_scan(session, connection=connection, timeout_s=1)

    refreshed = await connection_service.refresh_qr(connection.id)
    assert refreshed.url != qr.url
    assert connection.status is ConnectionStatus.awaiting_code, "still the same attempt"

    # And the same connection can still complete.
    assert (
        await connection_service.await_qr_scan(session, connection=connection, timeout_s=1)
    ) is ConnectionStatus.active


async def test_waiting_on_a_sign_in_that_is_not_running_is_refused(client, actor, session):
    connection = await connection_repo.create(
        session,
        user_id=uuid.UUID(actor.id),
        kind=ConnectionKind.user,
        label="Stale",
        status=ConnectionStatus.awaiting_code,
    )
    await session.commit()

    with pytest.raises(connection_service.ConnectionNotReady):
        await connection_service.await_qr_scan(session, connection=connection, timeout_s=1)


# --------------------------------------------------------------------------- #
# Getting unstuck
# --------------------------------------------------------------------------- #
async def test_a_failed_sign_in_blocks_every_retry_until_it_is_cleared(client, actor, session):
    """The reported problem: a sign-in that failed left a row in awaiting_code,
    and the partial unique index then refused every new attempt."""
    user_id = uuid.UUID(actor.id)
    connection, _ = await connection_service.start_qr_connection(
        session, user_id=user_id, label="First"
    )
    await session.commit()

    with pytest.raises(connection_service.DuplicateConnectionAttempt):
        await connection_service.start_qr_connection(session, user_id=user_id, label="Second")

    await connection_service.abandon(session, connection=connection)
    await session.commit()

    # Now it works.
    second, _ = await connection_service.start_qr_connection(
        session, user_id=user_id, label="Second"
    )
    assert second.label == "Second"


async def test_the_panel_offers_a_way_out_of_a_stuck_sign_in(client, actor, session):
    """A connection that never finished has no useful actions except clearing
    it, so that is the only one offered."""
    connection = await connection_repo.create(
        session,
        user_id=uuid.UUID(actor.id),
        kind=ConnectionKind.user,
        label="Master",
        status=ConnectionStatus.awaiting_code,
    )
    await session.commit()

    screen = views.connection_detail(connection=connection, chat_count=0)
    buttons = [b.callback_data for row in screen.keyboard.inline_keyboard for b in row]

    assert f"conn:{connection.id}:abandon" in buttons
    assert f"conn:{connection.id}:sync" not in buttons, "syncing a half-signed-in account is noise"
    assert "never finished" in screen.text


async def test_cancelling_removes_the_connection(client, actor, session):
    connection = await connection_repo.create(
        session,
        user_id=uuid.UUID(actor.id),
        kind=ConnectionKind.user,
        label="Master",
        status=ConnectionStatus.awaiting_code,
    )
    await session.commit()

    await handlers.connection_actions(
        a_callback(f"conn:{connection.id}:abandon"), user_id=uuid.UUID(actor.id)
    )

    session.expire_all()
    rows = (await session.execute(select(TelegramConnection))).scalars().all()
    assert not rows
    assert any("cleared" in alert.lower() for alert in BotCallback.alerts)


# --------------------------------------------------------------------------- #
# The messages a failed sign-in produces
# --------------------------------------------------------------------------- #
def test_a_burned_login_code_explains_itself_instead_of_saying_eligibility():
    """The reported bug: a rejected login code rendered "Eligibility has not
    been checked yet", which is the fallback text for an unclassified code and
    is nonsense in a sign-in."""
    text = reasons.describe(reasons.LOGIN_CODE_INVALID)

    assert "Eligibility" not in text
    assert "cancels any code" in text
    assert "QR" in text, "it must point at the thing that actually works"


@pytest.mark.parametrize(
    "exception_name,expected",
    [
        ("PhoneCodeInvalidError", reasons.LOGIN_CODE_INVALID),
        ("PhoneCodeExpiredError", reasons.LOGIN_CODE_EXPIRED),
        ("PhoneNumberInvalidError", reasons.PHONE_NUMBER_INVALID),
        ("PhoneNumberBannedError", reasons.PHONE_NUMBER_BANNED),
        ("PasswordHashInvalidError", reasons.TWO_FACTOR_PASSWORD_INVALID),
    ],
)
def test_sign_in_failures_are_classified(exception_name, expected):
    from app.adapters.errors import classify_error

    exc = type(exception_name, (Exception,), {})()
    assert classify_error(exc).code == expected


def test_sign_in_failures_are_never_retried():
    """Asking Telegram again with the same burned code cannot start working."""
    from app.adapters.errors import classify_error

    for name in ("PhoneCodeInvalidError", "PhoneCodeExpiredError", "PasswordHashInvalidError"):
        exc = type(name, (Exception,), {})()
        assert not classify_error(exc).retryable


def test_the_phone_route_is_still_offered_but_labelled():
    """It genuinely works when the account being connected is not the one
    driving the bot, so it is kept — with the caveat attached."""
    screen = views.connections_list(connections=[])
    buttons = [b.callback_data for row in screen.keyboard.inline_keyboard for b in row]
    assert "add:user" in buttons
    assert "add:phone" in buttons
