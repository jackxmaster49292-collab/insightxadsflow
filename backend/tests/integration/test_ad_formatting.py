"""An ad is posted exactly as it was written.

Bold, links and premium emoji are "entities" in Telegram's vocabulary: offsets
into the text rather than markup inside it. Storing only the text loses all
three, which is what turned a formatted ad with custom emoji into plain text
with fallback emoji.

Also here: an ad goes to groups, and not to a private chat or a channel.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.adapters.base import TextEntity
from app.db.models import Broadcast, BroadcastMedia, BroadcastStatus
from app.services import broadcast as broadcast_service
from tests.conftest import (
    connect_bot,
    discovered,
    fake_broadcast,
    script_for,
    sync_with_chats,
)
from tests.integration.test_broadcast import build_broadcast, drain

#: A realistic ad: a bold headline, a premium emoji, and a link.
AD_ENTITIES = [
    {"type": "bold", "offset": 0, "length": 13},
    {"type": "custom_emoji", "offset": 14, "length": 2, "custom_emoji_id": "5368324170671202286"},
    {"type": "text_link", "offset": 20, "length": 8, "url": "https://t.me/insightXpro_bot"},
]


# --------------------------------------------------------------------------- #
# Round-tripping the formatting
# --------------------------------------------------------------------------- #
async def test_the_formatting_reaches_every_group(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=3, text="INSIGHT STORE 🔥 order now")
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.body_entities = AD_ENTITIES
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)

    sends = script_for(ctx["connection_id"]).calls_to("send_text")
    assert len(sends) == 3
    for call in sends:
        entities = call.kwargs["entities"]
        assert [e.type for e in entities] == ["bold", "custom_emoji", "text_link"]


async def test_a_premium_emoji_keeps_its_identifier(client, actor, session):
    """Without the id Telegram renders a fallback emoji, which is exactly what
    the customer saw when only the text was stored."""
    ctx = await build_broadcast(actor, session, groups=1, text="INSIGHT STORE 🔥 order now")
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.body_entities = AD_ENTITIES
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)

    entities = script_for(ctx["connection_id"]).calls_to("send_text")[0].kwargs["entities"]
    emoji = next(e for e in entities if e.type == "custom_emoji")
    assert emoji.custom_emoji_id == "5368324170671202286"


async def test_a_link_keeps_its_url(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=1, text="INSIGHT STORE 🔥 order now")
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.body_entities = AD_ENTITIES
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)

    entities = script_for(ctx["connection_id"]).calls_to("send_text")[0].kwargs["entities"]
    link = next(e for e in entities if e.type == "text_link")
    assert link.url == "https://t.me/insightXpro_bot"


async def test_an_image_caption_keeps_its_formatting(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=1, text="INSIGHT STORE 🔥 order now")
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.body_entities = AD_ENTITIES
    broadcast.media_kind = BroadcastMedia.photo
    broadcast.media_bytes = b"\xff\xd8\xffjpeg"
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)

    photo = script_for(ctx["connection_id"]).calls_to("send_photo")[0]
    assert [e.type for e in photo.kwargs["caption_entities"]] == [
        "bold",
        "custom_emoji",
        "text_link",
    ]


async def test_an_ad_with_no_formatting_sends_none(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=1, text="Just plain text")
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)

    assert script_for(ctx["connection_id"]).calls_to("send_text")[0].kwargs["entities"] == []


# --------------------------------------------------------------------------- #
# The entity shape itself
# --------------------------------------------------------------------------- #
def test_an_entity_survives_the_database_round_trip():
    original = TextEntity(
        type="custom_emoji", offset=14, length=2, custom_emoji_id="5368324170671202286"
    )
    assert TextEntity.from_json(original.as_json()) == original


def test_only_the_fields_that_are_set_are_stored():
    """A row full of nulls is noise, and Telegram ignores them anyway."""
    assert TextEntity(type="bold", offset=0, length=4).as_json() == {
        "type": "bold",
        "offset": 0,
        "length": 4,
    }


def test_offsets_are_passed_through_untouched():
    """Both the Bot API and MTProto count UTF-16 code units. Recomputing them in
    Python's code points would shift every entity after an emoji."""
    from app.adapters.bot import _to_bot_entities
    from app.adapters.user import _to_mtproto_entities

    entity = TextEntity(type="bold", offset=14, length=2)

    assert _to_bot_entities([entity])[0].offset == 14
    assert _to_mtproto_entities([entity])[0].offset == 14


