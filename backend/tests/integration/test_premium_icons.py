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
    premium_icons.set_labels({})
    Sent.reset()
    yield
    premium_icons.set_map({})
    premium_icons.set_labels({})


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
async def test_screens_go_out_with_premium_text_and_button_icons(client, actor, state):
    """Text emoji become inline custom emoji; a button's leading emoji becomes
    its ``icon_custom_emoji_id``, drawn before the label."""
    from app.adminbot import handlers

    premium_icons.set_map({"📡": "999000111", "📣": "999000222"})
    await handlers.start(handlers_message("/start"), user_id=uuid.UUID(actor.id), state=state)

    text, markup = Sent.messages[-1]
    assert "tg://emoji?id=999000111" in text
    assert_valid_markdown_v2(text)

    ads_buttons = [
        b
        for row in markup.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("nav:ads")
    ]
    assert ads_buttons, "the home screen offers Ads"
    assert ads_buttons[0].icon_custom_emoji_id == "999000222"
    assert not ads_buttons[0].text.startswith("📣"), "the icon replaces the emoji, not joins it"
    for row in markup.inline_keyboard:
        for button in row:
            assert "tg://emoji" not in button.text, "the token syntax never belongs in a label"


def test_a_button_that_is_only_an_emoji_keeps_its_text():
    """A button must keep visible text, so pager arrows stay as they are."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    premium_icons.set_map({"⬅️": "111"})
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️", callback_data="nav:ads:0")]]
    )
    out = premium_icons.apply_keyboard(markup)
    assert out is markup, "nothing to change, same object back"


def test_the_keyboard_transform_preserves_callbacks():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    premium_icons.set_map({"📣": "222"})
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📣 Ads", callback_data="nav:ads:0")]]
    )
    out = premium_icons.apply_keyboard(markup)
    button = out.inline_keyboard[0][0]
    assert button.text == "Ads"
    assert button.icon_custom_emoji_id == "222"
    assert button.callback_data == "nav:ads:0"
    # And the original screen object was not mutated in place.
    assert markup.inline_keyboard[0][0].text == "📣 Ads"


async def test_a_rejected_premium_message_falls_back_to_plain(client, actor, state):
    """A bot without a Fragment username gets a 400 for custom emoji. The
    panel must degrade to plain icons, never to a blank screen."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    from app.adminbot import handlers

    premium_icons.set_map({"📡": "999000111"})

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📡 Panel", callback_data="nav:home")]]
    )
    sent: list[tuple[str, object]] = []

    async def send(text: str, markup) -> None:
        sent.append((text, markup))
        if "tg://emoji" in text:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=1, text=""),
                message="Bad Request: can't parse entities: custom emoji",
            )

    await handlers._deliver(send, "📡 *Panel*", keyboard)

    assert len(sent) == 2, "the premium attempt, then the plain retry"
    assert "tg://emoji" in sent[0][0]
    assert sent[0][1].inline_keyboard[0][0].icon_custom_emoji_id is not None
    assert sent[1] == ("📡 *Panel*", keyboard), "the retry is the untouched original"
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
        a_callback("op:emoji:run"), user_id=uuid.UUID(actor.id), state=state, is_operator=True
    )

    stored = await panel_emoji_repo.get_map(session)
    assert len(stored) == len(views.PANEL_EMOJI)
    assert premium_icons.enabled()
    assert "Live" in Sent.last() or "Extracted" in Sent.last()
    assert_valid_markdown_v2(premium_icons.strip(Sent.last()))


async def test_extraction_is_operator_only(client, actor, state, session):
    from app.adminbot import handlers

    await handlers.op_emoji(
        a_callback("op:emoji:run"), user_id=uuid.UUID(actor.id), state=state, is_operator=False
    )
    assert await panel_emoji_repo.get_map(session) == {}
    assert any("not available" in alert for alert in Sent.alerts)


async def test_extraction_needs_a_user_connection(client, actor, state):
    """The Bot API has no emoji search, and pretending otherwise would just
    store nothing and claim success."""
    from app.adminbot import handlers

    await connect_bot(actor)
    await handlers.op_emoji(
        a_callback("op:emoji:run"), user_id=uuid.UUID(actor.id), state=state, is_operator=True
    )
    assert any("Connect a Telegram account" in alert for alert in Sent.alerts)


