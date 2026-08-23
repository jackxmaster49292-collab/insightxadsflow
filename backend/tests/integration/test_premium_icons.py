"""The panel's premium icons: extraction, rendering, and the honest fallback.

Two Telegram rules frame every test here. Button labels cannot carry custom
emoji at all, and a bot without a Fragment username may not send them in
messages either — so the transform must upgrade message text only, and must
degrade to plain unicode rather than to a blank screen when Telegram refuses.
"""

from __future__ import annotations

import uuid

import pytest

from app.adminbot import premium_icons, views
from app.repositories import panel_emoji as panel_emoji_repo
from tests.conftest import connect_bot, script_for
from tests.integration.test_bot_flows import (
    ADMIN_CHAT,
    Sent,
    a_callback,
    assert_valid_markdown_v2,
)

FIRE_ID = "5368324170671202286"


@pytest.fixture(autouse=True)
def _plain_icons():
    """Every test starts with plain icons and a clean send recorder."""
    premium_icons.set_map({})
    Sent.reset()
    yield
    premium_icons.set_map({})


@pytest.fixture
def state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_CHAT, user_id=ADMIN_CHAT),
    )


# --------------------------------------------------------------------------- #
# The transform
# --------------------------------------------------------------------------- #
def test_a_mapped_emoji_becomes_a_custom_emoji_token():
    premium_icons.set_map({"🔥": FIRE_ID})
    out = premium_icons.apply("Offer 🔥 is live")
    assert out == f"Offer ![🔥](tg://emoji?id={FIRE_ID}) is live"
    assert_valid_markdown_v2(out)


def test_strip_is_the_exact_inverse_of_apply():
    premium_icons.set_map({"🔥": FIRE_ID, "✅": "111", "⚠️": "222"})
    original = "✅ done · ⚠️ careful · 🔥 hot"
    assert premium_icons.strip(premium_icons.apply(original)) == original


def test_an_unmapped_emoji_stays_plain():
    premium_icons.set_map({"🔥": FIRE_ID})
    assert premium_icons.apply("plain ✅ here") == "plain ✅ here"


def test_variation_selector_forms_do_not_double_wrap():
    """ "⚠️" contains "⚠" — the longer key must win, once."""
    premium_icons.set_map({"⚠️": "222", "⚠": "333"})
    out = premium_icons.apply("⚠️ watch")
    assert out.count("tg://emoji") == 1
    assert "id=222" in out


def test_an_empty_map_changes_nothing():
    assert premium_icons.apply("🔥 untouched") == "🔥 untouched"
    assert not premium_icons.enabled()


def test_suspension_stops_the_transform_but_keeps_the_map():
    premium_icons.set_map({"🔥": FIRE_ID})
    premium_icons.suspend()
    assert premium_icons.apply("🔥") == "🔥"
    assert premium_icons.suspended()
    # A fresh map lifts the suspension — that is the retry path.
    premium_icons.set_map({"🔥": FIRE_ID})
    assert premium_icons.enabled()


# --------------------------------------------------------------------------- #
# The send path
# --------------------------------------------------------------------------- #
async def test_screens_go_out_with_premium_icons_but_buttons_stay_plain(client, actor, state):
    """Telegram's rule, not a choice: button labels cannot carry entities."""
    from app.adminbot import handlers

    premium_icons.set_map({"📡": "999000111"})
    await handlers.start(handlers_message("/start"), user_id=uuid.UUID(actor.id), state=state)

    text, markup = Sent.messages[-1]
    assert "tg://emoji?id=999000111" in text
    assert_valid_markdown_v2(text)
    for row in markup.inline_keyboard:
        for button in row:
            assert "tg://emoji" not in button.text, "button labels must stay plain"


async def test_a_rejected_premium_message_falls_back_to_plain(client, actor, state):
    """A bot without a Fragment username gets a 400 for custom emoji. The
    panel must degrade to plain icons, never to a blank screen."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    from app.adminbot import handlers

    premium_icons.set_map({"📡": "999000111"})

    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)
        if "tg://emoji" in text:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=1, text=""),
                message="Bad Request: can't parse entities: custom emoji",
            )

    await handlers._deliver(send, "📡 *Panel*")

    assert len(sent) == 2, "the premium attempt, then the plain retry"
    assert "tg://emoji" in sent[0]
    assert sent[1] == "📡 *Panel*"
    assert premium_icons.suspended(), "and it stops trying until re-extracted"


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
async def test_an_operator_extracts_ids_from_their_own_account(client, actor, state, session):
    """The ids are Telegram documents — extraction is the only honest source."""
    from app.adminbot import handlers
    from app.db.models import ConnectionKind, ConnectionStatus
    from app.repositories import connections as connection_repo

    connection = await connection_repo.create(
        session,
        user_id=uuid.UUID(actor.id),
        kind=ConnectionKind.user,
        label="Jack",
        status=ConnectionStatus.active,
    )
    await session.commit()
    connection_id = connection.id
    script = script_for(connection_id)
    for emoticon in views.PANEL_EMOJI:
        script.custom_emoji[emoticon] = [f"55{abs(hash(emoticon)) % 10**15}"]

    await handlers.op_emoji(
        a_callback("op:emoji:run"), user_id=uuid.UUID(actor.id), is_operator=True
    )

    stored = await panel_emoji_repo.get_map(session)
    assert len(stored) == len(views.PANEL_EMOJI)
    assert premium_icons.enabled()
    assert "Live" in Sent.last() or "Extracted" in Sent.last()
    assert_valid_markdown_v2(premium_icons.strip(Sent.last()))


async def test_extraction_is_operator_only(client, actor, state, session):
    from app.adminbot import handlers

    await handlers.op_emoji(
        a_callback("op:emoji:run"), user_id=uuid.UUID(actor.id), is_operator=False
    )
    assert await panel_emoji_repo.get_map(session) == {}
    assert any("not available" in alert for alert in Sent.alerts)


async def test_extraction_needs_a_user_connection(client, actor, state):
    """The Bot API has no emoji search, and pretending otherwise would just
    store nothing and claim success."""
    from app.adminbot import handlers

    await connect_bot(actor)
    await handlers.op_emoji(
        a_callback("op:emoji:run"), user_id=uuid.UUID(actor.id), is_operator=True
    )
    assert any("Connect a Telegram account" in alert for alert in Sent.alerts)


async def test_turning_it_off_clears_the_map(client, actor, state, session):
    from app.adminbot import handlers

    premium_icons.set_map({"🔥": FIRE_ID})
    await panel_emoji_repo.replace(session, mapping={"🔥": FIRE_ID})
    await session.commit()

    await handlers.op_emoji(
        a_callback("op:emoji:off"), user_id=uuid.UUID(actor.id), is_operator=True
    )

    assert await panel_emoji_repo.get_map(session) == {}
    assert not premium_icons.enabled()


def test_the_status_screen_tells_the_fragment_truth():
    """An operator who was not told about the Fragment rule would read plain
    icons as this feature being broken."""
    screen = views.premium_icons_status(
        extracted={"🔥": FIRE_ID},
        live=False,
        suspended=True,
        has_user_connection=True,
    )
    assert "Fragment username" in screen.text
    assert_valid_markdown_v2(screen.text)

    empty = views.premium_icons_status(
        extracted={}, live=False, suspended=False, has_user_connection=False
    )
    assert "connect one under" in empty.text
    assert_valid_markdown_v2(empty.text)


def handlers_message(text):
    from tests.integration.test_bot_flows import a_message

    return a_message(text)
