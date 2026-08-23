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
from tests.conftest import connect_bot, discovered, script_for, sync_with_chats
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
def test_a_non_premium_account_is_warned_before_sending():
    """Custom emoji arrive as ordinary ones without Telegram Premium. Finding
    that out from 157 posted ads is the wrong way to learn it."""
    from app.adminbot import views

    warning = views.premium_emoji_warning(has_premium_emoji=True, account_is_premium=False)
    text = "\n".join(warning)

    assert "not Telegram Premium" in text
    assert "arrive as ordinary emoji" in text
    assert "everything else posts exactly as written" in text, (
        "the rest of the formatting does work, and saying so avoids a false alarm"
    )


def test_a_premium_account_is_not_warned():
    from app.adminbot import views

    assert views.premium_emoji_warning(has_premium_emoji=True, account_is_premium=True) == []


def test_an_ad_without_premium_emoji_is_not_warned():
    from app.adminbot import views

    assert views.premium_emoji_warning(has_premium_emoji=False, account_is_premium=False) == []


def test_the_compose_screen_explains_why_its_own_preview_looks_plain():
    """The preview is escaped plain text, and this bot could not render a custom
    emoji even if it tried — the Bot API reserves those for bots with a Fragment
    username. Without saying so, the preview reads as the result."""
    from types import SimpleNamespace

    from app.adminbot import views

    broadcast = SimpleNamespace(
        id=uuid.uuid4(),
        name="Sale",
        status=BroadcastStatus.draft,
        body_text="INSIGHT STORE",
        body_entities=[{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "1"}],
        media_kind=SimpleNamespace(value="none"),
        delay_ms=3000,
        paused_reason_code=None,
    )
    screen = views.ad_compose(
        broadcast=broadcast, target_count=1, estimate_s=0, account_is_premium=True
    )
    assert "preview above is plain text" in screen.text
    assert "The posted ad keeps them" in screen.text


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