def test_an_unknown_entity_type_is_dropped_rather_than_guessed():
    """Losing one piece of formatting beats Telegram rejecting the message and
    the ad never being posted."""
    from app.adapters.user import _to_mtproto_entities

    built = _to_mtproto_entities(
        [
            TextEntity(type="bold", offset=0, length=4),
            TextEntity(type="something_new_in_a_later_bot_api", offset=5, length=3),
        ]
    )
    assert len(built) == 1


def test_every_entity_type_the_bot_api_documents_is_handled():
    """A type we silently drop is formatting the customer typed and did not get.

    Pinned against the Bot API's documented list so a gap is a test failure
    rather than something noticed in a posted ad.
    """
    from app.adapters.user import _MTPROTO_ENTITIES, _to_mtproto_entities

    documented = {
        "bold",
        "italic",
        "underline",
        "strikethrough",
        "spoiler",
        "code",
        "pre",
        "blockquote",
        "url",
        "text_link",
        "email",
        "phone_number",
        "mention",
        "hashtag",
        "cashtag",
        "bot_command",
        "custom_emoji",
    }
    handled = set(_MTPROTO_ENTITIES) | {"custom_emoji", "text_link", "pre"}
    missing = documented - handled
    assert not missing, f"these would be silently dropped from an ad: {sorted(missing)}"

    # And each one actually builds something.
    for name in sorted(documented):
        entity = TextEntity(
            type=name,
            offset=0,
            length=2,
            url="https://example.com" if name == "text_link" else None,
            custom_emoji_id="123" if name == "custom_emoji" else None,
        )
        assert _to_mtproto_entities([entity]), f"{name} produced nothing"


def test_a_premium_emoji_rejection_says_what_to_do():
    """Telegram only lets a Premium account send custom emoji, and the message
    should name that rather than reading as a generic failure."""
    from app.adapters.errors import classify_error
    from app.domain import reasons

    exc = type("PremiumAccountRequiredError", (Exception,), {})()
    classified = classify_error(exc)

    assert classified.code == reasons.PREMIUM_EMOJI_REQUIRED
    assert not classified.retryable, "the account will not become premium by retrying"

    text = reasons.describe(reasons.PREMIUM_EMOJI_REQUIRED)
    assert "Telegram Premium" in text
    assert "ordinary emoji" in text


# --------------------------------------------------------------------------- #
# Where an ad may go
# --------------------------------------------------------------------------- #
async def test_the_ad_picker_offers_groups_only(client, actor, session):
    """A private chat is never a destination — an ad in someone's DM is
    unsolicited messaging — and a channel is excluded as a product choice."""
    from app.adminbot.handlers import _postable_chats

    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor,
        connection_id,
        [
            discovered(-1002000, "A group", chat_kind="supergroup"),
            discovered(-1002001, "A basic group", chat_kind="group"),
            discovered(-1002002, "A channel", chat_kind="channel"),
            discovered(500100, "A person", chat_kind="private"),
        ],
    )

    for_ads = await _postable_chats(session, uuid.UUID(actor.id), groups_only=True)
    assert {c.title for c in for_ads} == {"A group", "A basic group"}

    for_rules = await _postable_chats(session, uuid.UUID(actor.id))
    assert "A channel" in {c.title for c in for_rules}, "forwarding may target a channel"
    assert "A person" not in {c.title for c in for_rules}, "never a private chat"


