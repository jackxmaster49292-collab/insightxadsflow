"""Keeping a copy of what was posted, in a group the customer controls.

The reason decides the tests: an account can be lost, and a link into a private
group is worthless from an account that is no longer in it. So the archive is
checked for the *content* first and the links second, and each link is checked
for whether it would still work once the poster is gone.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import (
    AppSetting,
    Broadcast,
    BroadcastStatus,
    BroadcastTarget,
    ChatKind,
    TelegramChat,
    TelegramConnection,
)
from app.domain.message_links import LinkKind, link_for
from app.services import archive as archive_service
from app.services import broadcast as broadcast_service
from tests.conftest import script_for
from tests.integration.test_broadcast import build_broadcast, drain, group_id


@pytest.fixture(autouse=True)
async def as_operator(actor, session, monkeypatch):
    """The archive is operator-only, so the tests run as one.

    Not a convenience: it is the shape of the feature. The negative cases below
    take this fixture away deliberately.
    """
    from app.config import get_settings
    from app.repositories import users as user_repo
    from tests.integration.test_bot_flows import ADMIN_CHAT

    user = await user_repo.get_by_id(session, uuid.UUID(actor.id))
    user.telegram_user_id = ADMIN_CHAT
    await session.commit()
    monkeypatch.setattr(get_settings(), "admin_telegram_ids", str(ADMIN_CHAT), raising=False)


AD_ENTITIES = [
    {"type": "bold", "offset": 0, "length": 6},
    {"type": "custom_emoji", "offset": 7, "length": 2, "custom_emoji_id": "5368324170671202286"},
]


# --------------------------------------------------------------------------- #
# What a link is worth
# --------------------------------------------------------------------------- #
def test_a_public_group_link_outlives_the_account_that_posted():
    link = link_for(
        chat_kind="supergroup", peer_id=-1002001234567, username="insightxpro", message_id=42
    )
    assert link.url == "https://t.me/insightxpro/42"
    assert link.kind is LinkKind.public
    assert link.durable, "resolves for anyone, forever"


def test_a_private_group_link_works_but_only_for_members():
    """The -100 prefix is what t.me/c wants removed. And this link is exactly
    the one an archive cannot rely on."""
    link = link_for(chat_kind="supergroup", peer_id=-1002001234567, username=None, message_id=42)
    assert link.url == "https://t.me/c/2001234567/42"
    assert link.kind is LinkKind.members_only
    assert not link.durable


def test_a_basic_group_has_no_message_link_at_all():
    """Telegram publishes no form for these. Inventing one would produce a
    link to some other chat."""
    link = link_for(chat_kind="group", peer_id=-412345678, username=None, message_id=42)
    assert link.url is None
    assert link.kind is LinkKind.none


def test_an_unposted_target_has_no_link():
    link = link_for(chat_kind="supergroup", peer_id=-1002001234567, username="x", message_id=None)
    assert link.url is None


def test_an_id_without_the_prefix_is_refused_rather_than_guessed():
    link = link_for(chat_kind="supergroup", peer_id=2001234567, username=None, message_id=42)
    assert link.url is None, "guessing would address a different chat"


# --------------------------------------------------------------------------- #
# The archive itself
# --------------------------------------------------------------------------- #
async def _with_archive(session, actor, ctx):
    """Point this account's archive at the last of its groups."""
    chats = (
        (await session.execute(select(TelegramChat).where(TelegramChat.id.in_(ctx["chat_ids"]))))
        .scalars()
        .all()
    )
    keep = sorted(chats, key=lambda c: c.title)[-1]
    # The row already exists from sign-up, so this is an update rather than an
    # insert — the same thing the panel's own handler does.
    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    if setting is None:
        setting = AppSetting(user_id=uuid.UUID(actor.id))
        session.add(setting)
    setting.archive_chat_id = keep.id
    await session.commit()
    return keep


