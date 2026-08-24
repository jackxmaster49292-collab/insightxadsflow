"""Auto-reply: answering people who wrote in, and never anyone else.

The property under test throughout this file is a negative one — that no
automatic reply can reach someone who did not message the account first. Every
test here either exercises that boundary, the cooldown that keeps a single
answer from becoming repeat messaging, or the newer condition: replies only go
out while the account is advertising.

That last one is why ``build`` creates a running ad. An account that answers
strangers at three in the morning with no ad in sight is behaving like a bot;
one that answers while it is advertising is answering the people who saw the
ad.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.adapters.base import ChatRef, PeerKind
from app.adapters.errors import AdapterError, ErrorClass
from app.db.models import AutoReply, AutoReplyLog, ConnectionStatus, TelegramConnection
from app.domain import reasons
from app.repositories import autoreply as autoreply_repo
from app.services import autoreply as autoreply_service
from app.services import connections as connection_service
from tests.conftest import connect_bot, script_for

WRITER_ID = 500_100_200
OTHER_WRITER_ID = 500_100_201
GROUP_ID = -1002000


async def build(
    actor,
    session,
    *,
    enabled: bool = True,
    body: str = "Thanks for writing!",
    advertising: bool = True,
) -> dict:
    connection_id = await connect_bot(actor)
    connection = await session.get(TelegramConnection, uuid.UUID(connection_id))
    connection.status = ConnectionStatus.active

    await autoreply_repo.upsert(
        session,
        user_id=uuid.UUID(actor.id),
        connection_id=connection.id,
        body_text=body,
    )
    if enabled:
        await autoreply_repo.upsert(
            session, user_id=uuid.UUID(actor.id), connection_id=connection.id, enabled=True
        )
    if advertising:
        await _running_ad(session, actor, connection)
    await session.commit()
    return {"connection_id": connection_id, "connection": connection}


async def _running_ad(session, actor, connection):
    """An ad in flight, which is what auto-reply now keys off."""
    from app.db.models import BroadcastStatus
    from app.repositories import broadcasts as broadcast_repo

    broadcast = await broadcast_repo.create(
        session,
        user_id=uuid.UUID(actor.id),
        connection_id=connection.id,
        name="Running",
        delay_ms=0,
    )
    broadcast.status = BroadcastStatus.sending
    await session.flush()
    return broadcast


async def incoming(session, ctx, *, peer_id: int = WRITER_ID, kind: PeerKind = PeerKind.user):
    adapter = await connection_service.adapter_for(session, ctx["connection"])
    return await autoreply_service.handle_incoming(
        session,
        connection=ctx["connection"],
        adapter=adapter,
        sender=ChatRef(kind, peer_id),
    )


# --------------------------------------------------------------------------- #
# The boundary: only people who wrote first
# --------------------------------------------------------------------------- #
async def test_someone_who_writes_in_gets_an_answer(client, actor, session):
    ctx = await build(actor, session)
    decision = await incoming(session, ctx)

    assert decision.sent
    assert decision.reason_code == reasons.AUTO_REPLY_SENT
    sends = script_for(ctx["connection_id"]).calls_to("send_text")
    assert len(sends) == 1
    assert sends[0].args[0].peer_id == WRITER_ID, "answered the writer, nobody else"
    assert sends[0].args[1] == "Thanks for writing!"


async def test_a_group_message_never_produces_a_reply(client, actor, session):
    """Auto-reply exists for private conversations someone else started.
    Answering in a group would be posting where nobody asked us to."""
    ctx = await build(actor, session)

    for kind in (PeerKind.chat, PeerKind.channel):
        decision = await incoming(session, ctx, peer_id=GROUP_ID, kind=kind)
        assert not decision.sent
        assert decision.reason_code == reasons.AUTO_REPLY_NOT_PRIVATE

    assert not script_for(ctx["connection_id"]).calls_to("send_text")


async def test_nothing_is_sent_while_it_is_switched_off(client, actor, session):
    ctx = await build(actor, session, enabled=False)
    decision = await incoming(session, ctx)

    assert not decision.sent
    assert decision.reason_code == reasons.AUTO_REPLY_DISABLED
    assert not script_for(ctx["connection_id"]).calls_to("send_text")


async def test_a_connection_with_no_auto_reply_configured_stays_silent(client, actor, session):
    connection_id = await connect_bot(actor)
    connection = await session.get(TelegramConnection, uuid.UUID(connection_id))
    connection.status = ConnectionStatus.active
    await session.commit()

    adapter = await connection_service.adapter_for(session, connection)
    decision = await autoreply_service.handle_incoming(
        session,
        connection=connection,
        adapter=adapter,
        sender=ChatRef(PeerKind.user, WRITER_ID),
    )
    assert not decision.sent
    assert not script_for(connection_id).calls_to("send_text")


async def test_a_disconnected_connection_does_not_reply(client, actor, session):
    ctx = await build(actor, session)
    ctx["connection"].status = ConnectionStatus.disconnected
    await session.commit()

    decision = await incoming(session, ctx)
    assert not decision.sent
    assert decision.reason_code == reasons.CONNECTION_DISCONNECTED


async def test_turning_it_on_without_text_is_refused(client, actor, session):
    """Enabling with nothing to say would send an empty message, which Telegram
    rejects anyway. Refuse it where the reason is visible."""
    connection_id = await connect_bot(actor)
    with pytest.raises(ValueError) as exc:
        await autoreply_repo.upsert(
            session,
            user_id=uuid.UUID(actor.id),
            connection_id=uuid.UUID(connection_id),
            enabled=True,
        )
    assert "Write the reply text" in str(exc.value)


async def test_blank_text_stops_a_reply_even_if_enabled(client, actor, session):
    """Belt and braces for a row that got into a bad state some other way."""
    ctx = await build(actor, session)
    reply = await autoreply_repo.get_for_connection(session, connection_id=ctx["connection"].id)
    reply.body_text = "   "
    await session.commit()

    decision = await incoming(session, ctx)
    assert not decision.sent
    assert decision.reason_code == reasons.AUTO_REPLY_DISABLED


# --------------------------------------------------------------------------- #
# The cooldown
# --------------------------------------------------------------------------- #
async def test_the_same_person_is_not_answered_twice(client, actor, session):
    """One answer per person per window. A second is repeat messaging."""
    ctx = await build(actor, session)

    first = await incoming(session, ctx)
    second = await incoming(session, ctx)

    assert first.sent
    assert not second.sent
    assert second.reason_code == reasons.AUTO_REPLY_COOLDOWN
    assert len(script_for(ctx["connection_id"]).calls_to("send_text")) == 1


async def test_different_people_each_get_one(client, actor, session):
    ctx = await build(actor, session)

    assert (await incoming(session, ctx, peer_id=WRITER_ID)).sent
    assert (await incoming(session, ctx, peer_id=OTHER_WRITER_ID)).sent

    answered = {c.args[0].peer_id for c in script_for(ctx["connection_id"]).calls_to("send_text")}
    assert answered == {WRITER_ID, OTHER_WRITER_ID}


async def test_the_same_person_is_answered_again_once_the_wait_has_passed(client, actor, session):
    ctx = await build(actor, session)
    await incoming(session, ctx)

    entry = (
        await session.execute(select(AutoReplyLog).where(AutoReplyLog.peer_id == WRITER_ID))
    ).scalar_one()
    entry.replied_at = datetime.now(UTC) - timedelta(days=2)
    await session.commit()

    assert (await incoming(session, ctx)).sent
    assert len(script_for(ctx["connection_id"]).calls_to("send_text")) == 2

    await session.refresh(entry)
    assert entry.reply_count == 2


async def test_the_record_survives_a_restart(client, actor, session):
    """Held in the database rather than a cache: an empty cache after a restart
    would answer everyone a second time."""
    ctx = await build(actor, session)
    await incoming(session, ctx)
    await session.commit()

    rows = (await session.execute(select(AutoReplyLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].peer_id == WRITER_ID
    assert rows[0].connection_id == ctx["connection"].id


async def test_a_failed_send_does_not_consume_the_person_s_slot(client, actor, session):
    """Otherwise a transient failure costs them the whole cooldown for an answer
    that never arrived."""
    ctx = await build(actor, session)
    script_for(ctx["connection_id"]).fail_delivery(
        ChatRef(PeerKind.user, WRITER_ID),
        AdapterError(reasons.NETWORK_ERROR, ErrorClass.TRANSIENT),
    )

    failed = await incoming(session, ctx)
    assert not failed.sent

    retried = await incoming(session, ctx)
    assert retried.sent, "the next message from the same person must be answered"


async def test_a_failed_repeat_send_also_releases_the_slot(client, actor, session):
    """The harder case: the person already has a log row, so releasing cannot
    simply delete it."""
    ctx = await build(actor, session)
    await incoming(session, ctx)

    entry = (
        await session.execute(select(AutoReplyLog).where(AutoReplyLog.peer_id == WRITER_ID))
    ).scalar_one()
    entry.replied_at = datetime.now(UTC) - timedelta(days=2)
    await session.commit()

    script_for(ctx["connection_id"]).fail_delivery(
        ChatRef(PeerKind.user, WRITER_ID),
        AdapterError(reasons.NETWORK_ERROR, ErrorClass.TRANSIENT),
    )
    assert not (await incoming(session, ctx)).sent
    assert (await incoming(session, ctx)).sent, "still eligible after the failure"


async def test_the_cooldown_is_per_connection(client, actor, session):
    """Two connections are two conversations; one must not silence the other."""
    from app.db.models import ConnectionKind
    from app.repositories import connections as connection_repo

    first = await build(actor, session)
    # Built directly rather than through the API: connecting the same mock bot
    # twice would collide on (user, kind, telegram_account_id), which is correct
    # product behaviour and not what this test is about.
    second_connection = await connection_repo.create(
        session,
        user_id=uuid.UUID(actor.id),
        kind=ConnectionKind.bot,
        label="Second",
        status=ConnectionStatus.active,
    )
    second_connection.telegram_account_id = 777_000_222
    await session.flush()
    second_id = str(second_connection.id)
    await autoreply_repo.upsert(
        session,
        user_id=uuid.UUID(actor.id),
        connection_id=second_connection.id,
        body_text="Hello from the other one",
    )
    await autoreply_repo.upsert(
        session, user_id=uuid.UUID(actor.id), connection_id=second_connection.id, enabled=True
    )
    await session.commit()

    assert (await incoming(session, first)).sent
    assert (
        await incoming(session, {"connection_id": second_id, "connection": second_connection})
    ).sent


async def test_the_cooldown_can_be_changed(client, actor, session):
    ctx = await build(actor, session)
    await autoreply_repo.upsert(
        session,
        user_id=uuid.UUID(actor.id),
        connection_id=ctx["connection"].id,
        cooldown_s=3600,
    )
    await session.commit()

    reply = await autoreply_repo.get_for_connection(session, connection_id=ctx["connection"].id)
    assert reply.cooldown_s == 3600


async def test_purging_the_log_leaves_in_force_entries_alone(client, actor, session):
    """Purging an entry that is still inside its cooldown would let the same
    person be answered twice."""
    ctx = await build(actor, session)
    await incoming(session, ctx)
    await session.commit()

    removed = await autoreply_repo.purge_log(session, older_than_days=30)
    assert removed == 0
    assert (await session.execute(select(AutoReplyLog))).scalars().all()


# --------------------------------------------------------------------------- #
# Shape of the stored settings
# --------------------------------------------------------------------------- #
async def test_one_auto_reply_per_connection(client, actor, session):
    ctx = await build(actor, session)
    await autoreply_repo.upsert(
        session,
        user_id=uuid.UUID(actor.id),
        connection_id=ctx["connection"].id,
        body_text="Changed my mind",
    )
    await session.commit()

    rows = (await session.execute(select(AutoReply))).scalars().all()
    assert len(rows) == 1
    assert rows[0].body_text == "Changed my mind"


async def test_answering_counts_up(client, actor, session):
    ctx = await build(actor, session)
    await incoming(session, ctx, peer_id=WRITER_ID)
    await incoming(session, ctx, peer_id=OTHER_WRITER_ID)
    await session.commit()

    reply = await autoreply_repo.get_for_connection(session, connection_id=ctx["connection"].id)
    assert reply.sent_count == 2


def test_there_is_no_way_to_send_to_a_list():
    """A structural check on the module's surface: the only entry point takes a
    single sender that already messaged us. Adding a bulk variant here would be
    a way to message people who never wrote in."""
    public = [
        name
        for name in dir(autoreply_service)
        if not name.startswith("_") and callable(getattr(autoreply_service, name))
    ]
    assert sorted(n for n in public if n in {"handle_incoming", "ReplyDecision"}) == [
        "ReplyDecision",
        "handle_incoming",
    ]
    for name in public:
        assert "broadcast" not in name.lower()
        assert "all" not in name.lower().split("_")


# --------------------------------------------------------------------------- #
# Only while advertising
# --------------------------------------------------------------------------- #
async def test_no_ad_means_no_reply(client, actor, session):
    """The account is not advertising, so it stays quiet — which is the whole
    point of tying the two together."""
    ctx = await build(actor, session, advertising=False)
    decision = await incoming(session, ctx)

    assert not decision.sent
    assert decision.reason_code == reasons.AUTO_REPLY_NOT_ADVERTISING
    assert script_for(ctx["connection_id"]).calls_to("send_text") == []


async def test_a_repeating_ad_keeps_replies_alive_between_rounds(client, actor, session):
    """A repeating ad stays ``sending`` while it waits for its next round, and
    people write in during that gap — most of them, in fact."""
    from app.db.models import Broadcast, BroadcastStatus

    ctx = await build(actor, session)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    broadcast.repeat_every_s = 7200
    broadcast.next_run_at = datetime.now(UTC) + timedelta(hours=2)
    assert broadcast.status is BroadcastStatus.sending
    await session.commit()

    assert (await incoming(session, ctx)).sent


async def test_replies_continue_for_a_while_after_the_ad_finishes(client, actor, session):
    """A one-shot ad is over in a minute; the people who saw it are not."""
    from app.config import get_settings
    from app.db.models import Broadcast, BroadcastStatus

    ctx = await build(actor, session)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    broadcast.status = BroadcastStatus.completed
    broadcast.completed_at = datetime.now(UTC) - timedelta(
        hours=get_settings().auto_reply_after_ad_hours - 1
    )
    await session.commit()

    assert (await incoming(session, ctx)).sent


async def test_replies_stop_once_the_window_has_passed(client, actor, session):
    from app.config import get_settings
    from app.db.models import Broadcast, BroadcastStatus

    ctx = await build(actor, session)
    broadcast = (await session.execute(select(Broadcast))).scalar_one()
    broadcast.status = BroadcastStatus.completed
    broadcast.completed_at = datetime.now(UTC) - timedelta(
        hours=get_settings().auto_reply_after_ad_hours + 1
    )
    await session.commit()

    decision = await incoming(session, ctx)
    assert not decision.sent
    assert decision.reason_code == reasons.AUTO_REPLY_NOT_ADVERTISING


async def test_another_accounts_ad_does_not_unlock_your_replies(
    client, actor, other_actor, session
):
    """The window is per account. Someone else advertising says nothing about
    whether people are writing to you."""
    from app.db.models import BroadcastStatus, TelegramConnection
    from app.repositories import broadcasts as broadcast_repo

    ctx = await build(actor, session, advertising=False)

    theirs_id = await connect_bot(other_actor)
    theirs = await session.get(TelegramConnection, uuid.UUID(theirs_id))
    running = await broadcast_repo.create(
        session,
        user_id=uuid.UUID(other_actor.id),
        connection_id=theirs.id,
        name="Theirs",
        delay_ms=0,
    )
    running.status = BroadcastStatus.sending
    await session.commit()

    assert not (await incoming(session, ctx)).sent
