"""Clearing out groups that will not accept a post.

Nothing here happens automatically, and that is the design: a refusal can be a
fact about the group or a fact about this minute, and only the customer can
decide which ones are worth keeping. What the code owes them is an accurate
list, a clear reason for each, and no surprises about what "remove" means.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.adminbot import views
from app.db.models import Broadcast, BroadcastTarget, JobStatus, TelegramChat
from app.domain import reasons
from app.repositories import broadcasts as broadcast_repo
from app.repositories import chats as chat_repo
from app.services import broadcast as broadcast_service
from tests.integration.test_broadcast import build_broadcast, drain


async def _refuse(session, chat_id, code: str):
    chat = await session.get(TelegramChat, chat_id)
    await chat_repo.set_access(
        session,
        chat=chat,
        can_read_source=True,
        source_reason_code="ok",
        can_post_destination=False,
        destination_reason_code=code,
        check_source="pre_delivery",
    )
    return chat


# --------------------------------------------------------------------------- #
# Finding them
# --------------------------------------------------------------------------- #
async def test_refusing_groups_are_listed_with_their_reason(client, actor, session):
    ctx = await build_broadcast(actor, session, groups=3)
    await _refuse(session, ctx["chat_ids"][0], reasons.WRITE_FORBIDDEN)
    await _refuse(session, ctx["chat_ids"][1], reasons.NOT_A_MEMBER)
    await session.commit()

    refusing = await chat_repo.refusing(session, user_id=uuid.UUID(actor.id))
    assert {c.id for c in refusing} == set(ctx["chat_ids"][:2])
    assert {c.access.destination_reason_code for c in refusing} == {
        reasons.WRITE_FORBIDDEN,
        reasons.NOT_A_MEMBER,
    }


async def test_another_accounts_refusals_are_not_yours(client, actor, other_actor, session):
    theirs = await build_broadcast(other_actor, session, groups=1)
    await _refuse(session, theirs["chat_ids"][0], reasons.WRITE_FORBIDDEN)
    await session.commit()

    assert await chat_repo.refusing(session, user_id=uuid.UUID(actor.id)) == []


# --------------------------------------------------------------------------- #
# What removing does, and does not
# --------------------------------------------------------------------------- #
async def test_removing_takes_the_group_out_of_every_ad(client, actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback

    ctx = await build_broadcast(actor, session, groups=3)
    doomed = ctx["chat_ids"][0]
    await _refuse(session, doomed, reasons.WRITE_FORBIDDEN)
    await session.commit()

    await handlers.drop_dead_groups(a_callback(f"dead:one:{doomed}"), user_id=uuid.UUID(actor.id))

    remaining = await broadcast_repo.target_chat_ids(session, broadcast_id=ctx["broadcast_id"])
    assert doomed not in remaining
    assert len(remaining) == 2


async def test_the_account_stays_in_the_group(client, actor, state, session):
    """The one thing that must never happen: this is not "leave group"."""
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback

    ctx = await build_broadcast(actor, session, groups=2)
    doomed = ctx["chat_ids"][0]
    await _refuse(session, doomed, reasons.WRITE_FORBIDDEN)
    await session.commit()

    await handlers.drop_dead_groups(a_callback(f"dead:one:{doomed}"), user_id=uuid.UUID(actor.id))

    chat = await session.get(TelegramChat, doomed)
    await session.refresh(chat)
    assert chat is not None and chat.is_active, "still synced, still joined"


async def test_a_delivered_target_is_kept_as_a_record(client, actor, state, session):
    """A group that once received the ad and later broke keeps its row: the
    archive index is built from successful deliveries, and deleting them would
    erase something that genuinely happened."""
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback

    ctx = await build_broadcast(actor, session, groups=2, delay_ms=0)
    broadcast = await session.get(Broadcast, ctx["broadcast_id"])
    await broadcast_service.queue(session, broadcast=broadcast)
    await session.commit()
    await drain(session, broadcast.id)
    await session.commit()

    # It worked, then the group shut its doors.
    delivered = ctx["chat_ids"][0]
    await _refuse(session, delivered, reasons.WRITE_FORBIDDEN)
    await session.commit()

    await handlers.drop_dead_groups(
        a_callback(f"dead:one:{delivered}"), user_id=uuid.UUID(actor.id)
    )

    kept = (
        await session.execute(
            select(BroadcastTarget).where(
                BroadcastTarget.broadcast_id == broadcast.id,
                BroadcastTarget.chat_id == delivered,
            )
        )
    ).scalar_one()
    assert kept.status is JobStatus.succeeded


async def test_remove_all_spares_the_temporary_ones(client, actor, state, session):
    """A slow-mode wait clears by itself. Sweeping it up would throw away a
    perfectly good group over a condition that lasts a minute."""
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import a_callback

    ctx = await build_broadcast(actor, session, groups=3)
    shut, waiting = ctx["chat_ids"][0], ctx["chat_ids"][1]
    await _refuse(session, shut, reasons.WRITE_FORBIDDEN)
    await _refuse(session, waiting, reasons.SLOWMODE_WAIT)
    await session.commit()

    await handlers.drop_dead_groups(a_callback("dead:all"), user_id=uuid.UUID(actor.id))

    remaining = await broadcast_repo.target_chat_ids(session, broadcast_id=ctx["broadcast_id"])
    assert shut not in remaining
    assert waiting in remaining, "a passing condition is not a dead group"


async def test_another_accounts_group_cannot_be_dropped(client, actor, other_actor, state, session):
    from app.adminbot import handlers
    from tests.integration.test_bot_flows import Sent, a_callback

    theirs = await build_broadcast(other_actor, session, groups=1)
    await _refuse(session, theirs["chat_ids"][0], reasons.WRITE_FORBIDDEN)
    await session.commit()

    await handlers.drop_dead_groups(
        a_callback(f"dead:one:{theirs['chat_ids'][0]}"), user_id=uuid.UUID(actor.id)
    )

    remaining = await broadcast_repo.target_chat_ids(session, broadcast_id=theirs["broadcast_id"])
    assert remaining == theirs["chat_ids"], "untouched"
    assert any("Nothing to remove" in alert for alert in Sent.alerts)


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #
async def test_the_screen_separates_shut_from_waiting(client, actor, session):
    from tests.integration.test_bot_flows import (
        assert_keyboard_is_sendable,
        assert_valid_markdown_v2,
    )

    ctx = await build_broadcast(actor, session, groups=2)
    await _refuse(session, ctx["chat_ids"][0], reasons.WRITE_FORBIDDEN)
    await _refuse(session, ctx["chat_ids"][1], reasons.SLOWMODE_WAIT)
    await session.commit()

    refusing = await chat_repo.refusing(session, user_id=uuid.UUID(actor.id))
    screen = views.dead_groups(chats=refusing, page=0, temporary=chat_repo.TEMPORARY_REFUSALS)

    assert "may clear by itself" in screen.text
    assert "does not leave the group" in screen.text
    assert "Remove all 1 shut ones" in " ".join(
        b.text for row in screen.keyboard.inline_keyboard for b in row
    )
    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)


def test_the_screen_says_so_when_there_is_nothing_wrong():
    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    screen = views.dead_groups(chats=[], page=0, temporary=frozenset())
    assert "every group you have chosen accepts" in screen.text
    assert_valid_markdown_v2(screen.text)


async def test_a_hostile_group_title_cannot_break_the_screen(client, actor, session):
    from types import SimpleNamespace

    from tests.integration.test_bot_flows import assert_valid_markdown_v2

    chat = SimpleNamespace(
        id=uuid.uuid4(),
        title="_evil* [x](y) #1!",
        access=SimpleNamespace(destination_reason_code=reasons.WRITE_FORBIDDEN),
    )
    screen = views.dead_groups(chats=[chat], page=0, temporary=frozenset())
    assert_valid_markdown_v2(screen.text)
