"""Private groups.

Delivery-wise a private group behaves exactly like a public one. Two things
genuinely differ, and both are easy to get wrong:

1. **access_hash** — a private peer cannot be addressed without one, and it is
   per-account. Losing it means the group becomes unreachable after a restart.
2. **Migration** — upgrading a basic group to a supergroup gives it a *new*
   Telegram identifier. The old one dies, and the failure must say so rather
   than looking like a permission problem.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.adapters.base import AccessReport, ChatRef, DiscoveredChat, PeerKind
from app.db.models import TelegramChat
from app.domain import reasons
from app.repositories import chats as chat_repo
from tests.conftest import connect_bot, script_for, sync_with_chats

BASE = -1004_000_000


def group(index: int, title: str, *, public: bool, access_hash: int | None = None):
    return DiscoveredChat(
        ref=ChatRef(PeerKind.channel, BASE - index, access_hash=access_hash),
        title=title,
        chat_kind="supergroup",
        username=f"public_{index}" if public else None,
        is_public=public,
        has_protected_content=False,
    )


async def test_private_and_public_groups_are_equally_postable(client, actor):
    connection_id = await connect_bot(actor, "Account")
    await sync_with_chats(
        actor,
        connection_id,
        [
            group(0, "Private group", public=False, access_hash=555_000_111),
            group(1, "Public group", public=True),
        ],
    )

    chats = {c["title"]: c for c in (await actor.get("/telegram/chats?limit=200")).json()}
    assert chats["Private group"]["is_public"] is False
    assert chats["Public group"]["is_public"] is True

    # The thing that actually matters is identical for both.
    for title in ("Private group", "Public group"):
        assert chats[title]["source_eligible"] is True
        assert chats[title]["destination_eligible"] is True


async def test_access_hash_is_stored_encrypted_and_survives_a_reload(client, actor, session):
    """Without the access_hash a private group is unreachable after a restart.
    It is account-specific, so it is stored per connection and encrypted."""
    connection_id = await connect_bot(actor, "Account")
    await sync_with_chats(
        actor, connection_id, [group(0, "Private group", public=False, access_hash=555_000_111)]
    )

    row = (
        await session.execute(
            select(TelegramChat).where(TelegramChat.connection_id == uuid.UUID(connection_id))
        )
    ).scalar_one()

    assert row.access_hash_ciphertext is not None
    assert b"555000111" not in row.access_hash_ciphertext, "access_hash stored in clear"
    assert row.access_hash_key_version == 1

    # It round-trips, so the peer stays addressable across restarts.
    assert chat_repo.read_access_hash(row) == 555_000_111
    assert chat_repo.to_ref(row).access_hash == 555_000_111


async def test_access_hash_is_never_exposed_through_the_api(client, actor):
    connection_id = await connect_bot(actor, "Account")
    await sync_with_chats(
        actor, connection_id, [group(0, "Private group", public=False, access_hash=555_000_111)]
    )

    body = (await actor.get("/telegram/chats?limit=200")).text
    assert "555000111" not in body
    assert "access_hash" not in body


async def test_a_public_group_needs_no_access_hash(client, actor, session):
    """Public groups resolve by username, so a missing hash is not an error."""
    connection_id = await connect_bot(actor, "Account")
    await sync_with_chats(actor, connection_id, [group(1, "Public group", public=True)])

    row = (
        await session.execute(
            select(TelegramChat).where(TelegramChat.connection_id == uuid.UUID(connection_id))
        )
    ).scalar_one()
    assert row.access_hash_ciphertext is None
    assert chat_repo.read_access_hash(row) is None


async def test_a_migrated_group_reports_what_actually_happened(client, actor, session):
    """Upgrading a basic group to a supergroup changes its id. The old peer must
    fail with an explanation the customer can act on — not a generic permission
    error that sends them hunting through Telegram settings."""
    from app.adapters.base import InboundMessage, MediaType
    from app.db.models import ForwardingJob, JobStatus
    from app.services.dispatch import dispatch_inbound
    from tests.integration.test_forwarding import run_job

    connection_id = await connect_bot(actor, "Account")
    source = group(0, "Source", public=True)
    destination = group(1, "Private group", public=False, access_hash=777_000_222)
    await sync_with_chats(actor, connection_id, [source, destination])

    chats = {c["title"]: c for c in (await actor.get("/telegram/chats?limit=200")).json()}
    created = await actor.post(
        "/forwarding-rules",
        {
            "name": "To private group",
            "connection_id": connection_id,
            "source_chat_ids": [chats["Source"]["id"]],
            "destination_chat_ids": [chats["Private group"]["id"]],
        },
    )
    rule_id = created.json()["id"]
    await actor.post(f"/forwarding-rules/{rule_id}/activate", idem=uuid.uuid4().hex)

    await dispatch_inbound(
        session,
        connection_id=uuid.UUID(connection_id),
        message=InboundMessage(
            source=ChatRef(PeerKind.channel, source.ref.peer_id),
            message_ids=[500],
            media_type=MediaType.text,
            text="hello",
        ),
    )
    await session.commit()

    # The group is upgraded between queueing and delivery.
    script = script_for(connection_id)
    script.destination_allowed[destination.ref.key] = AccessReport(False, reasons.CHAT_MIGRATED)

    job = (
        (
            await session.execute(
                select(ForwardingJob).where(ForwardingJob.rule_id == uuid.UUID(rule_id))
            )
        )
        .scalars()
        .one()
    )
    outcome = await run_job(session, job)

    assert outcome.status is JobStatus.skipped
    assert outcome.reason_code == reasons.CHAT_MIGRATED

    events = (await actor.get(f"/forwarding-rules/{rule_id}/events")).json()
    detail = events[0]["reason_text"]
    assert "upgraded to a supergroup" in detail
    assert "new Telegram identifier" in detail
    assert "Synchronize" in detail


def test_migration_errors_are_never_retried():
    """Retrying a dead identifier can never succeed, so it must not consume the
    retry budget."""
    from app.adapters.errors import ErrorClass, classify_error

    class ChatMigratedError(Exception):
        pass

    for exc in (
        ChatMigratedError("migrated"),
        Exception("Bad Request: group chat was upgraded to a supergroup chat"),
        Exception("Bad Request: CHAT_MIGRATED"),
    ):
        result = classify_error(exc)
        assert result.code == reasons.CHAT_MIGRATED
        assert result.error_class is ErrorClass.PERMISSION
        assert not result.retryable
