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
    assert_keyboard_is_sendable,
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
    await connect_bot(actor)  # past the role question, onto the panel
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
    for emoticon in views.panel_emoji():
        script.custom_emoji[emoticon] = [f"55{abs(hash(emoticon)) % 10**15}"]

    await handlers.op_emoji(
        a_callback("op:emoji:run"), user_id=uuid.UUID(actor.id), state=state, is_operator=True
    )

    stored = await panel_emoji_repo.get_map(session)
    assert len(stored) == len(views.panel_emoji())
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
    assert "Nothing usable" in Sent.last()
    assert "type the pair" in Sent.last(), "and the other way in"


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
    await connect_bot(actor)
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


# --------------------------------------------------------------------------- #
# Fetching them without being asked
# --------------------------------------------------------------------------- #
async def _operator_account(session, actor, monkeypatch, label="Jack"):
    """Make ``actor`` an operator with a live Telegram account.

    ``monkeypatch`` rather than assignment: the settings object is cached for
    the process, and a test that widened the operator list permanently would
    quietly turn later tests into tests of something else.
    """
    from app.config import get_settings
    from app.db.models import ConnectionKind, ConnectionStatus
    from app.repositories import connections as connection_repo
    from app.repositories import users as user_repo

    monkeypatch.setattr(get_settings(), "admin_telegram_ids", str(ADMIN_CHAT), raising=False)

    user = await user_repo.get_by_id(session, uuid.UUID(actor.id))
    user.telegram_user_id = ADMIN_CHAT
    connection = await connection_repo.create(
        session,
        user_id=uuid.UUID(actor.id),
        kind=ConnectionKind.user,
        label=label,
        status=ConnectionStatus.active,
    )
    await session.commit()
    return connection


async def test_icons_are_fetched_at_startup_without_anyone_asking(
    client, actor, session, monkeypatch
):
    """The operator should not have to tap anything: the ids come from Telegram
    either way, and there is nothing a human adds to the process."""
    from app.adminbot import icon_setup
    from app.repositories import panel_emoji as panel_emoji_repo

    connection = await _operator_account(session, actor, monkeypatch)
    script = script_for(connection.id)
    for emoticon in views.panel_emoji():
        script.custom_emoji[emoticon] = [f"55{abs(hash(emoticon)) % 10**15}"]

    await icon_setup.ensure_icons()

    stored = await panel_emoji_repo.get_map(session)
    assert len(stored) == len(views.panel_emoji())
    assert premium_icons.enabled()


async def test_a_restart_does_not_refetch(client, actor, session, monkeypatch):
    """Forty needless lookups on every restart is not free, and the ids do not
    change."""
    from app.adminbot import icon_setup
    from app.repositories import panel_emoji as panel_emoji_repo

    connection = await _operator_account(session, actor, monkeypatch)
    script = script_for(connection.id)
    script.custom_emoji["🔥"] = ["111"]

    await panel_emoji_repo.replace(session, mapping={"🔥": "999"})
    await session.commit()

    await icon_setup.ensure_icons()

    assert script.calls_to("custom_emoji_ids") == [], "nothing was asked"
    assert (await panel_emoji_repo.get_map(session))["🔥"] == "999", "kept as stored"


async def test_no_operator_account_means_no_fetch_and_no_crash(client, actor, session):
    """A deployment where nobody has connected an account yet still starts."""
    from app.adminbot import icon_setup
    from app.repositories import panel_emoji as panel_emoji_repo

    await connect_bot(actor)  # a bot connection cannot search emoji
    await icon_setup.ensure_icons()

    assert await panel_emoji_repo.get_map(session) == {}
    assert not premium_icons.enabled()


async def test_only_an_operators_account_is_ever_used(client, actor, other_actor, session):
    """Reaching for a customer's Telegram session to decorate the panel would
    be using their account for something they never asked for."""
    from app.adminbot import icon_setup
    from app.db.models import ConnectionKind, ConnectionStatus
    from app.repositories import connections as connection_repo

    # A customer — not an operator — with a perfectly usable account.
    await connection_repo.create(
        session,
        user_id=uuid.UUID(other_actor.id),
        kind=ConnectionKind.user,
        label="Someone else",
        status=ConnectionStatus.active,
    )
    await session.commit()

    assert await icon_setup.operator_connection(session) is None

    await icon_setup.ensure_icons()
    from app.repositories import panel_emoji as panel_emoji_repo

    assert await panel_emoji_repo.get_map(session) == {}


async def test_a_telegram_failure_mid_fetch_does_not_stop_the_panel(
    client, actor, session, monkeypatch
):
    """Icons are decoration. A failure here must never be why the bot is down."""
    from app.adminbot import icon_setup
    from app.repositories import panel_emoji as panel_emoji_repo

    connection = await _operator_account(session, actor, monkeypatch)
    script_for(connection.id).fail_method("custom_emoji_ids", RuntimeError("telegram said no"))

    await icon_setup.ensure_icons()  # must not raise

    assert await panel_emoji_repo.get_map(session) == {}