async def test_a_private_chat_is_never_a_destination(client, actor, session):
    """The safety property, checked at the layer that decides it rather than
    only at the screen that hides it."""
    from app.db.models import ConnectionChatAccess, TelegramChat

    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor, connection_id, [discovered(500100, "A person", chat_kind="private")]
    )

    chat = (
        await session.execute(select(TelegramChat).where(TelegramChat.title == "A person"))
    ).scalar_one()
    access = await session.get(ConnectionChatAccess, chat.id)
    assert not access.can_post_destination
    assert access.destination_reason_code == "not_a_destination_type"


async def test_the_groups_screen_accounts_for_what_it_is_not_showing(client, actor):
    """719 synced chats and 40 on screen looks like a bug unless the gap is
    explained."""
    from app.adminbot import views

    screen = views.chats_list(
        chats=[
            type(
                "C",
                (),
                {
                    "id": uuid.uuid4(),
                    "title": "A group",
                    "access": type(
                        "A",
                        (),
                        {
                            "can_post_destination": True,
                            "can_read_source": True,
                            "destination_reason_code": "ok",
                        },
                    )(),
                },
            )()
        ],
        page=0,
        other_count=679,
    )
    assert "679 private chats and channels" in screen.text
    assert "an ad never posts to" in screen.text


# --------------------------------------------------------------------------- #
# Capturing what the customer actually typed
# --------------------------------------------------------------------------- #
async def test_composing_an_ad_stores_the_formatting_telegram_reported(client, actor, session):
    """The half that broke: the handler kept `message.text` and dropped
    `message.entities`, so an ad written with bold and premium emoji was stored
    as plain text and posted that way."""
    from datetime import UTC, datetime
    from typing import Any

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import CallbackQuery, Chat, Message, MessageEntity
    from aiogram.types import User as TgUser

    from app.adminbot import handlers

    CHAT = 900_100_200

    class Quiet(Message):
        async def answer(self, *_a: Any, **_kw: Any) -> Any:
            return self

        async def edit_text(self, *_a: Any, **_kw: Any) -> Any:
            return self

    class QuietCallback(CallbackQuery):
        async def answer(self, *_a: Any, **_kw: Any) -> Any:
            return True

    def message(text: str, entities: list[MessageEntity] | None = None) -> Quiet:
        return Quiet(
            message_id=1,
            date=datetime(2026, 1, 1, tzinfo=UTC),
            chat=Chat(id=CHAT, type="private"),
            from_user=TgUser(id=CHAT, is_bot=False, first_name="Op"),
            text=text,
            entities=entities,
        )

    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CHAT, user_id=CHAT)
    )
    user_id = uuid.UUID(actor.id)
    await connect_bot(actor)

    await handlers.ad_new(
        QuietCallback(
            id="1",
            from_user=TgUser(id=CHAT, is_bot=False, first_name="Op"),
            chat_instance="ci",
            data="ad:new",
            message=message(""),
        ),
        state=state,
    )
    await handlers.ad_name(message("Sale"), user_id=user_id, state=state)

    await handlers.ad_text(
        message(
            "INSIGHT STORE 🔥 order now",
            [
                MessageEntity(type="bold", offset=0, length=13),
                MessageEntity(
                    type="custom_emoji",
                    offset=14,
                    length=2,
                    custom_emoji_id="5368324170671202286",
                ),
            ],
        ),
        user_id=user_id,
        state=state,
    )

    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    assert broadcast.body_text == "INSIGHT STORE 🔥 order now"
    assert broadcast.body_entities == [
        {"type": "bold", "offset": 0, "length": 13},
        {
            "type": "custom_emoji",
            "offset": 14,
            "length": 2,
            "custom_emoji_id": "5368324170671202286",
        },
    ]


# --------------------------------------------------------------------------- #
# Premium emoji need a premium account, and the panel says so first
# --------------------------------------------------------------------------- #
def test_an_unchecked_account_is_not_reported_as_non_premium():
    """A stored default is not a finding. Reporting one as "not Premium" is
    exactly how a Premium account came to be told it was not."""
    from app.adminbot import views

    text = "\n".join(
        views.premium_emoji_warning(has_premium_emoji=True, account_is_premium=False, checked=False)
    )
    assert "not Telegram Premium" not in text
    assert "have not checked yet" in text
    assert "Check health" in text, "it must say how to find out"


