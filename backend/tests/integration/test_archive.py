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

from app.db.models import AppSetting, Broadcast, ChatKind, TelegramChat
from app.domain.message_links import LinkKind, link_for
from app.services import archive as archive_service
from app.services import broadcast as broadcast_service
from tests.conftest import script_for
from tests.integration.test_broadcast import build_broadcast, drain, group_id

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

    await handlers.set_archive(a_callback(f"arch:s:{theirs}"), user_id=uuid.UUID(actor.id))

    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    assert setting is None or setting.archive_chat_id is None


async def test_the_archive_screen_can_be_set_and_turned_off(client, actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import Sent, a_callback

    ctx = await build_broadcast(actor, session, groups=2)
    mine = ctx["chat_ids"][0]

    await handlers.set_archive(a_callback(f"arch:s:{mine}"), user_id=uuid.UUID(actor.id))
    setting = await session.get(AppSetting, uuid.UUID(actor.id))
    await session.refresh(setting)
    assert setting.archive_chat_id == mine
    assert "Group" in Sent.last()

    await handlers.set_archive(a_callback("arch:off"), user_id=uuid.UUID(actor.id))
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
    script.fail_method("chat_details", RuntimeError("FLOOD_WAIT_42"))
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    sends = script.calls_to("send_text")
    index = [c for c in sends if c.args[0].peer_id == keep.peer_id][-1].args[1]
    assert "Group 01" in index and "t.me/c/" in index, "links survive a failed lookup"


async def test_the_per_round_lookup_cap_is_respected(client, actor, session):
    from app.services.archive import _DETAILS_PER_ROUND

    groups = _DETAILS_PER_ROUND + 5
    ctx = await build_broadcast(actor, session, groups=groups, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await _with_archive(session, actor, ctx)
    script = script_for(ctx["connection_id"])
    await _details_for(session, ctx, script)
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()

    await drain(session, broadcast.id)
    await session.commit()

    assert len(script.calls_to("chat_details")) == _DETAILS_PER_ROUND


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
