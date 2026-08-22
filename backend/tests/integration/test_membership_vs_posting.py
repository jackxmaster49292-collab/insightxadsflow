"""Being joined to a chat is not the same as being able to post in it.

This is the single most common wrong assumption about a large forwarding setup:
"I'm in 500 groups, so I can post to 500 groups." Telegram says otherwise, and
the reasons are per-chat. These tests pin down what the panel reports for each
case so an operator sees the truth before activating a rule, not after.
"""

from __future__ import annotations

import uuid

from app.adapters.base import AccessReport, ChatRef, DiscoveredChat, PeerKind
from app.domain import reasons
from tests.conftest import connect_bot, script_for, sync_with_chats

BASE_PEER = -1003_000_000


def chat(index: int, title: str, kind: str = "supergroup", protected: bool = False):
    return DiscoveredChat(
        ref=ChatRef(PeerKind.channel, BASE_PEER - index),
        title=title,
        chat_kind=kind,
        username=None,
        is_public=False,
        has_protected_content=protected,
    )


async def test_joined_does_not_mean_postable(client, actor):
    """A realistic mix: member everywhere, but only some are usable as
    destinations — each with a reason the customer can act on."""
    connection_id = await connect_bot(actor, "Account")

    catalogue = [
        (chat(0, "Group where I can post"), None),
        (chat(1, "Group where only admins post"), reasons.WRITE_FORBIDDEN),
        (chat(2, "Channel I only subscribe to", kind="channel"), reasons.ADMIN_REQUIRED),
        (chat(3, "Group I was muted in"), reasons.BANNED),
        (chat(4, "Group with media disabled"), reasons.SEND_MEDIA_FORBIDDEN),
        (chat(5, "Forum topic that is closed"), reasons.TOPIC_CLOSED),
        (chat(6, "Channel that went private"), reasons.CHANNEL_PRIVATE),
    ]

    script = script_for(connection_id)
    for discovered, denial in catalogue:
        if denial is not None:
            script.destination_allowed[discovered.ref.key] = AccessReport(False, denial)

    await sync_with_chats(actor, connection_id, [c for c, _ in catalogue])

    chats = {c["title"]: c for c in (await actor.get("/telegram/chats?limit=200")).json()}
    assert len(chats) == len(catalogue)

    # Member of all seven; usable as a destination in exactly one.
    postable = [c for c in chats.values() if c["destination_eligible"]]
    assert len(postable) == 1
    assert postable[0]["title"] == "Group where I can post"

    # Every refusal explains itself in plain language.
    for title, expected in [
        ("Group where only admins post", reasons.WRITE_FORBIDDEN),
        ("Channel I only subscribe to", reasons.ADMIN_REQUIRED),
        ("Group I was muted in", reasons.BANNED),
        ("Group with media disabled", reasons.SEND_MEDIA_FORBIDDEN),
        ("Forum topic that is closed", reasons.TOPIC_CLOSED),
        ("Channel that went private", reasons.CHANNEL_PRIVATE),
    ]:
        row = chats[title]
        assert row["destination_eligible"] is False
        assert row["destination_reason_code"] == expected
        assert row["destination_reason_text"], f"{title} has no explanation"


async def test_an_ineligible_chat_cannot_be_put_in_a_rule(client, actor):
    """The check is not advisory — a rule naming an unpostable chat is refused,
    with the specific chat and reason attached."""
    connection_id = await connect_bot(actor, "Account")

    source = chat(0, "Source", kind="channel")
    blocked = chat(1, "Read-only group")

    script = script_for(connection_id)
    script.destination_allowed[blocked.ref.key] = AccessReport(False, reasons.WRITE_FORBIDDEN)
    await sync_with_chats(actor, connection_id, [source, blocked])

    chats = {c["title"]: c for c in (await actor.get("/telegram/chats?limit=200")).json()}
    response = await actor.post(
        "/forwarding-rules",
        {
            "name": "Doomed",
            "connection_id": connection_id,
            "source_chat_ids": [chats["Source"]["id"]],
            "destination_chat_ids": [chats["Read-only group"]["id"]],
        },
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "destination_not_eligible"
    assert body["details"]["reason_code"] == reasons.WRITE_FORBIDDEN


async def test_permission_lost_after_activation_is_caught_before_sending(client, actor, session):
    """Permissions change. A group can go read-only a week after the rule was
    built, and the delivery must stop rather than fail noisily at Telegram."""
    from sqlalchemy import select

    from app.adapters.base import InboundMessage, MediaType
    from app.db.models import ForwardingJob, JobStatus
    from app.services.dispatch import dispatch_inbound
    from tests.integration.test_forwarding import run_job

    connection_id = await connect_bot(actor, "Account")
    source, destination = chat(0, "Source", kind="channel"), chat(1, "Partner group")
    await sync_with_chats(actor, connection_id, [source, destination])

    chats = {c["title"]: c for c in (await actor.get("/telegram/chats?limit=200")).json()}
    created = await actor.post(
        "/forwarding-rules",
        {
            "name": "Partners",
            "connection_id": connection_id,
            "source_chat_ids": [chats["Source"]["id"]],
            "destination_chat_ids": [chats["Partner group"]["id"]],
        },
    )
    rule_id = created.json()["id"]
    await actor.post(f"/forwarding-rules/{rule_id}/activate", idem=uuid.uuid4().hex)

    await dispatch_inbound(
        session,
        connection_id=uuid.UUID(connection_id),
        message=InboundMessage(
            source=ChatRef(PeerKind.channel, source.ref.peer_id),
            message_ids=[100],
            media_type=MediaType.text,
            text="hello",
        ),
    )
    await session.commit()

    # The group goes read-only after the job was already queued.
    script = script_for(connection_id)
    script.destination_allowed[destination.ref.key] = AccessReport(False, reasons.WRITE_FORBIDDEN)

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
    assert outcome.reason_code == reasons.WRITE_FORBIDDEN
    # No attempt was made — the revalidation happens before the send.
    assert script.calls_to("forward_message") == []

    # And the chat is now marked ineligible in the panel, so the operator sees it.
    refreshed = {c["title"]: c for c in (await actor.get("/telegram/chats?limit=200")).json()}
    assert refreshed["Partner group"]["destination_eligible"] is False


async def test_a_protected_source_blocks_forwarding_everywhere(client, actor):
    """Content protection sits on the *source*. If it is on, no destination can
    receive it — however many groups you are in."""
    connection_id = await connect_bot(actor, "Account")
    protected = chat(0, "Protected channel", kind="channel", protected=True)
    destination = chat(1, "Partner group")
    await sync_with_chats(actor, connection_id, [protected, destination])

    chats = {c["title"]: c for c in (await actor.get("/telegram/chats?limit=200")).json()}
    assert chats["Protected channel"]["source_eligible"] is False
    assert chats["Protected channel"]["source_reason_code"] == reasons.PROTECTED_CONTENT

    response = await actor.post(
        "/forwarding-rules",
        {
            "name": "Cannot work",
            "connection_id": connection_id,
            "source_chat_ids": [chats["Protected channel"]["id"]],
            "destination_chat_ids": [chats["Partner group"]["id"]],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "protected_source"