def test_a_non_premium_account_is_warned_before_sending():
    """Custom emoji arrive as ordinary ones without Telegram Premium. Finding
    that out from 157 posted ads is the wrong way to learn it."""
    from app.adminbot import views

    warning = views.premium_emoji_warning(
        has_premium_emoji=True, account_is_premium=False, checked=True
    )
    text = "\n".join(warning)

    assert "not Telegram Premium" in text
    assert "arrive as ordinary emoji" in text
    assert "everything else posts exactly as written" in text, (
        "the rest of the formatting does work, and saying so avoids a false alarm"
    )


def test_a_premium_account_is_not_warned():
    from app.adminbot import views

    assert (
        views.premium_emoji_warning(has_premium_emoji=True, account_is_premium=True, checked=True)
        == []
    )


def test_an_ad_without_premium_emoji_is_not_warned():
    from app.adminbot import views

    assert views.premium_emoji_warning(has_premium_emoji=False, account_is_premium=False) == []


def test_the_compose_screen_explains_why_its_own_preview_looks_plain():
    """The preview is escaped plain text, and this bot could not render a custom
    emoji even if it tried — the Bot API reserves those for bots with a Fragment
    username. Without saying so, the preview reads as the result."""
    from app.adminbot import views

    broadcast = fake_broadcast(
        name="Sale",
        body_text="INSIGHT STORE",
        body_entities=[{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "1"}],
    )
    screen = views.ad_compose(
        broadcast=broadcast, target_count=1, estimate_s=0, account_is_premium=True
    )
    assert "Formatting kept" in screen.text
    assert "1 premium emoji" in screen.text
    assert "preview above is plain text" in screen.text
    assert "The posted ad does" in screen.text


def test_the_formatting_summary_names_what_was_captured():
    """The preview cannot show formatting — it is plain text, and a bot may not
    render a custom emoji at all — so this is the only confirmation available
    without posting an ad and looking at it."""
    from app.adminbot import views

    summary = views.formatting_summary(
        [
            {"type": "bold", "offset": 0, "length": 5},
            {"type": "italic", "offset": 6, "length": 3},
            *[
                {"type": "custom_emoji", "offset": i, "length": 2, "custom_emoji_id": "1"}
                for i in range(17)
            ],
            {"type": "text_link", "offset": 99, "length": 4, "url": "https://x"},
        ]
    )
    assert "17 premium emoji" in summary
    assert "bold" in summary and "italic" in summary
    assert "1 link" in summary


def test_an_ad_with_no_formatting_has_no_summary():
    from app.adminbot import views

    assert views.formatting_summary([]) == ""


async def test_the_premium_flag_is_read_from_telegram(client, actor, session):
    from app.db.models import TelegramConnection
    from tests.conftest import script_for as script

    connection_id = await connect_bot(actor)
    script(connection_id).premium = True
    await session.commit()

    from app.services import connections as connection_service

    connection = await session.get(TelegramConnection, uuid.UUID(connection_id))
    await connection_service.run_health_check(session, connection=connection)

    assert connection.is_premium is True
    assert connection.premium_checked_at is not None, "and we know when we asked"


# --------------------------------------------------------------------------- #
# Select all
# --------------------------------------------------------------------------- #
def test_the_picker_offers_select_all_not_just_the_page():
    """157 groups over 20 pages made "Select page" twenty taps."""
    from types import SimpleNamespace

    from app.adminbot import views

    chats = [SimpleNamespace(id=uuid.uuid4(), title=f"Group {i:03d}") for i in range(157)]
    screen = views.group_picker(
        chats=chats,
        selected=set(),
        page=0,
        title="Choose groups",
        hint="Tap to select.",
        done_callback="ad:x",
    )
    labels = [b.text for row in screen.keyboard.inline_keyboard for b in row]
    callbacks = [b.callback_data for row in screen.keyboard.inline_keyboard for b in row]

    assert "✅ Select all 157" in labels, "the count belongs on the button"
    assert f"{views.PICK}A" in callbacks
    assert "Select page" in labels, "still useful when you want one page"


