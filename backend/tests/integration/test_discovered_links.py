"""Chat links noticed in incoming messages.

The safety argument for this feature is structural and is checked here: it
reads messages the account already receives, it stores the link and the group
it appeared in and nothing else, and it never joins anything. A version that
looked at who posted, or that acted on what it found, would be a different
product.
"""

from __future__ import annotations

import uuid

from app.adapters.base import ChatRef, InboundMessage, LinkPreview, MediaType, PeerKind
from app.adminbot import views
from app.db.models import DiscoveredLink
from app.domain.links import LinkKind, links_in, links_in_message
from app.repositories import discovered_links as link_repo
from app.services import discovery as discovery_service
from tests.conftest import connect_bot, discovered, script_for, sync_with_chats
from tests.integration.test_bot_flows import assert_keyboard_is_sendable, assert_valid_markdown_v2

GROUP_A = -100_700_001
GROUP_B = -100_700_002


# --------------------------------------------------------------------------- #
# Reading a link out of a message
# --------------------------------------------------------------------------- #
def test_the_four_spellings_of_one_address_are_one_link():
    """``t.me/Deals`` at the start of a sentence and ``t.me/deals.`` at the end
    are the same chat. Counting them apart destroys the only signal here."""
    found = links_in("Try https://t.me/Deals and t.me/deals. and telegram.me/DEALS today")

    assert [(link.kind, link.key) for link in found] == [(LinkKind.public, "deals")]


def test_an_invite_link_is_kept_as_its_hash():
    found = links_in("Channel link :- https://t.me/+WGJQq38RPg9kMDg1")

    assert [(link.kind, link.key) for link in found] == [(LinkKind.invite, "WGJQq38RPg9kMDg1")]
    assert found[0].url == "https://t.me/+WGJQq38RPg9kMDg1"


def test_a_post_link_names_the_chat_it_is_in():
    """``t.me/name/451`` is one message; the chat is the part worth counting."""
    assert links_in("see https://t.me/premiumtools/451")[0].key == "premiumtools"


def test_telegrams_own_paths_are_not_read_as_chats():
    """``t.me/c/…`` addresses a private chat by internal id and ``t.me/share``
    is a utility link. A chat called "c" does not exist, and offering one
    would be a button that opens nothing."""
    assert links_in("t.me/c/2689645801/12") == []
    assert links_in("t.me/share/url?url=hi") == []
    assert links_in("t.me/joinchat/x") == [], "too short to be a hash"
    assert links_in("t.me/ab") == [], "too short to be a username"


def test_tg_scheme_links_count_too():
    """What a button or a forwarded post carries, rather than a typed URL."""
    assert links_in("tg://resolve?domain=insightxpro")[0].key == "insightxpro"


def test_a_link_hidden_behind_words_is_found():
    """ "👉 Join here" with the address only in the entity is how nearly every
    promotional post is written — so text alone misses exactly the messages
    this is for."""
    found = links_in_message("👉 Join here", ["https://t.me/hiddenone"])

    assert [link.key for link in found] == ["hiddenone"]


def test_a_message_with_no_links_produces_nothing():
    assert links_in("Netflix 4K — 1 Month — $ 0.5") == []
    assert links_in("") == []


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #
async def _two_groups(actor):
    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor,
        connection_id,
        [
            discovered(GROUP_A, "Deals Group", chat_kind="supergroup"),
            discovered(GROUP_B, "Tools Group", chat_kind="supergroup"),
        ],
    )
    return connection_id


def _message(peer_id: int, text: str, urls: list[str] | None = None) -> InboundMessage:
    return InboundMessage(
        source=ChatRef(PeerKind.channel, peer_id),
        message_ids=[1],
        media_type=MediaType.text,
        text=text,
        entity_urls=urls or [],
    )