async def test_turning_it_off_clears_the_map(client, actor, state, session):
    from app.adminbot import handlers

    premium_icons.set_map({"🔥": FIRE_ID})
    await panel_emoji_repo.replace(session, mapping={"🔥": FIRE_ID})
    await session.commit()

    await handlers.op_emoji(
        a_callback("op:emoji:off"), user_id=uuid.UUID(actor.id), state=state, is_operator=True
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
    assert "needs no login at all" in empty.text, "the send-emojis path is offered first"
    assert_valid_markdown_v2(empty.text)


def handlers_message(text):
    from tests.integration.test_bot_flows import a_message

    return a_message(text)


# --------------------------------------------------------------------------- #
# Extraction by just sending emojis — no login, no connection
# --------------------------------------------------------------------------- #
def _premium_message(text: str, emoji_ids: dict[str, str]):
    """A message whose premium emojis carry entities at true UTF-16 offsets."""
    from aiogram.types import MessageEntity

    from tests.integration.test_bot_flows import a_message

    entities = []
    for char, custom_id in emoji_ids.items():
        python_index = text.index(char)
        offset = len(text[:python_index].encode("utf-16-le")) // 2
        length = len(char.encode("utf-16-le")) // 2
        entities.append(
            MessageEntity(
                type="custom_emoji", offset=offset, length=length, custom_emoji_id=custom_id
            )
        )
    message = a_message(text)
    return message.model_copy(update={"entities": entities})


def test_utf16_offsets_map_the_right_character():
    """An emoji is a surrogate pair in UTF-16 — slicing by Python index maps
    the wrong character, which would draw someone's flame on the ✅ icon."""
    from app.adminbot.handlers import _custom_emoji_pairs

    # Two astral-plane emoji before the target shift Python and UTF-16 apart.
    message = _premium_message("🔥🔥 then ✅ done", {"🔥": "111", "✅": "222"})
    pairs = _custom_emoji_pairs(message)
    assert pairs == {"🔥": "111", "✅": "222"}


async def test_the_operator_can_extract_by_just_sending_emojis(client, actor, state, session):
    """No login and no connected account: the ids ride in on the message
    itself, because a custom emoji is a character plus an entity naming it."""
    from app.adminbot import handlers
    from app.adminbot.states import IconSetup

    await handlers.op_emoji(
        a_callback("op:emoji:send"), user_id=uuid.UUID(actor.id), state=state, is_operator=True
    )
    assert await state.get_state() == IconSetup.collect.state

    await handlers.icon_collect(
        _premium_message("🔥 ✅", {"🔥": "555", "✅": "666"}),
        user_id=uuid.UUID(actor.id),
        state=state,
    )

    stored = await panel_emoji_repo.get_map(session)
    assert stored == {"🔥": "555", "✅": "666"}
    assert premium_icons.enabled()
    assert "2 icons mapped" in Sent.last()

    # A second message merges rather than replaces.
    await handlers.icon_collect(
        _premium_message("⚠️", {"⚠️": "777"}), user_id=uuid.UUID(actor.id), state=state
    )
    stored = await panel_emoji_repo.get_map(session)
    assert stored == {"🔥": "555", "✅": "666", "⚠️": "777"}


async def test_a_plain_emoji_message_is_explained_not_saved(client, actor, state, session):
    """Keyboard emoji without entities carry no id — saying so beats silence."""
    from app.adminbot import handlers
    from app.adminbot.states import IconSetup
    from tests.integration.test_bot_flows import a_message

    await state.set_state(IconSetup.collect)
    await handlers.icon_collect(a_message("🔥 ✅"), user_id=uuid.UUID(actor.id), state=state)

    assert await panel_emoji_repo.get_map(session) == {}
    assert "No premium emoji" in Sent.last()


# --------------------------------------------------------------------------- #
# Renaming buttons
# --------------------------------------------------------------------------- #
async def test_an_operator_renames_a_button_and_it_comes_from_the_database(
    client, actor, state, session
):
    from app.adminbot import handlers
    from app.repositories import panel_buttons as panel_buttons_repo

    index = views.RENAMEABLE_BUTTONS.index("📣 Ads")
    await handlers.op_buttons(
        a_callback(f"op:btn:pick:{index}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )
    from tests.integration.test_bot_flows import a_message

    await handlers.button_label(a_message("🚀 Campaigns"), user_id=uuid.UUID(actor.id), state=state)

    assert await panel_buttons_repo.get_map(session) == {"📣 Ads": "🚀 Campaigns"}
    assert premium_icons.get_labels() == {"📣 Ads": "🚀 Campaigns"}

    # And the home screen now carries it.
    Sent.reset()
    await handlers.start(a_message("/start"), user_id=uuid.UUID(actor.id), state=state)
    labels = [b.text for row in Sent.messages[-1][1].inline_keyboard for b in row]
    assert "🚀 Campaigns" in labels
    assert "📣 Ads" not in labels

    # `-` goes back to the built-in.
    await handlers.op_buttons(
        a_callback(f"op:btn:pick:{index}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )
    await handlers.button_label(a_message("-"), user_id=uuid.UUID(actor.id), state=state)
    assert await panel_buttons_repo.get_map(session) == {}


async def test_renaming_is_operator_only(client, actor, state, session):
    from app.adminbot import handlers

    await handlers.op_buttons(
        a_callback("op:btn:pick:0"), user_id=uuid.UUID(actor.id), state=state, is_operator=False
    )
    assert any("not available" in alert for alert in Sent.alerts)


def test_labels_apply_before_icons_so_both_compose():
    """The rename key is the plain default; the icon pass then upgrades the
    renamed label's own leading emoji. Order the other way, the key would
    never match."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    premium_icons.set_labels({"📣 Ads": "🔥 Campaigns"})
    premium_icons.set_map({"🔥": "999"})
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📣 Ads", callback_data="nav:ads:0")]]
    )
    renamed = premium_icons.apply_labels(markup)
    final = premium_icons.apply_keyboard(renamed)
    button = final.inline_keyboard[0][0]
    assert button.text == "Campaigns"
    assert button.icon_custom_emoji_id == "999"
    premium_icons.set_labels({})


def test_a_custom_label_is_part_of_the_plain_retry():
    """Only premium icons ever fall back — a rename is plain text and stays."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    premium_icons.set_labels({"📣 Ads": "Campaigns"})
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📣 Ads", callback_data="nav:ads:0")]]
    )
    labelled = premium_icons.apply_labels(markup)
    assert labelled.inline_keyboard[0][0].text == "Campaigns"
    premium_icons.set_labels({})