async def test_select_all_selects_every_page(client, actor, session):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.adminbot import handlers, views
    from tests.integration.test_bot_flows import ADMIN_CHAT, a_callback, a_message

    user_id = uuid.UUID(actor.id)
    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor,
        connection_id,
        [discovered(-1002000 - i, f"Group {i:03d}", chat_kind="supergroup") for i in range(20)],
    )

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_CHAT, user_id=ADMIN_CHAT),
    )
    await handlers.ad_new(a_callback("ad:new"), state=state)
    await handlers.ad_name(a_message("Wide"), user_id=user_id, state=state)
    await handlers.ad_text(a_message("Hello"), user_id=user_id, state=state)

    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    await handlers.ad_actions(a_callback(f"ad:{broadcast.id}:pick"), user_id=user_id, state=state)
    await handlers.picker_actions(a_callback(f"{views.PICK}A"), user_id=user_id, state=state)

    data = await state.get_data()
    assert len(data["selected"]) == 20, "every page, not just the visible eight"


# --------------------------------------------------------------------------- #
# Telethon must never re-parse the text
# --------------------------------------------------------------------------- #
def test_an_empty_entity_list_stays_a_list():
    """Telethon reads ``formatting_entities is None`` as "parse this as
    Markdown". An ad with no formatting but a literal asterisk would have had
    it eaten as markup — and an ad written as `*not bold*` would have arrived
    bold."""
    import inspect

    from app.adapters import user

    for name in ("send_text", "send_photo"):
        source = inspect.getsource(getattr(user.UserAdapter, name))
        entity_line = next(line for line in source.splitlines() if "formatting_entities=" in line)
        assert "or None" not in entity_line, (
            f"{name} collapses an empty entity list to None, which makes "
            f"Telethon parse the text as Markdown: {entity_line.strip()}"
        )
        assert "parse_mode=None" in source, f"{name} must disable parsing outright"


def test_an_ad_with_no_formatting_still_disables_parsing(client, actor):
    """The property behind the check above, stated as behaviour."""
    from app.adapters.user import _to_mtproto_entities

    assert _to_mtproto_entities([]) == [], "an empty list, not None"


def test_the_compose_screen_says_when_nothing_was_captured():
    """An absent line reads as "not applicable". A line saying none is what
    answers "why are my premium emoji missing?"."""
    from app.adminbot import views

    broadcast = fake_broadcast(name="Old draft", body_text="INSIGHT STORE")
    screen = views.ad_compose(broadcast=broadcast, target_count=1, estimate_s=0)

    assert "*Formatting kept* — none" in screen.text
    assert "tap *Message* and send it again" in screen.text


def test_the_truncation_note_stays_with_the_message_it_truncates():
    """It sat after the formatting line, where "…and 199 more characters" read
    as though the 199 characters were formatting."""
    from app.adminbot import views

    broadcast = fake_broadcast(
        name="Long",
        body_text="x" * (views.PREVIEW_CHARS + 200),
        body_entities=[{"type": "bold", "offset": 0, "length": 4}],
    )
    text = views.ad_compose(broadcast=broadcast, target_count=1, estimate_s=0).text

    assert text.index("more characters") < text.index("Formatting kept")


# --------------------------------------------------------------------------- #
# Showing the ad back
# --------------------------------------------------------------------------- #
def test_a_normal_ad_is_shown_whole():
    """The old 400-character clip hid the end of nearly every real ad."""
    from app.adminbot import views

    body = "Our October offer is live. " * 40  # ~1080 characters
    screen = views.ad_compose(
        broadcast=fake_broadcast(body_text=body), target_count=1, estimate_s=0
    )
    assert "more characters" not in screen.text
    assert views.escape(body.strip()) in screen.text


def test_a_clip_is_measured_after_escaping():
    """Escaping nearly doubles a body of punctuation. Budgeting on the raw
    length is how a long ad becomes a 400 and a blank screen instead."""
    from app.adminbot import views

    shown, clipped = views.preview("." * 4000)
    assert len(shown) <= views.PREVIEW_CHARS
    assert clipped == 4000 - len(shown) // 2


