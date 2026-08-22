from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models import ConnectionStatus, TelegramChat, TelegramConnection
from app.domain import reasons
from tests.conftest import chat_ref, connect_bot, discovered, script_for, sync_with_chats


async def test_bot_connection_stores_the_token_encrypted(client, actor, session):
    connection_id = await connect_bot(actor)

    row = (
        await session.execute(
            select(TelegramConnection).where(TelegramConnection.id == uuid.UUID(connection_id))
        )
    ).scalar_one()

    raw = b"123456789:AAEtestTokenValueThatIsLongEnough00"
    assert row.bot_token_ciphertext is not None
    assert raw not in row.bot_token_ciphertext
    assert row.bot_token_key_version == 1
    assert row.status is ConnectionStatus.active


async def test_connection_response_never_exposes_the_token(client, actor):
    connection_id = await connect_bot(actor)
    body = (await actor.get(f"/telegram/connections/{connection_id}")).json()
    serialized = str(body)
    assert "AAEtestToken" not in serialized
    assert "bot_token" not in body
    assert "ciphertext" not in serialized


async def test_capabilities_explain_what_the_connection_can_do(client, actor):
    connection_id = await connect_bot(actor)
    body = (await actor.get(f"/telegram/connections/{connection_id}")).json()
    caps = body["capabilities"]
    # A bot cannot read a channel it only subscribes to, and the UI must say so.
    assert caps["can_read_subscribed_channels"] is False
    assert caps["max_download_bytes"] == 20 * 1024 * 1024
    assert any("cannot list its own chats" in note for note in caps["notes"])


async def test_duplicate_simultaneous_connection_attempts_are_blocked(client, actor):
    await actor.post(
        "/telegram/connections/user/start",
        {"label": "Account", "phone": "+447700900123"},
    )
    second = await actor.post(
        "/telegram/connections/user/start",
        {"label": "Another", "phone": "+447700900124"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "connection_attempt_in_progress"


async def test_phone_number_is_stored_only_as_a_hash(client, actor, session):
    await actor.post(
        "/telegram/connections/user/start", {"label": "Account", "phone": "+447700900123"}
    )
    row = (
        await session.execute(select(TelegramConnection).where(TelegramConnection.kind == "user"))
    ).scalar_one()
    assert row.phone_hash is not None
    assert row.phone_hash != "+447700900123"
    assert len(row.phone_hash) == 64


async def test_sync_is_accepted_without_blocking_and_discovers_chats(client, actor, session):
    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor,
        connection_id,
        [
            discovered(-1001, "Source channel"),
            discovered(-1002, "Destination group", chat_kind="supergroup"),
        ],
    )

    chats = (await actor.get(f"/telegram/chats?connection_id={connection_id}")).json()
    assert {c["title"] for c in chats} == {"Source channel", "Destination group"}
    assert all(c["source_eligible"] for c in chats)
    assert all(c["destination_eligible"] for c in chats)


async def test_peer_ids_are_serialized_as_strings(client, actor):
    connection_id = await connect_bot(actor)
    await sync_with_chats(actor, connection_id, [discovered(-1001234567890123, "Big id")])
    chats = (await actor.get("/telegram/chats")).json()
    assert chats[0]["peer_id"] == "-1001234567890123"
    assert isinstance(chats[0]["peer_id"], str)


async def test_overlapping_peer_ids_across_types_are_distinct_rows(client, actor, session):
    """Telegram's user/chat/channel id sequences overlap, so the same numeric id
    must not collapse into one row."""
    from app.adapters.base import PeerKind
    from app.repositories import chats as chat_repo

    connection_id = uuid.UUID(await connect_bot(actor))
    for kind in (PeerKind.channel, PeerKind.chat, PeerKind.user):
        await chat_repo.upsert_discovered(
            session,
            connection_id=connection_id,
            discovered=discovered(777, f"Peer as {kind.value}"),
        )
        # rebuild with the right peer type
        from app.adapters.base import DiscoveredChat

        await chat_repo.upsert_discovered(
            session,
            connection_id=connection_id,
            discovered=DiscoveredChat(
                ref=chat_ref(777, kind), title=f"Peer as {kind.value}", chat_kind="channel"
            ),
        )
    await session.commit()

    rows = (
        (
            await session.execute(
                select(TelegramChat).where(
                    TelegramChat.connection_id == connection_id, TelegramChat.peer_id == 777
                )
            )
        )
        .scalars()
        .all()
    )
    assert len({r.peer_type for r in rows}) == 3


async def test_protected_source_is_never_source_eligible(client, actor):
    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor, connection_id, [discovered(-1003, "Protected channel", protected=True)]
    )
    chat = (await actor.get("/telegram/chats")).json()[0]
    assert chat["source_eligible"] is False
    assert chat["source_reason_code"] == reasons.PROTECTED_CONTENT
    assert "does not bypass content protection" in chat["source_reason_text"]


async def test_ineligible_destination_reports_a_reason(client, actor):
    connection_id = await connect_bot(actor)
    script = script_for(connection_id)
    script.chats = [discovered(-1004, "Read only")]
    script.allow_destination(chat_ref(-1004), False, reasons.BOT_NOT_ADMIN)
    await sync_with_chats(actor, connection_id, script.chats)

    chat = (await actor.get("/telegram/chats")).json()[0]
    assert chat["destination_eligible"] is False
    assert chat["destination_reason_code"] == reasons.BOT_NOT_ADMIN
    assert "administrator" in chat["destination_reason_text"]


async def test_chats_removed_from_discovery_are_deactivated_not_deleted(client, actor):
    connection_id = await connect_bot(actor)
    await sync_with_chats(actor, connection_id, [discovered(-1005, "Goes away")])
    assert len((await actor.get("/telegram/chats")).json()) == 1

    await sync_with_chats(actor, connection_id, [])
    chats = (await actor.get("/telegram/chats")).json()
    assert len(chats) == 1
    assert chats[0]["is_active"] is False


async def test_disconnect_disables_dependent_rules(client, actor, session):
    from app.db.models import RuleStatus

    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor, connection_id, [discovered(-1006, "Src"), discovered(-1007, "Dst")]
    )
    chats = (await actor.get("/telegram/chats")).json()
    src = next(c for c in chats if c["title"] == "Src")
    dst = next(c for c in chats if c["title"] == "Dst")

    created = await actor.post(
        "/forwarding-rules",
        {
            "name": "R",
            "connection_id": connection_id,
            "source_chat_ids": [src["id"]],
            "destination_chat_ids": [dst["id"]],
        },
    )
    rule_id = created.json()["id"]
    await actor.post(f"/forwarding-rules/{rule_id}/activate", idem=uuid.uuid4().hex)

    from app.db.models import ControlTask
    from app.db.session import session_scope
    from app.worker import process_control_task

    await actor.post(
        f"/telegram/connections/{connection_id}/disconnect",
        {"revoke": True},
        idem=uuid.uuid4().hex,
    )
    async with session_scope() as db:
        task = (
            (await db.execute(select(ControlTask).where(ControlTask.kind == "disconnect")))
            .scalars()
            .first()
        )
    await process_control_task(task)

    detail = (await actor.get(f"/forwarding-rules/{rule_id}")).json()
    assert detail["status"] == RuleStatus.disconnected.value