async def test_an_emoji_telegram_has_no_premium_version_of_stays_plain(
    client, actor, session, monkeypatch
):
    """A partial map is the normal outcome, not a failure."""
    from app.adminbot import icon_setup
    from app.repositories import panel_emoji as panel_emoji_repo

    connection = await _operator_account(session, actor, monkeypatch)
    script_for(connection.id).custom_emoji["🔥"] = ["12345"]

    await icon_setup.ensure_icons()

    stored = await panel_emoji_repo.get_map(session)
    assert stored == {"🔥": "12345"}
    assert premium_icons.apply("🔥 and ✅") == "![🔥](tg://emoji?id=12345) and ✅"


# --------------------------------------------------------------------------- #
# Using an id you were simply given
# --------------------------------------------------------------------------- #
def test_markup_becomes_real_entities_at_true_utf16_offsets():
    """The whole point of accepting ids: a pack this account does not own can
    still be used. Offsets are UTF-16 — an emoji is a surrogate pair there, so
    counting characters would misplace every entity after the first."""
    text, entities = premium_icons.parse_entities(
        "Sale ![🔥](tg://emoji?id=111) now ![✅](tg://emoji?id=222) end"
    )

    assert text == "Sale 🔥 now ✅ end"
    raw = text.encode("utf-16-le")
    for entity, expected in zip(entities, ["🔥", "✅"], strict=True):
        sliced = raw[entity["offset"] * 2 : (entity["offset"] + entity["length"]) * 2]
        assert sliced.decode("utf-16-le") == expected
    assert [e["custom_emoji_id"] for e in entities] == ["111", "222"]


def test_text_without_markup_is_untouched():
    text, entities = premium_icons.parse_entities("Just a sale 🔥 today")
    assert text == "Just a sale 🔥 today"
    assert entities == []


async def test_an_ad_can_carry_an_id_written_by_hand(client, actor, state, session):
    from app.adminbot import handlers
    from app.db.models import Broadcast
    from tests.integration.test_bot_flows import a_callback, a_message

    await connect_bot(actor)
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Sale"), user_id=uuid.UUID(actor.id), state=state)
    await handlers.ad_text(
        a_message("Big sale ![🔥](tg://emoji?id=5368324170671202286) today"),
        user_id=uuid.UUID(actor.id),
        state=state,
    )

    from sqlalchemy import select

    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    await session.refresh(broadcast)
    assert broadcast.body_text == "Big sale 🔥 today"
    assert broadcast.body_entities == [
        {
            "type": "custom_emoji",
            "offset": 9,
            "length": 2,
            "custom_emoji_id": "5368324170671202286",
        }
    ]


def test_a_typed_pair_is_read_as_an_icon():
    from app.adminbot.handlers import _typed_emoji_pairs

    assert _typed_emoji_pairs("🔥 5368324170671202286") == {"🔥": "5368324170671202286"}
    assert _typed_emoji_pairs("![✅](tg://emoji?id=777)") == {"✅": "777"}
    assert _typed_emoji_pairs("🔥 111\n✅ 5368324170671202286") == {"✅": "5368324170671202286"}, (
        "111 is too short to be a Telegram document id"
    )


def test_ordinary_numbers_in_text_are_not_read_as_ids():
    """An ad full of prices and years must not have them silently turned into
    emoji ids."""
    from app.adminbot.handlers import _typed_emoji_pairs

    assert _typed_emoji_pairs("Netflix 4K — 1 Month — $ 0.5") == {}
    assert _typed_emoji_pairs("2026") == {}
    _, entities = premium_icons.parse_entities("Gemini Pro 18 Months — only $ 0.5")
    assert entities == []


async def test_sending_an_id_to_the_collector_maps_it(client, actor, state, session):
    from app.adminbot import handlers
    from app.adminbot.states import IconSetup
    from app.repositories import panel_emoji as panel_emoji_repo
    from tests.integration.test_bot_flows import a_message

    await state.set_state(IconSetup.collect)
    await handlers.icon_collect(
        a_message("🔥 5368324170671202286"), user_id=uuid.UUID(actor.id), state=state
    )

    assert await panel_emoji_repo.get_map(session) == {"🔥": "5368324170671202286"}