def test_a_clip_never_leaves_a_dangling_backslash():
    """A cut landing between a backslash and the character it escapes is itself
    a 400 — the same class of failure as an unescaped underscore."""
    from app.adminbot import views
    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    for limit in range(1, 60):
        shown, _ = views.preview("a.b-c!d(e)f", limit=limit)
        assert not shown.endswith("\\"), f"limit {limit} split an escape pair"
        assert_valid_markdown_v2(shown)


def test_the_longest_possible_ad_still_fits_a_telegram_message():
    """Both screens render an ad at Telegram's own maximum without exceeding
    the 4096 the reply itself is capped at."""
    from app.adminbot import views
    from app.config import get_settings
    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    body = ".!-()" * (get_settings().max_broadcast_text_len // 5)
    broadcast = fake_broadcast(body_text=body, status=BroadcastStatus.sending, repeat_every_s=7200)

    for screen in (
        views.ad_compose(broadcast=broadcast, target_count=500, estimate_s=1500),
        views.ad_detail(
            broadcast=broadcast,
            counts={"succeeded": 400, "skipped": 100},
            target_count=500,
        ),
    ):
        assert len(screen.text) <= 4096, f"{len(screen.text)} characters is a 400 from Telegram"
        assert_valid_markdown_v2(screen.text)


# --------------------------------------------------------------------------- #
# Repeating, on screen
# --------------------------------------------------------------------------- #
def test_the_compose_screen_states_the_repeat_either_way():
    """Silence would read as "no repeat" to one customer and "every hour" to
    another. Both cases say which."""
    from app.adminbot import views

    once = views.ad_compose(broadcast=fake_broadcast(), target_count=1, estimate_s=0)
    assert "once, then stop" in once.text

    repeating = views.ad_compose(
        broadcast=fake_broadcast(repeat_every_s=21_600), target_count=1, estimate_s=0
    )
    assert "every 6" in repeating.text


def test_a_running_repeat_shows_the_rounds_and_the_next_one():
    from datetime import UTC, datetime, timedelta

    from app.adminbot import views

    broadcast = fake_broadcast(
        status=BroadcastStatus.sending,
        repeat_every_s=7200,
        repeat_count=3,
        next_run_at=datetime.now(UTC) + timedelta(hours=1, minutes=58),
    )
    text = views.ad_detail(broadcast=broadcast, counts={"succeeded": 5}, target_count=5).text

    assert "Rounds sent* — 3" in text
    assert "Next round" in text
    assert "2\\.0h" in text or "in about" in text


def test_a_paused_repeat_does_not_advertise_a_next_round():
    """It is not coming, and saying otherwise is the kind of small lie that
    makes someone stop trusting the rest of the screen."""
    from datetime import UTC, datetime, timedelta

    from app.adminbot import views

    broadcast = fake_broadcast(
        status=BroadcastStatus.paused,
        repeat_every_s=7200,
        repeat_count=1,
        next_run_at=datetime.now(UTC) + timedelta(hours=2),
    )
    text = views.ad_detail(broadcast=broadcast, counts={}, target_count=2).text

    assert "Next round" not in text
    assert "Rounds sent* — 1" in text


def test_the_repeat_button_fits_telegram_s_callback_limit():
    from app.adminbot import views
    from tests.integration.test_bot_flows import assert_keyboard_is_sendable

    screen = views.ad_compose(broadcast=fake_broadcast(), target_count=1, estimate_s=0)
    assert_keyboard_is_sendable(screen.keyboard)
    assert any(
        button.callback_data.endswith(":repeat")
        for row in screen.keyboard.inline_keyboard
        for button in row
    )


# --------------------------------------------------------------------------- #
# Saying what actually happened
# --------------------------------------------------------------------------- #
def test_an_ad_nobody_received_is_not_a_green_tick():
    """`completed` means the round ran out of work, not that anyone got it. An
    ad whose only group refused it showed the same ✅ as one that reached 500."""
    from app.adminbot import views

    nothing_arrived = fake_broadcast(status=BroadcastStatus.completed)
    assert views.delivery_icon(nothing_arrived, {"skipped": 1}) == "⚠️"
    assert views.delivery_icon(nothing_arrived, {"succeeded": 1}) == "✅"


def test_the_detail_screen_names_how_many_missed_it():
    from app.adminbot import views

    text = views.ad_detail(
        broadcast=fake_broadcast(status=BroadcastStatus.completed),
        counts={"succeeded": 3, "skipped": 2},
        target_count=5,
    ).text

    assert "2 of 5 did not receive it" in text
    assert "Events" in text


def test_a_fully_delivered_ad_says_nothing_extra():
    """A warning on every screen is a warning nobody reads."""
    from app.adminbot import views

    text = views.ad_detail(
        broadcast=fake_broadcast(status=BroadcastStatus.completed),
        counts={"succeeded": 5},
        target_count=5,
    ).text
    assert "did not receive it" not in text


def test_the_ads_list_shows_what_arrived():
    from app.adminbot import views
    from tests.integration.test_bot_flows import assert_keyboard_is_sendable

    a = fake_broadcast(name="Reached nobody", status=BroadcastStatus.completed)
    b = fake_broadcast(name="Reached everyone", status=BroadcastStatus.completed)
    screen = views.ads_list(
        broadcasts=[a, b],
        page=0,
        can_create=True,
        counts_by_id={a.id: {"skipped": 4}, b.id: {"succeeded": 4}},
    )
    labels = [btn.text for row in screen.keyboard.inline_keyboard for btn in row]
    assert any(label.startswith("⚠️") and "0/4" in label for label in labels)
    assert any(label.startswith("✅") and "4/4" in label for label in labels)
    assert_keyboard_is_sendable(screen.keyboard)


def test_the_activity_screen_names_the_group():
    """Twelve identical "not allowed to post" lines say something is wrong but
    not which group to go and fix."""
    import uuid as _uuid
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.adminbot import views
    from app.domain import reasons
    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    chat_id = _uuid.uuid4()
    event = SimpleNamespace(
        occurred_at=datetime.now(UTC),
        outcome=SimpleNamespace(value="skipped"),
        reason_code=reasons.WRITE_FORBIDDEN,
        detail_safe=None,
        destination_chat_id=chat_id,
    )
    screen = views.activity(events=[event], titles={chat_id: "Crypto Deals (main)"})

    assert "Crypto Deals" in screen.text
    assert_valid_markdown_v2(screen.text)


def test_a_group_named_with_markdown_cannot_break_the_activity_screen():
    """Group titles are attacker-influenced — anyone can name a group `*bold*`."""
    import uuid as _uuid
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.adminbot import views
    from app.domain import reasons
    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    chat_id = _uuid.uuid4()
    event = SimpleNamespace(
        occurred_at=datetime.now(UTC),
        outcome=SimpleNamespace(value="skipped"),
        reason_code=reasons.WRITE_FORBIDDEN,
        detail_safe=None,
        destination_chat_id=chat_id,
    )
    screen = views.activity(events=[event], titles={chat_id: "_evil* [group](x) #1"})
    assert_valid_markdown_v2(screen.text)


# --------------------------------------------------------------------------- #
# Intervals in the units they were typed in
# --------------------------------------------------------------------------- #
def test_an_interval_reads_back_as_it_was_set():
    """ "12.0 hours" is what rounding produces; "90 minutes" is what someone
    typed, and cannot be expressed as a rounded number of hours at all."""
    from app.adminbot import views

    assert views.interval_label(43_200) == "12 hours"
    assert views.interval_label(5_400) == "1 hour 30 minutes"
    assert views.interval_label(3_600) == "1 hour"
    assert views.interval_label(1_800) == "30 minutes"
    assert views.repeat_label(None) == "once, then stop"


def test_the_confirm_screen_shows_the_whole_ad_and_says_what_it_will_do():
    """It clipped at 300 characters — the one screen where the customer is
    deciding whether to post showed the least of what they were posting."""
    from app.adminbot import views
    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    body = "Our October offer is live. " * 30
    fresh = views.ad_confirm(
        broadcast=fake_broadcast(body_text=body), target_count=5, estimate_s=12
    )
    assert views.escape(body.strip()) in fresh.text
    assert "Send this ad?" in fresh.text

    resuming = views.ad_confirm(
        broadcast=fake_broadcast(body_text=body, status=BroadcastStatus.paused),
        target_count=5,
        estimate_s=12,
    )
    assert "Save and resume?" in resuming.text
    assert "already posted to are not posted to again" in resuming.text
    for screen in (fresh, resuming):
        assert len(screen.text) <= 4096
        assert_valid_markdown_v2(screen.text)


def test_a_clipped_confirm_screen_is_still_valid_markdown():
    from app.adminbot import views
    from app.config import get_settings
    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    body = ".!-()" * (get_settings().max_broadcast_text_len // 5)
    screen = views.ad_confirm(
        broadcast=fake_broadcast(body_text=body), target_count=5, estimate_s=1
    )
    assert "more characters" in screen.text
    assert len(screen.text) <= 4096
    assert_valid_markdown_v2(screen.text)


# --------------------------------------------------------------------------- #
# Why a round missed
# --------------------------------------------------------------------------- #
def _valid(text: str) -> None:
    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    assert_valid_markdown_v2(text)


def test_the_ad_screen_says_why_the_groups_missed_it():
    """ "152 of 156 did not receive it" and a list of 152 rows is a pattern
    nobody can see — and one cause almost always accounts for nearly all of
    them, so naming it is the entire answer."""
    from app.adminbot import views
    from app.domain import reasons

    text = views.ad_detail(
        broadcast=fake_broadcast(status=BroadcastStatus.sending),
        counts={"succeeded": 4, "skipped": 152},
        target_count=156,
        reason_counts={reasons.NOT_A_MEMBER: 149, reasons.WRITE_FORBIDDEN: 3},
    ).text

    assert "152 of 156 did not receive it" in text
    assert views.escape(reasons.describe(reasons.NOT_A_MEMBER)) in text
    assert views.escape(reasons.describe(reasons.WRITE_FORBIDDEN)) in text
    assert text.index("*149*") < text.index("*3*"), "the common cause first"
    _valid(text)


def test_the_reasons_are_left_out_when_nothing_missed():
    from app.adminbot import views

    text = views.ad_detail(
        broadcast=fake_broadcast(status=BroadcastStatus.completed),
        counts={"succeeded": 5},
        target_count=5,
        reason_counts={},
    ).text

    assert "did not receive it" not in text
    _valid(text)


async def test_the_reason_counts_come_from_the_targets_own_codes(client, actor, session):
    """One grouped query rather than a count per row: an ad with 500 groups
    opens its screen as fast as any other."""
    from app.db.models import BroadcastTarget, JobStatus
    from app.domain import reasons
    from app.repositories import broadcasts as broadcast_repo

    built = await build_broadcast(actor, session, groups=4)
    targets = (
        (
            await session.execute(
                select(BroadcastTarget)
                .where(BroadcastTarget.broadcast_id == built["broadcast_id"])
                .order_by(BroadcastTarget.position)
            )
        )
        .scalars()
        .all()
    )
    outcomes = [
        (JobStatus.skipped, reasons.NOT_A_MEMBER),
        (JobStatus.skipped, reasons.NOT_A_MEMBER),
        (JobStatus.failed, reasons.WRITE_FORBIDDEN),
        (JobStatus.succeeded, None),
    ]
    for target, (status, code) in zip(targets, outcomes, strict=True):
        target.status = status
        target.last_error_code = code
    await session.commit()

    counted = await broadcast_repo.reason_counts(session, broadcast_id=built["broadcast_id"])

    assert counted == {reasons.NOT_A_MEMBER: 2, reasons.WRITE_FORBIDDEN: 1}, (
        "the delivered one is not a reason for anything"
    )