async def test_a_link_posted_in_a_group_is_counted(client, actor, session):
    connection_id = await _two_groups(actor)

    noted = await discovery_service.note_links(
        session,
        connection_id=uuid.UUID(connection_id),
        message=_message(GROUP_A, "join t.me/somedeals now"),
    )
    await session.commit()

    assert noted == 1
    rows = await link_repo.listing(session, user_id=uuid.UUID(actor.id))
    assert len(rows) == 1
    assert rows[0].link.link_key == "somedeals"
    assert rows[0].link.times_seen == 1
    assert rows[0].group_count == 1
    assert rows[0].titles == ["Deals Group"]


async def test_the_same_link_in_two_groups_counts_as_two_groups(client, actor, session):
    """The number that matters. Fifty posts by one person in one chat is one
    person; two groups carrying it is two communities that overlap with it."""
    connection_id = await _two_groups(actor)
    connection = uuid.UUID(connection_id)

    for peer in (GROUP_A, GROUP_A, GROUP_B):
        await discovery_service.note_links(
            session, connection_id=connection, message=_message(peer, "t.me/somedeals")
        )
    await session.commit()

    rows = await link_repo.listing(session, user_id=uuid.UUID(actor.id))
    assert rows[0].link.times_seen == 3
    assert rows[0].group_count == 2
    assert sorted(rows[0].titles) == ["Deals Group", "Tools Group"]


async def test_the_link_in_more_of_your_groups_ranks_first(client, actor, session):
    connection_id = await _two_groups(actor)
    connection = uuid.UUID(connection_id)

    # Seen many times, but only ever in one group — one enthusiastic poster.
    for _ in range(9):
        await discovery_service.note_links(
            session, connection_id=connection, message=_message(GROUP_A, "t.me/onepersonspam")
        )
    # Seen half as often, but in two — two communities.
    for peer in (GROUP_A, GROUP_B):
        await discovery_service.note_links(
            session, connection_id=connection, message=_message(peer, "t.me/genuinelypopular")
        )
    await session.commit()

    rows = await link_repo.listing(session, user_id=uuid.UUID(actor.id))
    assert [r.link.link_key for r in rows] == ["genuinelypopular", "onepersonspam"]


async def test_a_link_from_a_chat_we_do_not_know_is_ignored(client, actor, session):
    """ "Seen in some group" is not worth a row — the screen could not name it."""
    connection_id = await _two_groups(actor)

    noted = await discovery_service.note_links(
        session,
        connection_id=uuid.UUID(connection_id),
        message=_message(-100_999_999, "t.me/fromnowhere"),
    )
    await session.commit()

    assert noted == 0
    assert await link_repo.listing(session, user_id=uuid.UUID(actor.id)) == []


async def test_one_message_cannot_decide_the_whole_ranking(client, actor, session):
    """A link-farm dump listing two hundred addresses would otherwise outweigh
    every genuine mention in the account's groups put together."""
    connection_id = await _two_groups(actor)
    dump = " ".join(f"t.me/farmlink{i:03d}" for i in range(60))

    noted = await discovery_service.note_links(
        session, connection_id=uuid.UUID(connection_id), message=_message(GROUP_A, dump)
    )
    await session.commit()

    assert noted == discovery_service.MAX_LINKS_PER_MESSAGE


# --------------------------------------------------------------------------- #
# What is left out
# --------------------------------------------------------------------------- #
async def test_a_group_you_are_already_in_is_not_a_discovery(client, actor, session):
    """With 735 chats known, most of the list would otherwise be groups the
    account is already sitting in."""
    connection_id = await connect_bot(actor)
    await sync_with_chats(
        actor, connection_id, [discovered(GROUP_A, "Deals Group", chat_kind="supergroup")]
    )
    from sqlalchemy import select

    from app.db.models import TelegramChat

    chat = (await session.execute(select(TelegramChat))).scalars().first()
    chat.username = "DealsGroup"
    await session.commit()

    await discovery_service.note_links(
        session,
        connection_id=uuid.UUID(connection_id),
        message=_message(GROUP_A, "t.me/dealsgroup and t.me/somethingelse"),
    )
    await session.commit()

    keys = [r.link.link_key for r in await link_repo.listing(session, user_id=uuid.UUID(actor.id))]
    assert keys == ["somethingelse"], "the one already joined is not news"


