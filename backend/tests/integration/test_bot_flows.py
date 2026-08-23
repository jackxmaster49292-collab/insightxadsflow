"""The bot flows, driven through the real handlers.

The bot is the only admin surface now, so "does the panel work" is no longer a
question about HTTP routes. These tests push real ``Message`` and
``CallbackQuery`` objects through the actual handler functions, with the real
database underneath and the mock Telegram adapter at the edge.

Two classes of bug this catches that unit-testing the screens would not:

* a MarkdownV2 escaping mistake, which Telegram answers with a 400 and which
  therefore breaks a screen completely rather than cosmetically;
* a flow that leaves FSM state pointing at something that no longer exists.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.adminbot import handlers, views
from app.db.models import (
    Broadcast,
    BroadcastStatus,
    ConnectionStatus,
    ControlTask,
    ControlTaskKind,
    JobStatus,
    RuleStatus,
    TelegramConnection,
)
from app.domain import reasons
from tests.conftest import connect_bot, discovered, fake_broadcast, sync_with_chats

ADMIN_CHAT = 900_100_200


# --------------------------------------------------------------------------- #
# MarkdownV2 validation
# --------------------------------------------------------------------------- #
#: Characters Telegram requires to be escaped in MarkdownV2 unless they open or
#: close formatting. Source: Bot API "Formatting options".
MDV2_MUST_ESCAPE = set(r"[]()~>#+-=|{}.!")

#: The one piece of bracket syntax the panel emits: an inline custom emoji.
CUSTOM_EMOJI_TOKEN = re.compile(r"!\[[^\]]+\]\(tg://emoji\?id=\d+\)")


def assert_valid_markdown_v2(text: str) -> None:
    """Reject the two escaping mistakes Telegram rejects.

    **Unescaped literals.** A literal ``.``, ``-``, ``(`` and friends must carry
    a backslash.

    **Unbalanced formatting.** ``_`` and ``*`` are excluded from the set above
    because they legitimately open italics and bold — but an odd number of them
    means one was never closed, and Telegram answers
    ``Can't find end of Italic entity`` and drops the entire message. That is
    how the Accounts screen broke on a status value of ``awaiting_code``: the
    underscore inside it opened italics that never closed.
    """
    index = 0
    inside_code = False
    opened = {"_": 0, "*": 0}

    while index < len(text):
        # A custom-emoji token — ``![🔥](tg://emoji?id=N)`` — is markup, so its
        # brackets and parens are legitimately unescaped. Skipped whole.
        token = CUSTOM_EMOJI_TOKEN.match(text, index)
        if token and not inside_code:
            index = token.end()
            continue
        char = text[index]
        if char == "\\":
            index += 2  # escaped: literal, and not a delimiter
            continue
        if char == "`":
            inside_code = not inside_code
            index += 1
            continue
        if not inside_code:
            if char in MDV2_MUST_ESCAPE:
                around = text[max(0, index - 40) : index + 40]
                raise AssertionError(
                    f"unescaped {char!r} at byte {len(text[:index].encode())} would "
                    f"make Telegram reject this message with a 400:\n...{around}..."
                )
            if char in opened:
                opened[char] += 1
        index += 1

    assert not inside_code, "an unclosed code span would make Telegram reject this message"

    for delimiter, count in opened.items():
        if count % 2:
            name = "Italic" if delimiter == "_" else "Bold"
            raise AssertionError(
                f"{count} unescaped {delimiter!r} — an odd number, so one entity is "
                f'never closed. Telegram answers "Can\'t find end of {name} entity" '
                f"and drops the whole message.\n\n{text}"
            )


def assert_keyboard_is_sendable(markup: InlineKeyboardMarkup | None) -> None:
    if markup is None:
        return
    for row in markup.inline_keyboard:
        for button in row:
            data = button.callback_data
            if data is not None:
                size = len(data.encode())
                assert 1 <= size <= 64, f"callback_data {data!r} is {size} bytes"


# --------------------------------------------------------------------------- #
# Recording aiogram objects
# --------------------------------------------------------------------------- #
class Sent:
    """Everything the bot tried to send, checked as it goes."""

    messages: ClassVar[list[tuple[str, InlineKeyboardMarkup | None]]] = []
    alerts: ClassVar[list[str]] = []
    deleted: ClassVar[int] = 0

    @classmethod
    def reset(cls) -> None:
        cls.messages = []
        cls.alerts = []
        cls.deleted = 0

    @classmethod
    def record(cls, text: str, markup: InlineKeyboardMarkup | None, parse_mode: str | None) -> None:
        if parse_mode == "MarkdownV2":
            assert_valid_markdown_v2(text)
        assert_keyboard_is_sendable(markup)
        cls.messages.append((text, markup))

    @classmethod
    def last(cls) -> str:
        assert cls.messages, "the bot sent nothing"
        return cls.messages[-1][0]

    @classmethod
    def buttons(cls) -> list[str]:
        assert cls.messages, "the bot sent nothing"
        markup = cls.messages[-1][1]
        if markup is None:
            return []
        return [b.callback_data or "" for row in markup.inline_keyboard for b in row]

    @classmethod
    def text_contains(cls, needle: str) -> bool:
        return any(needle in text for text, _ in cls.messages)


class BotMessage(Message):
    """A real Message whose sends are captured instead of hitting Telegram."""

    async def answer(
        self,
        text: str = "",
        reply_markup: Any = None,
        parse_mode: str | None = None,
        **_kwargs: Any,
    ) -> Any:
        Sent.record(text, reply_markup, parse_mode)
        return self

    async def edit_text(
        self,
        text: str = "",
        reply_markup: Any = None,
        parse_mode: str | None = None,
        **_kwargs: Any,
    ) -> Any:
        Sent.record(text, reply_markup, parse_mode)
        return self

    async def delete(self, **_kwargs: Any) -> bool:
        Sent.deleted += 1
        return True


class BotCallback(CallbackQuery):
    async def answer(self, text: str | None = None, **_kwargs: Any) -> Any:
        if text:
            Sent.alerts.append(text)
        return True


def a_message(text: str = "") -> BotMessage:
    return BotMessage(
        message_id=1,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(id=ADMIN_CHAT, type="private"),
        from_user=TgUser(id=ADMIN_CHAT, is_bot=False, first_name="Op"),
        text=text,
    )


def a_callback(data: str) -> BotCallback:
    return BotCallback(
        id="1",
        from_user=TgUser(id=ADMIN_CHAT, is_bot=False, first_name="Op"),
        chat_instance="ci",
        data=data,
        message=a_message(),
    )


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_CHAT, user_id=ADMIN_CHAT),
    )


@pytest.fixture(autouse=True)
def _reset_sent():
    Sent.reset()
    yield


# --------------------------------------------------------------------------- #
# Home and navigation
# --------------------------------------------------------------------------- #
async def test_start_renders_the_home_screen(client, actor, state):
    await handlers.start(a_message("/start"), user_id=uuid.UUID(actor.id), state=state)

    assert "InsightAdFlow" in Sent.last()
    assert "nav:ads:0" in Sent.buttons()
    assert "nav:autoreply" in Sent.buttons()


async def test_every_navigation_screen_renders(client, actor, state):
    """A MarkdownV2 mistake on any of these is a screen that simply never
    appears, so all of them are visited."""
    user_id = uuid.UUID(actor.id)
    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor, connection_id, [discovered(-1002000, "Group (one)", chat_kind="supergroup")]
    )

    for target in (
        "nav:home",
        "nav:ads:0",
        "nav:autoreply",
        "nav:rules:0",
        "nav:conns",
        "nav:chats:0",
        "nav:activity",
    ):
        Sent.reset()
        handler = {
            "nav:home": handlers.nav_home,
            "nav:ads:0": handlers.nav_ads,
            "nav:autoreply": handlers.nav_autoreply,
            "nav:rules:0": handlers.nav_rules,
            "nav:conns": handlers.nav_connections,
            "nav:chats:0": handlers.nav_chats,
            "nav:activity": handlers.nav_activity,
        }[target]
        query = a_callback(target)
        if handler in (handlers.nav_home,):
            await handler(query, user_id=user_id, state=state)
        else:
            await handler(query, user_id=user_id)
        assert Sent.messages, f"{target} rendered nothing"


async def test_a_group_title_full_of_markdown_does_not_break_the_screen(client, actor, state):
    """Chat titles are attacker-influenced. An unescaped one makes Telegram
    reject the whole message, so the screen disappears entirely."""
    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor,
        connection_id,
        [discovered(-1002000, "*Deals* [x](y) _now_ #1! (50%)", chat_kind="supergroup")],
    )

    await handlers.nav_chats(a_callback("nav:chats:0"), user_id=uuid.UUID(actor.id))
    assert Sent.messages


# --------------------------------------------------------------------------- #
# Connecting an account by phone number
# --------------------------------------------------------------------------- #
async def test_the_phone_login_flow_reaches_a_connected_account(client, actor, state, session):
    user_id = uuid.UUID(actor.id)

    await handlers.add_account(a_callback("add:user"), state=state)
    await handlers.account_label(a_message("Main account"), state=state)
    assert "country code" in Sent.last()

    await handlers.account_phone(a_message("+919876543210"), user_id=user_id, state=state)
    assert "login code" in Sent.last()

    await handlers.account_code(a_message("1 2 3 4 5"), user_id=user_id, state=state)
    assert Sent.text_contains("Account connected")

    connection = (
        await session.execute(
            select(TelegramConnection).where(TelegramConnection.label == "Main account")
        )
    ).scalar_one()
    assert connection.status is ConnectionStatus.active
    assert connection.phone_hash is not None
    assert await state.get_state() is None, "the flow must not leave state behind"


async def test_the_phone_number_is_never_stored_in_the_clear(client, actor, state, session):
    """Only a hash is kept, and the message carrying it is deleted."""
    user_id = uuid.UUID(actor.id)
    await handlers.add_account(a_callback("add:user"), state=state)
    await handlers.account_label(a_message("Main"), state=state)

    deleted_before = Sent.deleted
    # Deliberately not the number the prompt uses as its example, so an echo is
    # distinguishable from the example itself.
    await handlers.account_phone(a_message("+447700900123"), user_id=user_id, state=state)
    assert Sent.deleted == deleted_before + 1, "the message with the phone must be deleted"

    connection = (
        await session.execute(select(TelegramConnection).where(TelegramConnection.label == "Main"))
    ).scalar_one()
    assert connection.phone_hash != "+447700900123"
    assert len(connection.phone_hash) == 64

    # And the number never appears in anything the bot said back.
    for text, _ in Sent.messages:
        assert "447700900123" not in text


async def test_the_login_code_message_is_deleted(client, actor, state):
    user_id = uuid.UUID(actor.id)
    await handlers.add_account(a_callback("add:user"), state=state)
    await handlers.account_label(a_message("Main"), state=state)
    await handlers.account_phone(a_message("+919876543210"), user_id=user_id, state=state)

    deleted_before = Sent.deleted
    await handlers.account_code(a_message("1 2 3 4 5"), user_id=user_id, state=state)
    assert Sent.deleted == deleted_before + 1


async def test_the_warning_comes_before_a_code_is_ever_requested(client, actor, state):
    """Telegram burns a code the moment it sees the account send it, so being
    told afterwards is useless — the code is already gone."""
    await handlers.add_account(a_callback("add:user"), state=state)

    warning = Sent.last()
    assert "previously shared" in warning
    assert "not* the one you are messaging me from" in warning


async def test_a_malformed_phone_number_is_rejected_without_starting_a_login(
    client, actor, state, session
):
    user_id = uuid.UUID(actor.id)
    await handlers.add_account(a_callback("add:user"), state=state)
    await handlers.account_label(a_message("Main"), state=state)
    await handlers.account_phone(a_message("9876543210"), user_id=user_id, state=state)

    assert "country code" in Sent.last()
    rows = (await session.execute(select(TelegramConnection))).scalars().all()
    assert not rows, "no connection may be created from a bad number"


async def test_a_bot_token_that_is_not_a_token_is_rejected(client, actor, state, session):
    user_id = uuid.UUID(actor.id)
    await handlers.add_bot(a_callback("add:bot"), state=state)
    await handlers.connect_bot_label(a_message("Sales bot"), state=state)
    await handlers.connect_bot_token(a_message("hello"), user_id=user_id, state=state)

    assert Sent.text_contains("does not look like a bot token")
    rows = (await session.execute(select(TelegramConnection))).scalars().all()
    assert not rows


async def test_the_bot_token_message_is_deleted(client, actor, state):
    user_id = uuid.UUID(actor.id)
    await handlers.add_bot(a_callback("add:bot"), state=state)
    await handlers.connect_bot_label(a_message("Sales bot"), state=state)

    deleted_before = Sent.deleted
    await handlers.connect_bot_token(
        a_message("123456789:AAEtestTokenValueThatIsLongEnough00"), user_id=user_id, state=state
    )
    assert Sent.deleted == deleted_before + 1


async def test_the_warning_about_chat_history_is_shown_before_every_secret(client, actor, state):
    """The tradeoff is real and is stated plainly rather than glossed over."""
    await handlers.add_bot(a_callback("add:bot"), state=state)
    await handlers.connect_bot_label(a_message("Sales bot"), state=state)
    assert "deleted from this chat" in Sent.last()

    Sent.reset()
    await handlers.add_account(a_callback("add:user"), state=state)
    await handlers.account_label(a_message("Main"), state=state)
    assert "deleted from this chat" in Sent.last()


# --------------------------------------------------------------------------- #
# Composing and sending an ad
# --------------------------------------------------------------------------- #
async def prepare_account(actor, groups: int = 3) -> str:
    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor,
        connection_id,
        [
            discovered(-1002000 - i, f"Group {i + 1:02d}", chat_kind="supergroup")
            for i in range(groups)
        ],
    )
    return connection_id


async def test_composing_an_ad_end_to_end(client, actor, state, session):
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=3)

    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("October offer"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Two for one this week."), user_id=user_id, state=state)

    assert "October offer" in Sent.last()
    assert "0 selected" in Sent.last() or "Groups — 0" in Sent.last()

    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    assert broadcast.body_text == "Two for one this week."
    assert broadcast.status is BroadcastStatus.draft

    # Pick every group, then send.
    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:pick"), user_id=user_id, state=state)
    assert "selected" in Sent.last()

    for index in range(3):
        await handlers.picker_actions(
            a_callback(f"{views.PICK}t{index}"), user_id=user_id, state=state
        )
    assert "3 selected" in Sent.last()

    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:save"), user_id=user_id, state=state)
    assert any("3 group(s) selected" in alert for alert in Sent.alerts)

    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:send"), user_id=user_id, state=state)

    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.sending
    assert any("Sending to 3 groups" in alert for alert in Sent.alerts)


async def test_the_send_button_stays_hidden_until_the_ad_is_ready(client, actor, state, session):
    """Offering a control that cannot work is worse than not offering it."""
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=1)

    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Half-written"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Some text"), user_id=user_id, state=state)

    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    assert f"ad:{broadcast.id}:send" not in Sent.buttons(), "no groups chosen yet"
    assert f"ad:{broadcast.id}:confirm" not in Sent.buttons()
    assert "at least one group" in Sent.last()


async def test_sending_an_ad_with_no_groups_explains_rather_than_fails(
    client, actor, state, session
):
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=1)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Nowhere"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Hello"), user_id=user_id, state=state)

    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:send"), user_id=user_id, state=state)

    assert any("at least one group" in alert for alert in Sent.alerts)
    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.draft


async def test_the_pause_between_groups_can_be_changed(client, actor, state, session):
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=2)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Paced"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Hi"), user_id=user_id, state=state)

    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:delay"), user_id=user_id, state=state)
    await handlers.ad_delay(a_message("7"), user_id=user_id, state=state)

    await session.refresh(broadcast)
    assert broadcast.delay_ms == 7000
    assert "7\\.0s" in Sent.last(), "the dot must be escaped for MarkdownV2"


async def test_a_nonsense_pause_is_rejected(client, actor, state, session):
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=1)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Paced"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Hi"), user_id=user_id, state=state)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()

    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:delay"), user_id=user_id, state=state)
    await handlers.ad_delay(a_message("soon"), user_id=user_id, state=state)
    assert "number of seconds" in Sent.last()

    await session.refresh(broadcast)
    assert broadcast.delay_ms == 3000, "unchanged"


async def test_the_repeat_can_be_set_and_turned_off(client, actor, state, session):
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=2)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Nightly"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Hi"), user_id=user_id, state=state)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()

    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:repeat"), user_id=user_id, state=state)
    await handlers.ad_repeat(a_message("6"), user_id=user_id, state=state)
    await session.refresh(broadcast)
    assert broadcast.repeat_every_s == 21_600
    assert "every 6" in Sent.last()

    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:repeat"), user_id=user_id, state=state)
    await handlers.ad_repeat(a_message("0"), user_id=user_id, state=state)
    await session.refresh(broadcast)
    assert broadcast.repeat_every_s is None, "0 means post once"
    assert "once, then stop" in Sent.last()


@pytest.mark.parametrize(
    ("answer", "seconds"),
    [
        ("6", 21_600),
        ("6h", 21_600),
        ("90m", 5_400),
        ("1h 30m", 5_400),
        ("120 minutes", 7_200),
        ("2 hours", 7_200),
        ("1.5h", 5_400),
    ],
)
async def test_a_repeat_can_be_given_in_minutes_or_hours(
    client, actor, state, session, answer, seconds
):
    """ "90m" cannot be said in whole hours, and a bare number has to keep
    meaning what the prompt says it means."""
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=1)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Nightly"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Hi"), user_id=user_id, state=state)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()

    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:repeat"), user_id=user_id, state=state)
    await handlers.ad_repeat(a_message(answer), user_id=user_id, state=state)

    await session.refresh(broadcast)
    assert broadcast.repeat_every_s == seconds


@pytest.mark.parametrize(
    "answer", ["soon", "30m", "inf", "nan", "99999", "-3", "3 apples", "", "1 fortnight"]
)
async def test_a_repeat_that_cannot_be_honoured_is_refused(client, actor, state, session, answer):
    """`inf` and `nan` parse as floats. Reaching the database, they are an
    overflow rather than a sentence anyone can act on."""
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=1)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Nightly"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Hi"), user_id=user_id, state=state)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()

    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:repeat"), user_id=user_id, state=state)
    await handlers.ad_repeat(a_message(answer), user_id=user_id, state=state)

    await session.refresh(broadcast)
    assert broadcast.repeat_every_s is None, "left unchanged"
    assert_valid_markdown_v2(Sent.last())


async def test_a_running_ad_can_be_edited(client, actor, state, session):
    """An ad that repeats for weeks needs its wording changed at some point, and
    the alternative — build a new one and re-pick 500 groups — is not one."""
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=2)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Live"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Old wording"), user_id=user_id, state=state)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    broadcast.status = BroadcastStatus.sending
    await session.commit()

    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:edit"), user_id=user_id, state=state)

    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.paused, "not edited underneath a running worker"
    assert broadcast.paused_reason_code == reasons.BROADCAST_BEING_EDITED
    assert "Paused while you edit" in Sent.last()
    assert f"ad:{broadcast.id}:pick:0" in Sent.buttons(), "groups editable too"

    await handlers.ad_text(a_message("New wording"), user_id=user_id, state=state)
    await session.refresh(broadcast)
    assert broadcast.body_text == "New wording"


async def test_editing_offers_no_discard_button(client, actor, state, session):
    """Discard deletes drafts. On a live ad the button would either do nothing
    or something alarming, and neither is a button worth showing."""
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=1)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Live"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Hi"), user_id=user_id, state=state)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    broadcast.status = BroadcastStatus.paused
    await session.commit()

    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:edit"), user_id=user_id, state=state)
    assert "Discard" not in Sent.last()


async def test_starting_a_second_ad_discards_the_unfinished_one(client, actor, state, session):
    """Two half-written ads carrying identical buttons would be impossible to
    tell apart in a chat."""
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=1)

    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("First"), user_id=user_id, state=state)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Second"), user_id=user_id, state=state)

    rows = (await session.execute(select(Broadcast))).scalars().all()
    assert [r.name for r in rows] == ["Second"]


# --------------------------------------------------------------------------- #
# The group picker
# --------------------------------------------------------------------------- #
async def open_picker(actor, state, session, groups: int = 12):
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=groups)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Picker"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Hi"), user_id=user_id, state=state)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:pick"), user_id=user_id, state=state)
    return broadcast, user_id


async def test_toggling_a_group_twice_leaves_it_unselected(client, actor, state, session):
    _, user_id = await open_picker(actor, state, session, groups=3)

    await handlers.picker_actions(a_callback(f"{views.PICK}t0"), user_id=user_id, state=state)
    assert "1 selected" in Sent.last()
    await handlers.picker_actions(a_callback(f"{views.PICK}t0"), user_id=user_id, state=state)
    assert "0 selected" in Sent.last()


async def test_select_page_only_selects_the_visible_page(client, actor, state, session):
    """12 groups over pages of 8: selecting page 0 must not silently pick 12."""
    _, user_id = await open_picker(actor, state, session, groups=12)

    await handlers.picker_actions(a_callback(f"{views.PICK}a0"), user_id=user_id, state=state)
    assert f"{views.PICKER_PAGE_SIZE} selected" in Sent.last()


async def test_clear_all_clears_across_pages(client, actor, state, session):
    _, user_id = await open_picker(actor, state, session, groups=12)

    await handlers.picker_actions(a_callback(f"{views.PICK}a0"), user_id=user_id, state=state)
    await handlers.picker_actions(a_callback(f"{views.PICK}a1"), user_id=user_id, state=state)
    assert "12 selected" in Sent.last()

    await handlers.picker_actions(a_callback(f"{views.PICK}n0"), user_id=user_id, state=state)
    assert "0 selected" in Sent.last()


async def test_paging_keeps_the_selection(client, actor, state, session):
    _, user_id = await open_picker(actor, state, session, groups=12)

    await handlers.picker_actions(a_callback(f"{views.PICK}t0"), user_id=user_id, state=state)
    await handlers.picker_actions(a_callback(f"{views.PICK}p1"), user_id=user_id, state=state)
    assert "1 selected" in Sent.last()


async def test_an_out_of_range_index_is_ignored_rather_than_crashing(client, actor, state, session):
    """A stale keyboard from an earlier, longer list must not raise."""
    _, user_id = await open_picker(actor, state, session, groups=3)

    await handlers.picker_actions(a_callback(f"{views.PICK}t99"), user_id=user_id, state=state)
    assert "0 selected" in Sent.last()


async def test_the_picker_only_offers_groups_the_account_can_post_in(client, actor, state, session):
    """Listing a group it cannot write to invites choosing it and finding out
    300 deliveries later."""
    from app.db.models import ConnectionChatAccess, TelegramChat

    _, user_id = await open_picker(actor, state, session, groups=3)

    blocked = (
        (await session.execute(select(TelegramChat).order_by(TelegramChat.title))).scalars().first()
    )
    access = await session.get(ConnectionChatAccess, blocked.id)
    access.can_post_destination = False
    await session.commit()

    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:pick"), user_id=user_id, state=state)
    assert "of 2" in Sent.last(), "the group it cannot post in is not offered"


async def test_the_picker_explains_what_to_do_when_nothing_is_synced(client, actor, state, session):
    user_id = uuid.UUID(actor.id)
    await connect_bot(actor)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Empty"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Hi"), user_id=user_id, state=state)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()

    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:pick"), user_id=user_id, state=state)
    assert "Sync groups" in Sent.last()


# --------------------------------------------------------------------------- #
# Auto-reply
# --------------------------------------------------------------------------- #
async def test_writing_and_enabling_an_auto_reply(client, actor, state, session):
    from app.repositories import autoreply as autoreply_repo

    user_id = uuid.UUID(actor.id)
    connection_id = await prepare_account(actor, groups=1)

    await handlers.autoreply_actions(a_callback("ar:text"), user_id=user_id, state=state)
    await handlers.autoreply_text(
        a_message("Thanks for your message, we will reply shortly."),
        user_id=user_id,
        state=state,
    )
    assert "not written yet" not in Sent.last()

    await handlers.autoreply_actions(a_callback("ar:on"), user_id=user_id, state=state)
    reply = await autoreply_repo.get_for_connection(session, connection_id=uuid.UUID(connection_id))
    assert reply.enabled


async def test_it_cannot_be_switched_on_before_the_text_is_written(client, actor, state, session):
    user_id = uuid.UUID(actor.id)
    await prepare_account(actor, groups=1)

    await handlers.autoreply_actions(a_callback("ar:on"), user_id=user_id, state=state)
    assert any("Write the reply text" in alert for alert in Sent.alerts)


async def test_the_screen_says_it_only_answers_people_who_write_first(client, actor, state):
    """The safety property is the product's promise, so it is on the screen."""
    await prepare_account(actor, groups=1)
    await handlers.nav_autoreply(a_callback("nav:autoreply"), user_id=uuid.UUID(actor.id))

    text = Sent.last()
    assert "message this account first" in text or "write to you first" in text
    assert "never posts in" in text


async def test_the_wait_can_be_changed_and_has_a_floor(client, actor, state, session):
    from app.repositories import autoreply as autoreply_repo

    user_id = uuid.UUID(actor.id)
    connection_id = await prepare_account(actor, groups=1)
    await handlers.autoreply_actions(a_callback("ar:text"), user_id=user_id, state=state)
    await handlers.autoreply_text(a_message("Hello"), user_id=user_id, state=state)

    await handlers.autoreply_actions(a_callback("ar:cooldown"), user_id=user_id, state=state)
    await handlers.autoreply_cooldown(a_message("0.5"), user_id=user_id, state=state)
    assert "at least 1 hour" in Sent.last()

    await handlers.autoreply_cooldown(a_message("12"), user_id=user_id, state=state)
    reply = await autoreply_repo.get_for_connection(session, connection_id=uuid.UUID(connection_id))
    assert reply.cooldown_s == 12 * 3600


# --------------------------------------------------------------------------- #
# Connection management
# --------------------------------------------------------------------------- #
async def test_syncing_groups_is_queued_not_executed_in_the_handler(client, actor, session):
    """The bot never blocks on Telegram; a slow sync must not freeze the panel."""
    from app.adapters.factory import mock_script_for

    user_id = uuid.UUID(actor.id)
    connection_id = await connect_bot(actor)
    script = mock_script_for(uuid.UUID(connection_id))
    script.calls.clear()

    await handlers.connection_actions(a_callback(f"conn:{connection_id}:sync"), user_id=user_id)

    assert script.calls_to("list_available_chats") == []
    task = (
        (
            await session.execute(
                select(ControlTask).where(ControlTask.kind == ControlTaskKind.sync_chats)
            )
        )
        .scalars()
        .first()
    )
    assert task is not None


async def test_disconnecting_asks_first(client, actor, session):
    user_id = uuid.UUID(actor.id)
    connection_id = await connect_bot(actor)

    await handlers.connection_actions(a_callback(f"conn:{connection_id}:askdel"), user_id=user_id)
    assert "Disconnect" in Sent.last()
    assert "cannot unsend" in Sent.last()

    tasks = (
        (
            await session.execute(
                select(ControlTask).where(ControlTask.kind == ControlTaskKind.disconnect)
            )
        )
        .scalars()
        .all()
    )
    assert not tasks, "nothing happens until it is confirmed"


async def test_another_account_s_connection_does_not_resolve(client, actor, other_actor):
    connection_id = await connect_bot(actor)
    await handlers.connection_actions(
        a_callback(f"conn:{connection_id}:sync"), user_id=uuid.UUID(other_actor.id)
    )
    assert any("no longer exists" in alert for alert in Sent.alerts)


# --------------------------------------------------------------------------- #
# Stale and malformed input
# --------------------------------------------------------------------------- #
async def test_a_button_for_something_that_was_deleted_says_so(client, actor, state):
    await handlers.ad_actions(
        a_callback(f"ad:{uuid.uuid4()}"), user_id=uuid.UUID(actor.id), state=state
    )
    assert any("no longer exists" in alert for alert in Sent.alerts)


async def test_a_malformed_callback_does_not_crash(client, actor, state):
    for data in ("ad:not-a-uuid", "rule:", "conn:zzz"):
        Sent.reset()
        query = a_callback(data)
        if data.startswith("ad"):
            await handlers.ad_actions(query, user_id=uuid.UUID(actor.id), state=state)
        elif data.startswith("rule"):
            await handlers.rule_actions(query, user_id=uuid.UUID(actor.id), state=state)
        else:
            await handlers.connection_actions(query, user_id=uuid.UUID(actor.id))
        assert Sent.alerts, f"{data} produced no response at all"


async def test_cancel_clears_a_half_finished_flow(client, actor, state):
    await handlers.add_account(a_callback("add:user"), state=state)
    await handlers.account_label(a_message("Main"), state=state)
    assert await state.get_state() is not None

    await handlers.cancel_flow(a_message("/cancel"), user_id=uuid.UUID(actor.id), state=state)
    assert await state.get_state() is None
    assert "InsightAdFlow" in Sent.last()


async def test_help_lists_what_the_product_does_and_does_not_do(client):
    await handlers.help_command(a_message("/help"))
    text = Sent.last()
    assert "/ads" in text
    assert "already joined" in text
    assert "have not written to you" in text


# --------------------------------------------------------------------------- #
# The escaping checker itself
# --------------------------------------------------------------------------- #
def test_the_markdown_checker_catches_what_telegram_would_reject():
    assert_valid_markdown_v2("A properly escaped sentence\\.")
    assert_valid_markdown_v2("*bold* and _italic_ and `a.b` code")

    for broken in ("Missing a full stop.", "An (unescaped) bracket", "A - dash"):
        with pytest.raises(AssertionError):
            assert_valid_markdown_v2(broken)


def test_the_checker_agrees_with_telegram_s_documented_list():
    """Pinned so a future edit cannot quietly narrow it."""
    documented = set(r"_*[]()~`>#+-=|{}.!")
    # `_`, `*` and backtick are excluded deliberately: they open formatting.
    assert documented - set("_*`") == MDV2_MUST_ESCAPE
    assert not re.search(r"[a-zA-Z0-9]", "".join(MDV2_MUST_ESCAPE))


# --------------------------------------------------------------------------- #
# Every status value, through every screen
# --------------------------------------------------------------------------- #
def a_connection(status: str, kind: str = "user"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        label="Master",
        kind=SimpleNamespace(value=kind),
        status=SimpleNamespace(value=status),
        telegram_username="someone",
        last_error_message_safe=None,
    )


def a_broadcast(status, **overrides):
    return fake_broadcast(status=status, **overrides)


def a_rule(status):
    return SimpleNamespace(
        id=uuid.uuid4(), name="Rule", status=status, delay_ms=0, paused_reason_code=None
    )


@pytest.mark.parametrize("status", [s.value for s in ConnectionStatus])
def test_every_connection_status_renders(status):
    """`awaiting_code`, `awaiting_2fa` and `paused_safety` all contain an
    underscore. Unescaped, that opens italics MarkdownV2 never closes, and
    Telegram drops the whole screen — which is exactly how Accounts broke."""
    connection = a_connection(status)

    for screen in (
        views.connections_list(connections=[connection]),
        views.connection_detail(connection=connection, chat_count=0),
        views.confirm_disconnect(connection=connection),
        views.home(connections=[connection], rules=[], broadcasts=[], counts={}),
    ):
        assert_valid_markdown_v2(screen.text)
        assert_keyboard_is_sendable(screen.keyboard)


@pytest.mark.parametrize("status", list(JobStatus))
def test_every_delivery_status_renders(status):
    """`needs_attention` and `dead_letter` reach the screen through the delivery
    counts, which is a different code path from the status line."""
    counts = {status.value: 3}

    for screen in (
        views.ad_detail(
            broadcast=a_broadcast(BroadcastStatus.sending), counts=counts, target_count=3
        ),
        views.rule_detail(
            rule=a_rule(RuleStatus.active),
            source_titles=["Source"],
            destination_count=3,
            job_counts=counts,
            preview="A preview sentence.",
        ),
    ):
        assert_valid_markdown_v2(screen.text)
        assert_keyboard_is_sendable(screen.keyboard)


@pytest.mark.parametrize("status", list(BroadcastStatus))
def test_every_broadcast_status_renders(status):
    for screen in (
        views.ad_detail(broadcast=a_broadcast(status), counts={}, target_count=1),
        views.ads_list(broadcasts=[a_broadcast(status)], page=0, can_create=True),
    ):
        assert_valid_markdown_v2(screen.text)
        assert_keyboard_is_sendable(screen.keyboard)


@pytest.mark.parametrize("status", list(RuleStatus))
def test_every_rule_status_renders(status):
    for screen in (
        views.rules_list(rules=[a_rule(status)], page=0, can_create=True),
        views.rule_detail(
            rule=a_rule(status),
            source_titles=["Source"],
            destination_count=1,
            job_counts={},
            preview="A preview sentence.",
        ),
    ):
        assert_valid_markdown_v2(screen.text)
        assert_keyboard_is_sendable(screen.keyboard)


@pytest.mark.parametrize("code", sorted(reasons.REASON_TEXT))
def test_every_reason_sentence_survives_being_rendered(code):
    """Reason text is written by hand and lands inside italics on several
    screens. One with a stray underscore would break the screen carrying it."""
    connection = a_connection("error")
    connection.last_error_message_safe = reasons.describe(code)

    screen = views.connections_list(connections=[connection])
    assert_valid_markdown_v2(screen.text)


def test_the_checker_catches_the_bug_that_broke_the_accounts_screen():
    """A regression guard on the guard: the exact text Telegram rejected must
    fail here, or this whole class of bug can come back unnoticed."""
    broken = "\U0001f517 *Accounts*\n\n* *Master* — user, awaiting_code"

    with pytest.raises(AssertionError, match="never closed"):
        assert_valid_markdown_v2(broken)


def test_the_checker_accepts_correctly_paired_formatting():
    assert_valid_markdown_v2("*bold* and _italic_ together")
    assert_valid_markdown_v2("an escaped \\_underscore\\_ is not a delimiter")
    assert_valid_markdown_v2("`a_b` inside code is literal")