async def test_the_copy_goes_out_before_the_index(client, actor, session):
    """The copy is what survives the account. The index only points at other
    people's groups, so it goes second."""
    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0, text="SALE 🔥 today")
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.body_entities = AD_ENTITIES
    keep = await _with_archive(session, actor, ctx)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    sends = script_for(ctx["connection_id"]).calls_to("send_text")
    archived = [c for c in sends if c.args[0].peer_id == keep.peer_id]
    # Three ad deliveries plus the archive copy plus one index message.
    assert len(archived) >= 2

    copy, index = archived[-2], archived[-1]
    assert copy.args[1] == "SALE 🔥 today", "the ad, exactly as written"
    entity_types = [e.type for e in copy.kwargs["entities"]]
    assert entity_types == ["bold", "custom_emoji"], "formatting and premium emoji survive"
    assert "Group" in index.args[1]


async def test_the_index_names_every_group_and_its_link(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    keep = await _with_archive(session, actor, ctx)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()

    sends = script_for(ctx["connection_id"]).calls_to("send_text")
    index = [c for c in sends if c.args[0].peer_id == keep.peer_id][-1].args[1]

    assert "Group 01" in index and "Group 02" in index and "Group 03" in index
    assert "t.me/c/" in index, "private supergroups still get a members-only link"
    assert "members only" in index, "and the catch is stated"


async def test_no_archive_configured_means_nothing_extra_is_sent(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)

    assert len(script_for(ctx["connection_id"]).calls_to("send_text")) == 2, "only the ads"


async def test_a_failing_archive_never_fails_the_round(client, actor, session):
    """The ads are already delivered by the time this runs. A missing copy is a
    smaller loss than a round marked failed over its own bookkeeping."""
    from app.db.models import BroadcastStatus

    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _with_archive(session, actor, ctx)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    script = script_for(ctx["connection_id"])
    outcomes = await drain(session, broadcast.id)
    await session.commit()

    assert all(o.status.value == "succeeded" for o in outcomes)
    await session.refresh(broadcast)
    assert broadcast.status is BroadcastStatus.completed
    assert script.calls_to("send_text")


async def test_only_delivered_groups_are_indexed(client, actor, session):
    """An index that listed a group the ad never reached would be a lie in the
    one place kept as a record."""
    from app.adapters.base import AccessReport
    from app.domain import reasons
    from app.repositories import chats as chat_repo

    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    keep = await _with_archive(session, actor, ctx)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    blocked = chat_repo.to_ref(await session.get(TelegramChat, ctx["chat_ids"][0]))
    script_for(ctx["connection_id"]).destination_allowed[blocked.key] = AccessReport(
        allowed=False, reason_code=reasons.WRITE_FORBIDDEN
    )

    await drain(session, broadcast.id)
    await session.commit()

    sends = script_for(ctx["connection_id"]).calls_to("send_text")
    index = [c for c in sends if c.args[0].peer_id == keep.peer_id][-1].args[1]
    assert "2 groups" in index
    assert "Group 01" not in index, "the refused group is not claimed as archived"


async def test_a_long_index_is_split_to_fit_telegram(client, actor, session):
    """One message per round would exceed 4096 and be refused wholesale."""
    lines = [
        f"A group with a fairly long title number {i}\nhttps://t.me/c/2001/{i}" for i in range(200)
    ]
    chunks = archive_service._chunks(lines, header="📁 Ad — 200 groups")

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 4096
    joined = "\n".join(chunks)
    for i in (0, 99, 199):
        assert f"number {i}\n" in joined, "no group is dropped by the split"


async def test_each_repeat_round_is_archived(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = 7200
    keep = await _with_archive(session, actor, ctx)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    sends = script_for(ctx["connection_id"]).calls_to("send_text")
    index = [c for c in sends if c.args[0].peer_id == keep.peer_id][-1].args[1]
    assert "round 1" in index


async def test_another_accounts_chat_cannot_become_your_archive(
    client, actor, other_actor, state, session
):
    """An id alone must never be enough — the same rule as everywhere else."""
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback

    ctx = await build_broadcast(other_actor, session, groups=1)
    theirs = ctx["chat_ids"][0]

    await handlers.set_archive(
        a_callback(f"arch:s:{theirs}"), user_id=uuid.UUID(actor.id), is_operator=True
    )

    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    assert setting is None or setting.archive_chat_id is None


async def test_the_archive_screen_can_be_set_and_turned_off(client, actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import Sent, a_callback

    ctx = await build_broadcast(actor, session, groups=2)
    mine = ctx["chat_ids"][0]

    await handlers.set_archive(
        a_callback(f"arch:s:{mine}"), user_id=uuid.UUID(actor.id), is_operator=True
    )
    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    await session.refresh(setting)
    assert setting.archive_chat_id == mine
    assert "Group" in Sent.last()

    await handlers.set_archive(
        a_callback("arch:off"), user_id=uuid.UUID(actor.id), is_operator=True
    )
    await session.refresh(setting)
    assert setting.archive_chat_id is None


def test_the_archive_screen_explains_why_a_copy_and_not_just_links():
    from app.adminbot import views
    from tests.integration.test_bot_flows import (
        assert_keyboard_is_sendable,
        assert_valid_markdown_v2,
    )

    empty = views.archive_settings(current=None, chats=[], page=0)
    assert "only opens for members" in empty.text
    assert "No groups synced yet" in empty.text
    assert_valid_markdown_v2(empty.text)
    assert_keyboard_is_sendable(empty.keyboard)


@pytest.mark.parametrize("kind", [ChatKind.group, ChatKind.supergroup])
def test_a_hostile_group_title_cannot_break_the_archive_screen(kind):
    from types import SimpleNamespace

    from app.adminbot import views
    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    chat = SimpleNamespace(id=uuid.uuid4(), title="_evil* [x](y) #1!", chat_kind=kind)
    screen = views.archive_settings(current=chat.title, chats=[chat], page=0)
    assert_valid_markdown_v2(screen.text)


def test_group_id_helper_is_a_supergroup_id():
    """The suite's own fixtures must exercise the -100 form, or the link tests
    would pass against ids Telegram never issues."""
    assert str(group_id(0)).startswith("-100")


# --------------------------------------------------------------------------- #
# The bio of a private group, so nothing is lost
# --------------------------------------------------------------------------- #
async def _details_for(session, ctx, script):
    """Script a bio for every group in this broadcast."""
    from app.adapters.base import ChatDetails
    from app.repositories import chats as chat_repo

    for chat_id in ctx["chat_ids"]:
        chat = await session.get(TelegramChat, chat_id)
        script.chat_details[chat_repo.to_ref(chat).key] = ChatDetails(
            description=f"Deals and offers for {chat.title}", member_count=12_400
        )


async def test_a_private_groups_bio_and_size_are_in_the_index(client, actor, session):
    """A members-only link needs the account to still be a member; the title
    alone is hard to recognise months later. The bio and the size are what
    identify the group again — which is the whole reason for keeping them."""
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    keep = await _with_archive(session, actor, ctx)
    script = script_for(ctx["connection_id"])
    await _details_for(session, ctx, script)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    sends = script.calls_to("send_text")
    index = [c for c in sends if c.args[0].peer_id == keep.peer_id][-1].args[1]
    assert "Deals and offers for Group 01" in index
    assert "12,400 members" in index


async def test_details_are_cached_so_the_next_round_asks_nothing(client, actor, session):
    """GetFullChannel is one of Telegram's most eagerly rate-limited calls.
    Once learned, a bio is read from the chat row, not from Telegram."""
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = 7200
    await _with_archive(session, actor, ctx)
    script = script_for(ctx["connection_id"])
    await _details_for(session, ctx, script)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()
    first_round = len(script.calls_to("chat_details"))
    assert first_round > 0

    await drain(session, broadcast.id)  # round two, same groups
    await session.commit()
    assert len(script.calls_to("chat_details")) == first_round, "round two asked nothing"


async def test_a_public_groups_details_are_not_fetched(client, actor, session):
    """A username is already a durable way back; the lookup budget belongs to
    the private groups that have no other identity."""
    from sqlalchemy import update as sa_update

    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _with_archive(session, actor, ctx)
    await session.execute(
        sa_update(TelegramChat)
        .where(TelegramChat.id.in_(ctx["chat_ids"]))
        .values(username="somepublicgroup", is_public=True)
    )
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    assert script_for(ctx["connection_id"]).calls_to("chat_details") == []


async def test_a_failing_details_lookup_still_archives_the_rest(client, actor, session):
    """The copy and the links are the record; the bio is a garnish on it."""
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    keep = await _with_archive(session, actor, ctx)
    script = script_for(ctx["connection_id"])
    script.fail_method("chat_details", RuntimeError("lookup exploded"))
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    sends = script.calls_to("send_text")
    index = [c for c in sends if c.args[0].peer_id == keep.peer_id][-1].args[1]
    assert "Group 01" in index and "t.me/c/" in index, "links survive a failed lookup"


async def test_every_private_group_is_covered_in_one_round(client, actor, session, monkeypatch):
    """A capped version was offered and rejected, correctly: an archive that
    identifies only some of the groups is not a record. All of them, one
    round, taking the time it takes."""
    from app.services import archive as archive_module

    monkeypatch.setattr(archive_module, "_DETAILS_GAP_S", 0)

    groups = 40
    ctx = await build_broadcast(actor, session, groups=groups, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    keep = await _with_archive(session, actor, ctx)
    script = script_for(ctx["connection_id"])
    await _details_for(session, ctx, script)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    assert len(script.calls_to("chat_details")) == groups, "no group left out"
    sends = script.calls_to("send_text")
    index = "\n".join(c.args[1] for c in sends if c.args[0].peer_id == keep.peer_id)
    assert index.count("12,400 members") == groups, "and every one is identified"


async def test_a_failed_lookup_is_retried_every_round_until_learned(
    client, actor, session, monkeypatch
):
    """ "Try again in the second round, as many times as it takes" — a failure
    leaves the chat unmarked, so every later round asks again."""
    from app.services import archive as archive_module

    monkeypatch.setattr(archive_module, "_DETAILS_GAP_S", 0)

    ctx = await build_broadcast(actor, session, groups=1, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.repeat_every_s = 7200
    keep = await _with_archive(session, actor, ctx)
    script = script_for(ctx["connection_id"])
    script.fail_method("chat_details", RuntimeError("telegram hiccup"))
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)  # round 1: lookup fails
    await session.commit()
    round_one = len(script.calls_to("chat_details"))
    assert round_one > 0

    # The hiccup clears; round two asks again and this time it sticks.
    script.method_errors.clear()
    await _details_for(session, ctx, script)
    await drain(session, broadcast.id)
    await session.commit()

    assert len(script.calls_to("chat_details")) > round_one, "round two asked again"
    sends = script.calls_to("send_text")
    index = [c for c in sends if c.args[0].peer_id == keep.peer_id][-1].args[1]
    assert "12,400 members" in index


async def test_a_long_telegram_wait_defers_the_rest_to_next_round(
    client, actor, session, monkeypatch
):
    """A wait past the ceiling is never shortened — the walk stops instead,
    and the unlearned chats stay unmarked for the next round."""
    from app.adapters.errors import AdapterError, ErrorClass
    from app.domain import reasons
    from app.services import archive as archive_module

    monkeypatch.setattr(archive_module, "_DETAILS_GAP_S", 0)

    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    keep = await _with_archive(session, actor, ctx)
    script = script_for(ctx["connection_id"])
    script.fail_method(
        "chat_details",
        AdapterError(reasons.FLOOD_WAIT, ErrorClass.RATE_LIMIT, retry_after_s=3_000),
    )
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    assert len(script.calls_to("chat_details")) == 1, "stopped at the first long wait"
    sends = script.calls_to("send_text")
    index = [c for c in sends if c.args[0].peer_id == keep.peer_id][-1].args[1]
    assert "t.me/c/" in index, "the record itself still goes out"

    unmarked = (
        (
            await session.execute(
                select(TelegramChat).where(
                    TelegramChat.id.in_(ctx["chat_ids"]),
                    TelegramChat.details_synced_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(unmarked) == len(ctx["chat_ids"]), "all still owed, all retried next round"


async def test_a_short_telegram_wait_is_obeyed_then_the_walk_continues(
    client, actor, session, monkeypatch
):
    """Under the ceiling the wait is served in place — obeyed in full, never
    shortened — and the round still ends fully covered."""
    from app.adapters.errors import AdapterError, ErrorClass
    from app.domain import reasons
    from app.services import archive as archive_module

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(archive_module, "_sleep", fake_sleep)
    monkeypatch.setattr(archive_module, "_DETAILS_GAP_S", 0)

    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _with_archive(session, actor, ctx)
    script = script_for(ctx["connection_id"])
    await _details_for(session, ctx, script)
    # One wait, then Telegram relents (the mock raises only while set).
    script.fail_method(
        "chat_details",
        AdapterError(reasons.FLOOD_WAIT, ErrorClass.RATE_LIMIT, retry_after_s=42),
    )
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    # Clear the error as soon as the first wait is recorded, like Telegram
    # letting the next call through after the wait was served.
    original = fake_sleep

    async def sleep_then_clear(seconds: float) -> None:
        await original(seconds)
        script.method_errors.clear()

    monkeypatch.setattr(archive_module, "_sleep", sleep_then_clear)

    await drain(session, broadcast.id)
    await session.commit()

    assert 42 in slept, "Telegram's number, served in full"
    synced = (
        (
            await session.execute(
                select(TelegramChat).where(
                    TelegramChat.id.in_(ctx["chat_ids"]),
                    TelegramChat.details_synced_at.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(synced) == len(ctx["chat_ids"]), "the walk finished after the wait"


async def test_a_long_bio_is_clipped_not_dominant(client, actor, session):
    """One chatty bio must not swallow the chunk budget for everyone else."""
    from app.adapters.base import ChatDetails
    from app.repositories import chats as chat_repo
    from app.services.archive import _BIO_CHARS

    ctx = await build_broadcast(actor, session, groups=1, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    keep = await _with_archive(session, actor, ctx)
    script = script_for(ctx["connection_id"])
    chat = await session.get(TelegramChat, ctx["chat_ids"][0])
    script.chat_details[chat_repo.to_ref(chat).key] = ChatDetails(
        description="word " * 200, member_count=5
    )
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    sends = script.calls_to("send_text")
    index = [c for c in sends if c.args[0].peer_id == keep.peer_id][-1].args[1]
    bio_line = next(line for line in index.splitlines() if "word" in line)
    assert len(bio_line) < _BIO_CHARS + 40
    assert "…" in bio_line


# --------------------------------------------------------------------------- #
# Delivered by the bot, so it survives the account
# --------------------------------------------------------------------------- #
BOT_GROUP_ID = -1003988585561


async def _bot_archive(session, actor, chat_id: int = BOT_GROUP_ID):
    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    if setting is None:
        setting = AppSetting(user_id=uuid.UUID(actor.id))
        session.add(setting)
    setting.archive_bot_chat_id = chat_id
    setting.archive_chat_id = None
    await session.commit()


def _bot_script():
    from app.services.archive import _ARCHIVE_BOT_ID

    return script_for(_ARCHIVE_BOT_ID)


async def test_the_bot_delivers_the_archive_when_it_is_set(client, actor, session):
    """The whole reason for this route: an archive the *account* delivers stops
    the day that account is lost, which is the event being insured against."""
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0, text="SALE 🔥")
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    broadcast.body_entities = AD_ENTITIES
    await _bot_archive(session, actor)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    account_sends = script_for(ctx["connection_id"]).calls_to("send_text")
    assert len(account_sends) == 2, "the account posted the ads and nothing else"

    bot_sends = _bot_script().calls_to("send_text")
    assert [c.args[0].peer_id for c in bot_sends] == [BOT_GROUP_ID, BOT_GROUP_ID]
    copy, index = bot_sends
    assert copy.args[1] == "SALE 🔥"
    assert [e.type for e in copy.kwargs["entities"]] == ["bold", "custom_emoji"]
    assert "Group 01" in index.args[1]


async def test_the_bot_route_wins_over_a_synced_group(client, actor, session):
    """Two destinations would double every copy, so one is chosen — the bot,
    because it is the one that outlives the account."""
    ctx = await build_broadcast(actor, session, groups=1, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    keep = await _with_archive(session, actor, ctx)
    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    setting.archive_bot_chat_id = BOT_GROUP_ID
    await session.commit()
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    to_account_group = [
        c
        for c in script_for(ctx["connection_id"]).calls_to("send_text")
        if c.args[0].peer_id == keep.peer_id
    ]
    assert len(to_account_group) == 1, "only the ad itself, no second copy"
    assert _bot_script().calls_to("send_text"), "the bot carried the archive"


async def test_setting_one_route_clears_the_other(client, actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback

    ctx = await build_broadcast(actor, session, groups=1)
    await _bot_archive(session, actor)

    await handlers.set_archive(
        a_callback(f"arch:s:{ctx['chat_ids'][0]}"), user_id=uuid.UUID(actor.id), is_operator=True
    )
    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    await session.refresh(setting)
    assert setting.archive_chat_id == ctx["chat_ids"][0]
    assert setting.archive_bot_chat_id is None, "one destination at a time"


async def test_turning_it_off_clears_both_routes(client, actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback

    await _bot_archive(session, actor)
    await handlers.set_archive(
        a_callback("arch:off"), user_id=uuid.UUID(actor.id), is_operator=True
    )

    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    await session.refresh(setting)
    assert setting.archive_bot_chat_id is None
    assert setting.archive_chat_id is None


async def test_the_id_is_proved_before_it_is_saved(client, actor, state, session):
    """A missing invite or a missing permission is discovered here, not by
    silently swallowing every archive from then on."""
    # send_text fails per destination, not per method name.
    from app.adapters.base import ChatRef, PeerKind
    from app.adminbot import handlers
    from app.adminbot.states import SetArchive
    from tests.integration.test_bot_flows import Sent, a_message

    _bot_script().fail_delivery(
        ChatRef(PeerKind.channel, BOT_GROUP_ID), RuntimeError("CHAT_WRITE_FORBIDDEN")
    )
    await state.set_state(SetArchive.chat)
    await handlers.archive_chat_given(
        a_message(str(BOT_GROUP_ID)),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )

    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    assert setting is None or setting.archive_bot_chat_id is None
    assert "could not post there" in Sent.last()


async def test_a_proved_id_is_saved_and_announced(client, actor, state, session):
    from app.adminbot import handlers
    from app.adminbot.states import SetArchive
    from tests.integration.test_bot_flows import a_message

    await state.set_state(SetArchive.chat)
    await handlers.archive_chat_given(
        a_message(str(BOT_GROUP_ID)),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=True,
    )

    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    await session.refresh(setting)
    assert setting.archive_bot_chat_id == BOT_GROUP_ID

    hello = _bot_script().calls_to("send_text")[0]
    assert hello.args[0].peer_id == BOT_GROUP_ID
    assert "Archive set up" in hello.args[1]


async def test_nonsense_instead_of_an_id_is_explained(client, actor, state, session):
    from app.adminbot import handlers
    from app.adminbot.states import SetArchive
    from tests.integration.test_bot_flows import Sent, a_message

    await state.set_state(SetArchive.chat)
    await handlers.archive_chat_given(
        a_message("my group"), user_id=uuid.UUID(actor.id), state=state, is_operator=True
    )

    assert "not a chat id" in Sent.last()
    assert _bot_script().calls_to("send_text") == [], "nothing sent on a typo"


async def test_a_forwarded_message_supplies_the_id(client, actor, state, session):
    """Nobody should have to know where Telegram hides a chat id."""
    from aiogram.types import Chat, MessageOriginChat

    from app.adminbot import handlers
    from app.adminbot.states import SetArchive
    from tests.integration.test_bot_flows import a_message

    forwarded = a_message("anything").model_copy(
        update={
            "forward_origin": MessageOriginChat(
                type="chat",
                date=__import__("datetime").datetime.now(__import__("datetime").UTC),
                sender_chat=Chat(id=BOT_GROUP_ID, type="supergroup"),
                chat=Chat(id=BOT_GROUP_ID, type="supergroup"),
            )
        }
    )

    await state.set_state(SetArchive.chat)
    await handlers.archive_chat_given(
        forwarded, user_id=uuid.UUID(actor.id), state=state, is_operator=True
    )

    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    await session.refresh(setting)
    assert setting.archive_bot_chat_id == BOT_GROUP_ID


def test_the_screen_offers_both_routes_and_explains_the_difference():
    from app.adminbot import views
    from tests.integration.test_bot_flows import (
        assert_keyboard_is_sendable,
        assert_valid_markdown_v2,
    )

    screen = views.archive_settings(current=None, chats=[], page=0)
    callbacks = [b.callback_data for row in screen.keyboard.inline_keyboard for b in row]
    assert "arch:bot" in callbacks
    assert "if the posting account is ever gone" in screen.text
    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)


# --------------------------------------------------------------------------- #
# Operator only — the admin id and nowhere else
# --------------------------------------------------------------------------- #
async def test_a_non_operator_cannot_open_the_archive_screen(client, actor, state):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import Sent, a_callback

    await handlers.nav_archive(
        a_callback("nav:arch:0"), user_id=uuid.UUID(actor.id), is_operator=False
    )
    assert any("not available" in alert for alert in Sent.alerts)


async def test_a_non_operator_cannot_set_an_archive(client, actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback

    ctx = await build_broadcast(actor, session, groups=1)
    await handlers.set_archive(
        a_callback(f"arch:s:{ctx['chat_ids'][0]}"),
        user_id=uuid.UUID(actor.id),
        is_operator=False,
    )

    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    assert setting is None or setting.archive_chat_id is None


async def test_a_non_operator_left_mid_flow_is_shown_the_door(client, actor, state, session):
    """Being in a conversation state is not authorization — an operator list
    can change between the question and the answer."""
    from app.adminbot import handlers
    from app.adminbot.states import SetArchive
    from tests.integration.test_bot_flows import a_message

    await state.set_state(SetArchive.chat)
    await handlers.archive_chat_given(
        a_message(str(BOT_GROUP_ID)),
        user_id=uuid.UUID(actor.id),
        state=state,
        is_operator=False,
    )

    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    assert setting is None or setting.archive_bot_chat_id is None
    assert await state.get_state() is None, "and not left stuck in the flow"


async def test_a_row_belonging_to_a_non_operator_archives_nothing(
    client, actor, session, monkeypatch
):
    """The layer that holds however a row got written: a leftover from before
    the feature was restricted, a direct database edit, a future API."""
    from app.config import get_settings
    from app.services import archive as archive_service

    ctx = await build_broadcast(actor, session, groups=1, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _bot_archive(session, actor)

    # Same row, same setting — the account simply is not an operator any more.
    monkeypatch.setattr(get_settings(), "admin_telegram_ids", "", raising=False)
    assert await archive_service.destination_for(session, user_id=uuid.UUID(actor.id)) is None

    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()

    assert _bot_script().calls_to("send_text") == [], "nothing archived"


def test_the_archive_button_is_operator_only():
    from types import SimpleNamespace

    from app.adminbot import views

    connection = SimpleNamespace(
        id=uuid.uuid4(),
        label="Jack",
        kind=SimpleNamespace(value="user"),
        status=SimpleNamespace(value="active"),
    )

    def buttons(is_operator: bool) -> list[str]:
        screen = views.home(
            connections=[connection],
            rules=[],
            broadcasts=[],
            counts={},
            is_operator=is_operator,
        )
        return [b.callback_data for row in screen.keyboard.inline_keyboard for b in row]

    assert "nav:arch:0" in buttons(True)
    assert "nav:arch:0" not in buttons(False)


# --------------------------------------------------------------------------- #
# Stopping an ad still leaves a record
# --------------------------------------------------------------------------- #
async def test_stopping_an_ad_archives_what_it_already_delivered(client, actor, session):
    """Stopping half way is exactly when the record matters most. settle()
    returns early for anything not still sending, so a stopped ad used to leave
    no trace of the groups it had already reached."""
    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _bot_archive(session, actor)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    # One group receives it, then the customer stops the ad.
    targets = (
        (
            await session.execute(
                select(BroadcastTarget)
                .where(BroadcastTarget.broadcast_id == broadcast.id)
                .order_by(BroadcastTarget.position)
            )
        )
        .scalars()
        .all()
    )
    connection = await session.get(TelegramConnection, broadcast.connection_id)
    from app.services import connections as connection_service

    adapter = await connection_service.adapter_for(session, connection)
    await broadcast_service.execute_target(
        session, target=targets[0], adapter=adapter, connection=connection
    )
    await session.commit()

    _bot_script().calls.clear()
    await broadcast_service.cancel(session, broadcast=broadcast)
    await session.commit()

    archived = _bot_script().calls_to("send_text")
    assert archived, "the one delivered group is still recorded"
    index = archived[-1].args[1]
    assert "1 groups" in index
    assert "Group 01" in index
    assert "Group 02" not in index, "and groups it never reached are not claimed"


async def test_stopping_an_ad_that_delivered_nothing_archives_nothing(client, actor, session):
    """An empty index would be noise in the one place kept as evidence."""
    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _bot_archive(session, actor)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await broadcast_service.cancel(session, broadcast=broadcast)
    await session.commit()

    assert _bot_script().calls_to("send_text") == []


async def test_the_bot_route_archives_without_an_account_adapter(client, actor, session):
    """Stopping should not have to build an MTProto client when the bot is the
    one that carries the archive."""
    from app.services import archive as archive_service

    ctx = await build_broadcast(actor, session, groups=1, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _bot_archive(session, actor)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()

    _bot_script().calls.clear()
    sent = await archive_service.store_round(session, broadcast=broadcast, adapter=None)

    assert sent > 0
    assert _bot_script().calls_to("send_text")


async def test_the_account_route_says_so_when_it_has_no_courier(client, actor, session):
    """Silence here would look identical to "no archive configured"."""
    from app.services import archive as archive_service

    ctx = await build_broadcast(actor, session, groups=1, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _with_archive(session, actor, ctx)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()

    sent = await archive_service.store_round(session, broadcast=broadcast, adapter=None)
    assert sent == 0, "refused, and logged — not silently treated as unconfigured"


async def test_a_paused_then_resumed_ad_archives_everything_it_sent(client, actor, session):
    """Pause is not an ending, so it files nothing — but the deliveries made
    before it must still appear when the round finally finishes. Archiving on
    pause instead would file a partial record and then a full one, and two
    conflicting copies of the same round is worse than one late one."""
    from app.domain import reasons

    ctx = await build_broadcast(actor, session, groups=3, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _bot_archive(session, actor)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    targets = (
        (
            await session.execute(
                select(BroadcastTarget)
                .where(BroadcastTarget.broadcast_id == broadcast.id)
                .order_by(BroadcastTarget.position)
            )
        )
        .scalars()
        .all()
    )
    connection = await session.get(TelegramConnection, broadcast.connection_id)
    from app.services import connections as connection_service

    adapter = await connection_service.adapter_for(session, connection)

    # One group, then a pause.
    await broadcast_service.execute_target(
        session, target=targets[0], adapter=adapter, connection=connection
    )
    await broadcast_service.pause(
        session, broadcast=broadcast, reason_code=reasons.BROADCAST_PAUSED_BY_CUSTOMER
    )
    await session.commit()
    assert _bot_script().calls_to("send_text") == [], "a pause is not an ending"

    # Resumed, and the rest go out.
    broadcast.status = BroadcastStatus.sending
    broadcast.paused_reason_code = None
    await session.commit()
    for target in targets[1:]:
        await broadcast_service.execute_target(
            session, target=target, adapter=adapter, connection=connection
        )
    await session.commit()

    index = _bot_script().calls_to("send_text")[-1].args[1]
    assert "3 groups" in index
    for title in ("Group 01", "Group 02", "Group 03"):
        assert title in index, "including the one delivered before the pause"


async def test_a_full_length_bio_survives_whole(client, actor, session):
    """Telegram caps a description at 255 characters. Clipping below that cut
    the end off real bios — and the end is not the throwaway part when the
    point is recognising the group later."""
    from app.adapters.base import ChatDetails
    from app.repositories import chats as chat_repo

    bio = "A" * 255
    ctx = await build_broadcast(actor, session, groups=1, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _bot_archive(session, actor)
    script = script_for(ctx["connection_id"])
    chat = await session.get(TelegramChat, ctx["chat_ids"][0])
    script.chat_details[chat_repo.to_ref(chat).key] = ChatDetails(description=bio, member_count=900)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    index = _bot_script().calls_to("send_text")[-1].args[1]
    assert bio in index, "the whole description, not a prefix of it"
    assert "…" not in index


async def test_the_database_keeps_the_description_verbatim(client, actor, session):
    """Whatever the message shows, the stored copy is the untouched original."""
    from app.adapters.base import ChatDetails
    from app.repositories import chats as chat_repo

    bio = "B" * 255
    ctx = await build_broadcast(actor, session, groups=1, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _bot_archive(session, actor)
    chat = await session.get(TelegramChat, ctx["chat_ids"][0])
    script_for(ctx["connection_id"]).chat_details[chat_repo.to_ref(chat).key] = ChatDetails(
        description=bio, member_count=1
    )
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()

    await session.refresh(chat)
    assert chat.description == bio