async def test_hiding_one_keeps_it_gone(client, actor, session):
    """Hidden rather than deleted: the same link posted another forty times
    must not bring it back."""
    connection_id = await _two_groups(actor)
    await discovery_service.note_links(
        session, connection_id=uuid.UUID(connection_id), message=_message(GROUP_A, "t.me/junklink")
    )
    await session.commit()

    rows = await link_repo.listing(session, user_id=uuid.UUID(actor.id))
    assert await link_repo.hide(session, user_id=uuid.UUID(actor.id), link_id=rows[0].link.id)
    await discovery_service.note_links(
        session, connection_id=uuid.UUID(connection_id), message=_message(GROUP_B, "t.me/junklink")
    )
    await session.commit()

    assert await link_repo.listing(session, user_id=uuid.UUID(actor.id)) == []


async def test_the_sweep_only_takes_the_ones_seen_once_in_one_group(client, actor, session):
    """That is what a stray forward looks like. Anything posted twice, or in
    two groups, is somebody's actual promotion and is left alone."""
    connection_id = await _two_groups(actor)
    connection = uuid.UUID(connection_id)

    await discovery_service.note_links(
        session, connection_id=connection, message=_message(GROUP_A, "t.me/strayforward")
    )
    for peer in (GROUP_A, GROUP_B):
        await discovery_service.note_links(
            session, connection_id=connection, message=_message(peer, "t.me/realpromotion")
        )
    await session.commit()

    hidden = await link_repo.hide_seen_once(session, user_id=uuid.UUID(actor.id))
    await session.commit()

    assert hidden == 1
    keys = [r.link.link_key for r in await link_repo.listing(session, user_id=uuid.UUID(actor.id))]
    assert keys == ["realpromotion"]


async def test_another_account_never_sees_your_links(client, actor, session):
    from tests.conftest import register

    connection_id = await _two_groups(actor)
    await discovery_service.note_links(
        session, connection_id=uuid.UUID(connection_id), message=_message(GROUP_A, "t.me/mine")
    )
    await session.commit()

    stranger = await register(client, "stranger-links@example.com")
    assert await link_repo.listing(session, user_id=uuid.UUID(stranger.id)) == []


# --------------------------------------------------------------------------- #
# Looking one up
# --------------------------------------------------------------------------- #
async def test_a_link_that_turns_out_to_be_a_person_leaves_the_list(client, actor, session):
    """``t.me/name`` is a username, and a username can belong to anybody. The
    only way to know is to look, and a person is not a chat to advertise in."""
    connection_id = await _two_groups(actor)
    script = script_for(connection_id)
    script.link_previews["public:someperson"] = LinkPreview(title="Some Person", chat_kind="user")
    script.link_previews["public:realgroup"] = LinkPreview(
        title="Real Group", member_count=4200, chat_kind="supergroup"
    )

    await discovery_service.note_links(
        session,
        connection_id=uuid.UUID(connection_id),
        message=_message(GROUP_A, "t.me/someperson t.me/realgroup"),
    )
    await session.commit()

    from app.db.models import TelegramConnection
    from app.services import connections as connection_service

    connection = await session.get(TelegramConnection, uuid.UUID(connection_id))
    adapter = await connection_service.adapter_for(session, connection)
    rows = await link_repo.listing(session, user_id=uuid.UUID(actor.id))
    await discovery_service.resolve(session, links=[r.link for r in rows], adapter=adapter)
    await session.commit()

    remaining = await link_repo.listing(session, user_id=uuid.UUID(actor.id))
    assert [r.link.link_key for r in remaining] == ["realgroup"]
    assert remaining[0].link.resolved_member_count == 4200