# --------------------------------------------------------------------------- #
# A button icon is a field, not eighteen digits of label
# --------------------------------------------------------------------------- #
async def test_an_id_sent_while_renaming_becomes_the_icon(client, actor, state, session):
    """Pasted into the label it rendered as the digits of the id — which is
    what the operator saw and reported."""
    from app.adminbot import handlers
    from app.repositories import panel_buttons as panel_buttons_repo
    from tests.integration.test_bot_flows import a_callback, a_message

    index = views.RENAMEABLE_BUTTONS.index("📣 Ads")
    await handlers.op_buttons(
        a_callback(f"op:btn:pick:{index}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )
    await handlers.button_label(
        a_message("5368324170671202286 Ads"), user_id=uuid.UUID(actor.id), state=state
    )

    assert await panel_buttons_repo.get_map(session) == {"📣 Ads": "Ads"}
    assert await panel_buttons_repo.get_icons(session) == {"📣 Ads": "5368324170671202286"}

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📣 Ads", callback_data="nav:ads:0")]]
    )
    button = premium_icons.apply_labels(markup).inline_keyboard[0][0]
    assert button.text == "Ads", "digits do not belong in the words"
    assert button.icon_custom_emoji_id == "5368324170671202286"


async def test_an_icon_with_no_words_is_refused(client, actor, state, session):
    """Telegram requires text on a button; an id alone would produce one that
    cannot be sent at all."""
    from app.adminbot import handlers
    from app.repositories import panel_buttons as panel_buttons_repo
    from tests.integration.test_bot_flows import a_callback, a_message

    index = views.RENAMEABLE_BUTTONS.index("📣 Ads")
    await handlers.op_buttons(
        a_callback(f"op:btn:pick:{index}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )
    await handlers.button_label(
        a_message("5368324170671202286"), user_id=uuid.UUID(actor.id), state=state
    )

    assert await panel_buttons_repo.get_map(session) == {}
    assert "icon with no words" in Sent.last()


def test_an_explicit_button_icon_beats_the_automatic_one():
    """A choice made by hand for one button must not be overwritten by the
    pass that infers icons from leading emoji."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    premium_icons.set_map({"📣": "automatic"})
    premium_icons.set_labels({"📣 Ads": "Ads"}, {"📣 Ads": "chosen"})

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📣 Ads", callback_data="nav:ads:0")]]
    )
    final = premium_icons.apply_keyboard(premium_icons.apply_labels(markup))
    button = final.inline_keyboard[0][0]
    assert button.icon_custom_emoji_id == "chosen"
    assert button.text == "Ads"


# --------------------------------------------------------------------------- #
# Where the icons come from
# --------------------------------------------------------------------------- #
async def test_the_accounts_own_packs_are_preferred_over_searching(
    client, actor, session, monkeypatch
):
    """Searching returned 1 icon of 43 on a real account: it surfaces what
    Telegram suggests, while the packs are what the account actually has."""
    from app.adminbot import icon_setup

    connection = await _operator_account(session, actor, monkeypatch)
    script = script_for(connection.id)
    script.installed_emoji = {
        emoticon: f"pack-{i}" for i, emoticon in enumerate(views.panel_emoji())
    }
    script.custom_emoji = {"🔥": ["searched"]}

    found = await icon_setup.fetch_icons(await _adapter_for(session, connection))

    assert len(found) == len(views.panel_emoji())
    assert found["🔥"] == script.installed_emoji["🔥"], "owned beats suggested"
    assert script.calls_to("custom_emoji_ids") == [], "nothing left to search for"


async def test_search_still_covers_what_the_packs_miss(client, actor, session, monkeypatch):
    from app.adminbot import icon_setup

    connection = await _operator_account(session, actor, monkeypatch)
    script = script_for(connection.id)
    script.installed_emoji = {"🔥": "owned"}
    script.custom_emoji = {"✅": ["searched"]}

    found = await icon_setup.fetch_icons(await _adapter_for(session, connection))

    assert found["🔥"] == "owned"
    assert found["✅"] == "searched"


async def _adapter_for(session, connection):
    from app.services import connections as connection_service

    return await connection_service.adapter_for(session, connection)


# --------------------------------------------------------------------------- #
# Nothing is left plain
# --------------------------------------------------------------------------- #
def test_the_scanner_misses_no_block_any_screen_uses():
    """Checked against a second opinion, because the obvious test is vacuous.

    Asking "is every emoji in the source covered?" proves nothing when the
    covered set *is* the source. The real risk is the scanner's own regex
    missing a Unicode block — which happened: ▶ lives in Geometric Shapes,
    the first version did not list that block, and the Resume button sat plain
    among premium ones.

    So the oracle is ``unicodedata``: every character it calls a symbol must be
    one the scanner also found. That fails the moment a block is missing,
    whatever the source happens to contain.
    """
    import unicodedata

    from app.adminbot.emoji_scan import emoji_in
    from tests.integration.test_bot_flows import _every_screen

    missed: dict[str, str] = {}
    for name, screen in _every_screen():
        surface = screen.text + " ".join(
            b.text for row in screen.keyboard.inline_keyboard for b in row
        )
        by_scanner = set("".join(emoji_in(surface)))
        for char in surface:
            if unicodedata.category(char) == "So" and char not in by_scanner:
                missed[char] = f"{name} (U+{ord(char):04X})"

    assert not missed, f"the scanner does not know these are emoji: {missed}"


def test_every_rendered_emoji_reaches_the_extractor():
    """The set the extractor asks Telegram about is the set the screens draw."""
    from app.adminbot.emoji_scan import emoji_in
    from tests.integration.test_bot_flows import _every_screen

    covered = set(views.panel_emoji())
    for name, screen in _every_screen():
        surface = screen.text + " ".join(
            b.text for row in screen.keyboard.inline_keyboard for b in row
        )
        for emoji in emoji_in(surface):
            assert emoji in covered, f"{emoji} is drawn on {name} and never looked up"


def test_the_scan_reads_the_source_rather_than_a_list():
    """A list drifts; the source is the thing being described."""
    found = views.panel_emoji()
    assert len(found) > 40
    # A sample of icons from screens added at very different times.
    for emoji in ("📣", "🗄", "☑️", "▶️", "🧹", "🩺"):
        assert emoji in found, f"{emoji} is drawn somewhere and was not found"


def test_variation_selectors_are_kept_apart():
    """``⚠`` and ``⚠️`` are different strings and Telegram indexes them
    differently, so collapsing them would map the wrong one."""
    from app.adminbot.emoji_scan import emoji_in

    assert emoji_in("⚠️ warning") == ["⚠️"]
    assert emoji_in("⚠ warning") == ["⚠"]


def test_letterlike_symbols_are_not_mistaken_for_emoji():
    """``™`` sits in the same block as ``ℹ️``. Searching Telegram for a premium
    trademark sign is a wasted lookup and a confusing "not found" entry."""
    from app.adminbot.emoji_scan import emoji_in

    assert emoji_in("InsightAdFlow™ №1") == []
    assert emoji_in("ℹ️ About") == ["ℹ️"]


def test_the_screen_lists_what_has_no_premium_version():
    """The operator asked to be told which ones to do by hand — a list they can
    copy, not a count they have to work out."""
    wanted = ("✅", "📣", "🔥")
    screen = views.premium_icons_status(
        extracted={"✅": "1"},
        live=True,
        suspended=False,
        has_user_connection=True,
        wanted=wanted,
    )

    assert "No premium version found for 2" in screen.text
    assert "📣" in screen.text and "🔥" in screen.text
    assert "✅" not in screen.text.split("No premium version")[1], "the found one is not listed"
    assert_valid_markdown_v2(screen.text)


def test_the_screen_says_so_when_nothing_is_missing():
    screen = views.premium_icons_status(
        extracted={"✅": "1", "📣": "2"},
        live=True,
        suspended=False,
        has_user_connection=True,
        wanted=("✅", "📣"),
    )
    assert "Every icon has one" in screen.text
    assert "No premium version" not in screen.text
    assert_valid_markdown_v2(screen.text)


# --------------------------------------------------------------------------- #
# The library, and button colour
# --------------------------------------------------------------------------- #
def test_the_library_shows_every_id_in_a_copyable_form():
    """The ids are the point: an operator who wants one inside an ad needs the
    number itself, and hunting for it through Telegram is the work this saves."""
    first = views.panel_emoji()[0]
    screen = views.emoji_library(extracted={first: "5368324170671202286"}, page=0)

    assert "`5368324170671202286`" in screen.text, "monospace, so a tap copies it"
    assert first in screen.text
    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)


def test_the_library_lists_the_emoji_with_no_id_too():
    """The one an operator most wants to reach is the one extraction missed —
    and listing only the matched ones was the only way not to show it."""
    alphabet = views.panel_emoji()
    seen: set[str] = set()
    for page in range(len(alphabet)):
        screen = views.emoji_library(extracted={}, page=page)
        for row in screen.keyboard.inline_keyboard:
            for button in row:
                if (button.callback_data or "").startswith("op:emoji:one:"):
                    seen.add(button.callback_data.split(":", 4)[4])
        assert_valid_markdown_v2(screen.text)
        assert_keyboard_is_sendable(screen.keyboard)

    assert seen == set(alphabet), "every emoji the panel draws is reachable"


def test_an_untouched_library_still_offers_every_emoji():
    screen = views.emoji_library(extracted={}, page=0)
    data = [b.callback_data for row in screen.keyboard.inline_keyboard for b in row]

    assert "op:emoji:send" in data
    assert any((d or "").startswith("op:emoji:one:") for d in data)
    assert f"*plain* {len(views.panel_emoji())}" in screen.text
    assert_valid_markdown_v2(screen.text)


def test_the_screen_behind_a_library_entry_offers_both_ways():
    """Where an unmatched emoji is finally fixable without retyping the
    character beside a number."""
    one = views.panel_emoji()[0]

    plain = views.emoji_one(emoticon=one, custom_id=None, page=0)
    data = [b.callback_data for row in plain.keyboard.inline_keyboard for b in row]
    assert "Drawn plain" in plain.text
    assert f"op:emoji:ask:all:{one}" in data
    assert f"op:emoji:del:all:{one}" not in data, "nothing to clear yet"

    mapped = views.emoji_one(emoticon=one, custom_id="5368324170671202286", page=0)
    data = [b.callback_data for row in mapped.keyboard.inline_keyboard for b in row]
    assert "`5368324170671202286`" in mapped.text
    assert f"op:emoji:del:all:{one}" in data

    assert_valid_markdown_v2(plain.text)
    assert_valid_markdown_v2(mapped.text)
    assert_keyboard_is_sendable(mapped.keyboard)


async def test_a_button_can_be_given_one_of_telegrams_three_colours(client, actor, state, session):
    from app.adminbot import handlers
    from app.repositories import panel_buttons as panel_buttons_repo
    from tests.integration.test_bot_flows import a_callback

    index = views.RENAMEABLE_BUTTONS.index("📣 Ads")
    green = next(i for i, (_l, v) in enumerate(views.BUTTON_STYLES) if v == "success")

    await handlers.op_buttons(
        a_callback(f"op:btn:sty:{index}:{green}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )

    assert await panel_buttons_repo.get_styles(session) == {"📣 Ads": "success"}
    assert premium_icons.get_styles() == {"📣 Ads": "success"}


def test_the_colour_reaches_the_button():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    premium_icons.set_labels({}, {}, {"📣 Ads": "success"})
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📣 Ads", callback_data="nav:ads:0")]]
    )
    button = premium_icons.apply_labels(markup).inline_keyboard[0][0]

    assert button.style == "success"
    assert button.text == "📣 Ads", "colour alone changes nothing else"
    premium_icons.set_labels({})


async def test_a_colour_can_be_taken_back_off(client, actor, state, session):
    from app.adminbot import handlers
    from app.repositories import panel_buttons as panel_buttons_repo
    from tests.integration.test_bot_flows import a_callback

    index = views.RENAMEABLE_BUTTONS.index("📣 Ads")
    red = next(i for i, (_l, v) in enumerate(views.BUTTON_STYLES) if v == "danger")
    plain = next(i for i, (_l, v) in enumerate(views.BUTTON_STYLES) if v is None)

    for choice in (red, plain):
        await handlers.op_buttons(
            a_callback(f"op:btn:sty:{index}:{choice}"),
            user_id=uuid.UUID(actor.id),
            state=state,
            is_operator=True,
        )

    assert await panel_buttons_repo.get_styles(session) == {}, "back to Telegram's own"


async def test_colouring_a_button_does_not_rename_it(client, actor, state, session):
    """A colour needs a row of its own, and that row must not read as a rename.

    It carries the built-in label, so reporting it among the renames put
    ``🚀 Send now → 🚀 Send now`` on the buttons screen and counted it — a
    rename to the identical words, which is not one.
    """
    from app.adminbot import handlers
    from app.repositories import panel_buttons as panel_buttons_repo
    from tests.integration.test_bot_flows import a_callback

    index = views.RENAMEABLE_BUTTONS.index("🚀 Send now")
    blue = next(i for i, (_l, v) in enumerate(views.BUTTON_STYLES) if v == "primary")

    await handlers.op_buttons(
        a_callback(f"op:btn:sty:{index}:{blue}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )

    labels = await panel_buttons_repo.get_map(session)
    assert "🚀 Send now" not in labels, "a colour is not a rename"

    screen = views.panel_buttons_list(custom=labels, page=index // views.BUTTONS_PAGE_SIZE)
    assert "→" not in " ".join(b.text for row in screen.keyboard.inline_keyboard for b in row), (
        "and the screen shows no arrow"
    )


def test_the_picker_shows_each_choice_in_its_own_colour():
    """The only honest preview of a thing whose whole purpose is how it looks."""
    screen = views.button_style_picker(default_text="📣 Ads", index=0, current="success")

    styles = [b.style for row in screen.keyboard.inline_keyboard for b in row]
    assert "primary" in styles and "success" in styles and "danger" in styles
    assert "• 🟢 Green" in " ".join(
        b.text for row in screen.keyboard.inline_keyboard for b in row
    ), "and marks the current one"
    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)


async def test_a_colour_survives_a_restart(client, actor, state, session):
    """The whole point of the table: a redeploy must not fall back to plain."""
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback

    index = views.RENAMEABLE_BUTTONS.index("📣 Ads")
    green = next(i for i, (_l, v) in enumerate(views.BUTTON_STYLES) if v == "success")
    await handlers.op_buttons(
        a_callback(f"op:btn:sty:{index}:{green}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )

    # Everything in memory is lost, as it would be on a redeploy.
    premium_icons.set_labels({}, {}, {})
    assert premium_icons.get_styles() == {}

    await handlers._reload_button_look(session)
    assert premium_icons.get_styles() == {"📣 Ads": "success"}


# --------------------------------------------------------------------------- #
# Setting one by hand — the emoji extraction could not match
# --------------------------------------------------------------------------- #
async def test_an_id_sent_for_one_emoji_maps_only_that_one(client, actor, state, session):
    """The route that needs no keyboard and no pack: pick the emoji on screen,
    send the digits. Extraction leaves some unmatched every time, and before
    this the only fix was retyping the character beside the number — which is
    exactly the character an operator does not have to hand."""
    from app.adminbot import handlers
    from app.adminbot.states import IconSetup
    from app.repositories import panel_emoji as panel_emoji_repo
    from tests.integration.test_bot_flows import a_callback, a_message

    one = views.panel_emoji()[0]
    await handlers.op_emoji(
        a_callback(f"op:emoji:ask:all:{one}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )
    assert await state.get_state() == IconSetup.one.state

    await handlers.icon_one(
        a_message("5368324170671202286"), user_id=uuid.UUID(actor.id), state=state
    )

    assert await panel_emoji_repo.get_map(session) == {one: "5368324170671202286"}
    assert premium_icons.get_map() == {one: "5368324170671202286"}, "and live at once"


async def test_setting_one_emoji_leaves_the_others_alone(client, actor, state, session):
    """``replace`` swaps the whole table, so a single edit has to merge first —
    getting this wrong would wipe every other icon on each hand-set one."""
    from app.adminbot import handlers
    from app.repositories import panel_emoji as panel_emoji_repo
    from tests.integration.test_bot_flows import a_callback, a_message

    first, second = views.panel_emoji()[0], views.panel_emoji()[1]
    await panel_emoji_repo.replace(session, mapping={second: "111111111111111111"})
    await session.commit()

    await handlers.op_emoji(
        a_callback(f"op:emoji:ask:all:{first}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )
    await handlers.icon_one(
        a_message("5368324170671202286"), user_id=uuid.UUID(actor.id), state=state
    )

    assert await panel_emoji_repo.get_map(session) == {
        second: "111111111111111111",
        first: "5368324170671202286",
    }


async def test_one_emoji_can_be_put_back_to_plain(client, actor, state, session):
    from app.adminbot import handlers
    from app.repositories import panel_emoji as panel_emoji_repo
    from tests.integration.test_bot_flows import a_callback

    one = views.panel_emoji()[0]
    await panel_emoji_repo.replace(session, mapping={one: "5368324170671202286"})
    await session.commit()

    await handlers.op_emoji(
        a_callback(f"op:emoji:del:all:{one}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )

    assert await panel_emoji_repo.get_map(session) == {}
    assert premium_icons.get_map() == {}


async def test_an_emoji_the_panel_does_not_draw_is_refused(client, actor, state, session):
    """Callback data is not trustworthy input. An id set for a character no
    screen contains would sit in the table forever, matching nothing."""
    from app.adminbot import handlers
    from app.adminbot.states import IconSetup
    from tests.integration.test_bot_flows import a_callback

    await handlers.op_emoji(
        a_callback("op:emoji:ask:all:🦄"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )

    assert await state.get_state() != IconSetup.one.state


# --------------------------------------------------------------------------- #
# An icon pinned to one button
# --------------------------------------------------------------------------- #
async def test_a_button_icon_can_be_pinned_from_its_own_screen(client, actor, state, session):
    """Beside the colour, and reachable without renaming anything: the icon is
    a field of its own, so asking for the label again to change it was work
    with no purpose."""
    from app.adminbot import handlers
    from app.adminbot.states import EditButton
    from app.repositories import panel_buttons as panel_buttons_repo
    from tests.integration.test_bot_flows import a_callback, a_message

    index = views.RENAMEABLE_BUTTONS.index("📣 Ads")
    await handlers.op_buttons(
        a_callback(f"op:btn:ask:{index}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )
    assert await state.get_state() == EditButton.icon.state

    await handlers.button_icon(
        a_message("5368324170671202286"), user_id=uuid.UUID(actor.id), state=state
    )

    assert await panel_buttons_repo.get_icons(session) == {"📣 Ads": "5368324170671202286"}
    assert await panel_buttons_repo.get_map(session) == {}, "pinning an icon is not a rename"


async def test_a_pinned_icon_takes_the_plain_emoji_out_of_the_label(client, actor, state, session):
    """The icon is drawn *before* the words. A label still opening with the
    plain character shows the same picture twice, side by side."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.adminbot import handlers
    from app.repositories import panel_buttons as panel_buttons_repo
    from tests.integration.test_bot_flows import a_callback, a_message

    index = views.RENAMEABLE_BUTTONS.index("📣 Ads")
    await handlers.op_buttons(
        a_callback(f"op:btn:ask:{index}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )
    await handlers.button_icon(
        a_message("5368324170671202286"), user_id=uuid.UUID(actor.id), state=state
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📣 Ads", callback_data="nav:ads:0")]]
    )
    button = premium_icons.apply_labels(markup).inline_keyboard[0][0]
    assert button.icon_custom_emoji_id == "5368324170671202286"
    assert button.text == "Ads", "not '📣 Ads' beside the same picture"
    assert await panel_buttons_repo.get_map(session) == {}, "and no rename was needed"


async def test_a_pinned_icon_can_be_handed_back_to_the_automatic_one(client, actor, state, session):
    from app.adminbot import handlers
    from app.repositories import panel_buttons as panel_buttons_repo
    from tests.integration.test_bot_flows import a_callback, a_message

    index = views.RENAMEABLE_BUTTONS.index("📣 Ads")
    await handlers.op_buttons(
        a_callback(f"op:btn:ask:{index}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )
    await handlers.button_icon(
        a_message("5368324170671202286"), user_id=uuid.UUID(actor.id), state=state
    )
    await handlers.op_buttons(
        a_callback(f"op:btn:auto:{index}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )

    assert await panel_buttons_repo.get_icons(session) == {}
    assert premium_icons.get_button_icons() == {}


async def test_a_pinned_icon_survives_a_restart(client, actor, state, session):
    """The same promise as the colour: a redeploy must not fall back to plain."""
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback, a_message

    index = views.RENAMEABLE_BUTTONS.index("📣 Ads")
    await handlers.op_buttons(
        a_callback(f"op:btn:ask:{index}"),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )
    await handlers.button_icon(
        a_message("5368324170671202286"), user_id=uuid.UUID(actor.id), state=state
    )

    premium_icons.set_labels({}, {}, {})
    assert premium_icons.get_button_icons() == {}

    await handlers._reload_button_look(session)
    assert premium_icons.get_button_icons() == {"📣 Ads": "5368324170671202286"}


async def test_resetting_a_label_keeps_the_colour_and_the_icon(client, actor, state, session):
    """Deleting the row was the obvious way and it threw away two other
    settings: red button, reset the label, and the red went with it."""
    from app.adminbot import handlers
    from app.repositories import panel_buttons as panel_buttons_repo
    from tests.integration.test_bot_flows import a_callback, a_message

    index = views.RENAMEABLE_BUTTONS.index("🚫 Stop")
    red = next(i for i, (_l, v) in enumerate(views.BUTTON_STYLES) if v == "danger")
    actor_id = uuid.UUID(actor.id)

    await handlers.op_buttons(
        a_callback(f"op:btn:sty:{index}:{red}"), user_id=actor_id, state=state, is_operator=True
    )
    await handlers.op_buttons(
        a_callback(f"op:btn:ask:{index}"), user_id=actor_id, state=state, is_operator=True
    )
    await handlers.button_icon(a_message("5368324170671202286"), user_id=actor_id, state=state)
    await handlers.op_buttons(
        a_callback(f"op:btn:pick:{index}"), user_id=actor_id, state=state, is_operator=True
    )
    await handlers.button_label(a_message("-"), user_id=actor_id, state=state)

    assert await panel_buttons_repo.get_map(session) == {}, "back to the built-in words"
    assert await panel_buttons_repo.get_styles(session) == {"🚫 Stop": "danger"}, "still red"
    assert await panel_buttons_repo.get_icons(session) == {"🚫 Stop": "5368324170671202286"}


def test_the_icon_screen_names_the_automatic_one_it_would_replace():
    """So an operator can see what they are overriding before they override
    it — the automatic icon is right for almost every button."""
    inherited = views.button_icon_picker(
        default_text="📣 Ads", index=0, current=None, inherited="111111111111111111"
    )
    assert "Automatic" in inherited.text and "`111111111111111111`" in inherited.text

    pinned = views.button_icon_picker(
        default_text="📣 Ads", index=0, current="222222222222222222", inherited="111111111111111111"
    )
    assert "Pinned" in pinned.text and "`222222222222222222`" in pinned.text
    assert "op:btn:auto:0" in [
        b.callback_data for row in pinned.keyboard.inline_keyboard for b in row
    ]

    assert_valid_markdown_v2(inherited.text)
    assert_valid_markdown_v2(pinned.text)
    assert_keyboard_is_sendable(pinned.keyboard)


# --------------------------------------------------------------------------- #
# End to end — every message, not only the screens
# --------------------------------------------------------------------------- #
def test_the_scan_covers_every_module_that_speaks_to_a_person():
    """A screen is not the only thing this bot sends. An alert and the warning
    before a login code are messages too, and the one that arrives *unasked*
    is a poor place to be the only plain icon left."""
    import inspect

    from app.adminbot import notifier, secrets
    from app.adminbot.emoji_scan import emoji_in
    from app.services import archive

    covered = set(views.panel_emoji())
    for module in (notifier, secrets, archive):
        for emoticon in emoji_in(inspect.getsource(module)):
            assert emoticon in covered, f"{module.__name__} draws {emoticon}, unreachable"


async def _an_alert_for(session, actor, title: str):
    """One queued alert, addressed to a user Telegram can actually reach."""
    from app.db.models import User
    from app.repositories import admins as admin_repo

    user = await session.get(User, uuid.UUID(actor.id))
    user.telegram_user_id = ADMIN_CHAT
    await admin_repo.notify(
        session,
        user_id=uuid.UUID(actor.id),
        kind="rule_paused",
        title=title,
        body="Telegram asked for a long wait.",
        dedupe_key=f"premium-icons-{title}",
    )
    await session.commit()


async def test_an_alert_carries_premium_icons_like_every_screen(client, actor, session):
    """The notifier sent straight to the Bot API, around the transform every
    other message goes through — so an operator with premium icons everywhere
    still got a plain warning on the one message they did not ask for."""
    from app.adminbot import notifier

    premium_icons.set_map({"⚠️": "999000333"})
    await _an_alert_for(session, actor, "Rule paused")

    sent: list[str] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **_kw):
            sent.append(text)

    delivered = await notifier.drain_once(FakeBot())

    assert delivered == 1
    assert "tg://emoji?id=999000333" in sent[0], "the same upgrade as a screen"
    assert_valid_markdown_v2(sent[0])


async def test_an_alert_falls_back_to_plain_when_telegram_refuses(client, actor, session):
    """Same bargain as the panel: a degraded icon is a shrug, a lost alert is
    the operator not hearing about a paused rule."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    from app.adminbot import notifier

    premium_icons.set_map({"⚠️": "999000333"})
    await _an_alert_for(session, actor, "Rule paused")

    sent: list[str] = []

    class RefusingBot:
        async def send_message(self, chat_id, text, **_kw):
            sent.append(text)
            if "tg://emoji" in text:
                raise TelegramBadRequest(
                    method=SendMessage(chat_id=1, text=""),
                    message="Bad Request: can't parse entities: custom emoji",
                )

    delivered = await notifier.drain_once(RefusingBot())

    assert delivered == 1, "the alert still arrives"
    assert "tg://emoji" not in sent[-1]
    assert "Rule paused" in sent[-1]


def test_the_library_can_be_read_as_messages_or_as_buttons():
    """An operator thinks of them separately: the icons on the buttons are one
    job, the ticks and crosses in what it says back is another."""
    from app.adminbot.emoji_scan import BUTTON, TEXT, emoji_places

    places = emoji_places()
    everywhere = views.library_alphabet("all")
    in_text = views.library_alphabet("text")
    on_buttons = views.library_alphabet("btn")

    assert set(in_text) | set(on_buttons) == set(everywhere), "no emoji belongs to neither"
    assert in_text and on_buttons and set(in_text) != set(on_buttons)
    for emoticon in in_text:
        assert TEXT in places[emoticon]
    for emoticon in on_buttons:
        assert BUTTON in places[emoticon]


def test_a_renameable_buttons_emoji_is_known_to_be_a_button():
    """Those labels become button text without passing through an
    ``InlineKeyboardButton(...)`` call, so a scan of the call sites alone files
    every one of them as message text."""
    from app.adminbot.emoji_scan import BUTTON, leading_emoji

    on_buttons = set(views.library_alphabet("btn"))
    for label in views.RENAMEABLE_BUTTONS:
        lead = leading_emoji(label)
        assert lead in on_buttons, f"{label} is a button, {lead} was not filed as one"

    for label, _style in views.BUTTON_STYLES:
        assert leading_emoji(label) in on_buttons
    assert BUTTON  # the constant the classification is written in terms of


def test_a_comment_cannot_claim_an_emoji_is_on_a_screen():
    """``panel_emoji`` reads the raw text on purpose — generous is the right
    side to err on. The *places* map must not be, or a worked example in a
    docstring would tell an operator their buttons carry an icon they do not."""
    from app.adminbot.emoji_scan import emoji_in, emoji_places

    source = "# a comment with 🦄 in it\nBUTTON = '🚀 Go'\n"
    assert "🦄" in emoji_in(source), "the raw scan sees it"

    places = emoji_places()
    assert "🦄" not in places