async def test_a_lookup_that_fails_is_not_retried_forever(client, actor, session):
    """A link to a deleted chat would otherwise cost a network call every time
    the screen is drawn, for as long as the row exists."""
    connection_id = await _two_groups(actor)
    script = script_for(connection_id)
    script.link_previews["public:goneaway"] = LinkPreview(reason_code="peer_invalid")

    await discovery_service.note_links(
        session, connection_id=uuid.UUID(connection_id), message=_message(GROUP_A, "t.me/goneaway")
    )
    await session.commit()

    from app.db.models import TelegramConnection
    from app.services import connections as connection_service

    connection = await session.get(TelegramConnection, uuid.UUID(connection_id))
    adapter = await connection_service.adapter_for(session, connection)
    rows = await link_repo.listing(session, user_id=uuid.UUID(actor.id))
    await discovery_service.resolve(session, links=[r.link for r in rows], adapter=adapter)
    await session.commit()

    assert await link_repo.unresolved(session, link_ids=[rows[0].link.id]) == []


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #
def test_the_empty_screen_says_it_watches_rather_than_searches():
    screen = views.discovered_links(rows=[], page=0, total=0)

    assert "Nothing yet" in screen.text
    assert "does not go looking" in screen.text
    assert_valid_markdown_v2(screen.text)


def test_the_screen_names_your_groups_and_offers_no_join_button():
    """Opening a link is the operator's decision, so the only button that acts
    on one is the one that hides it."""
    from types import SimpleNamespace

    link = DiscoveredLink(
        id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        kind="public",
        link_key="realgroup",
        times_seen=12,
        resolved_title="Real Group",
        resolved_member_count=4200,
        resolved_kind="supergroup",
    )
    row = SimpleNamespace(link=link, group_count=3, titles=["Deals Group", "Tools Group", "Third"])
    screen = views.discovered_links(rows=[row], page=0, total=1)

    assert "Real Group" in screen.text
    assert "4,200 members" in screen.text
    assert "*3* of your groups" in screen.text
    assert "Deals Group" in screen.text and "\\+1" in screen.text

    data = [b.callback_data for r in screen.keyboard.inline_keyboard for b in r]
    assert any((d or "").startswith("link:hide:") for d in data)
    assert not any("join" in (d or "").lower() for d in data)
    urls = [b.url for r in screen.keyboard.inline_keyboard for b in r if b.url]
    assert urls == ["https://t.me/realgroup"]

    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)


def test_a_hostile_chat_title_cannot_break_the_screen():
    from types import SimpleNamespace

    link = DiscoveredLink(
        id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        kind="invite",
        link_key="WGJQq38RPg9kMDg1",
        times_seen=2,
        resolved_title="*bold* _under_ [link](x) — 100% off!",
        resolved_kind="supergroup",
    )
    row = SimpleNamespace(link=link, group_count=1, titles=["*evil* group"])
    screen = views.discovered_links(rows=[row], page=0, total=1)

    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)


# --------------------------------------------------------------------------- #
# The promise
# --------------------------------------------------------------------------- #
def test_nothing_about_the_sender_is_stored():
    """The question is "which chats keep coming up", and neither the message
    nor the person who sent it is needed to answer it. Not storing them is the
    difference between counting mentions and keeping a file on people."""
    columns = {c.name for c in DiscoveredLink.__table__.columns}

    for forbidden in ("sender_id", "sender", "from_user", "author", "message_text", "text", "body"):
        assert forbidden not in columns, f"{forbidden} has no business being here"


def test_discovery_can_only_notice_and_look_up():
    """The safety argument is that it observes and never acts, so the module's
    public surface is pinned. A third function is how "notice a link" quietly
    becomes "do something about it".

    (That nothing anywhere joins a chat or reads a member list is checked once
    for the whole codebase, in ``test_guards``. This is the narrower promise:
    that *this* module gained no verb.)
    """
    import inspect

    from app.services import discovery

    public = {
        name
        for name, obj in vars(discovery).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == discovery.__name__
    }
    assert public == {"note_links", "resolve"}, f"discovery gained a verb: {sorted(public)}"


def test_the_only_telegram_call_discovery_makes_is_a_preview():
    """``preview_link`` returns what Telegram shows anyone holding the link and
    refuses everything else. Any other adapter method reached from here would
    be doing something to a chat rather than looking at one."""
    import inspect
    import re

    from app.services import discovery

    source = inspect.getsource(discovery)
    called = set(re.findall(r"adapter\.(\w+)", source))
    assert called == {"preview_link"}, f"discovery reaches Telegram by: {sorted(called)}"
