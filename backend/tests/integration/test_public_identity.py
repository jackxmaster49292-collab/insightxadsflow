"""The bot's public face: profile text, the role question, and the links.

Two Telegram facts constrain all of it, and both are checked rather than
assumed: ``setMyDescription`` is plain text with a 512-character ceiling (120
for the short one), and it carries no entities — so no links and no custom
emoji, whatever the panel's own screens can do.
"""

from __future__ import annotations

import uuid

import pytest

from app.adminbot import views
from app.config import get_settings
from tests.conftest import connect_bot
from tests.integration.test_bot_flows import (
    Sent,
    a_callback,
    assert_keyboard_is_sendable,
    assert_valid_markdown_v2,
)

LINKS = [
    ("Support", "https://t.me/insightxpro_support"),
    ("Updates", "https://t.me/insightxpro_updates"),
    ("Privacy", "https://insightxpro.com/privacy"),
    ("Terms", "https://insightxpro.com/terms"),
]


# --------------------------------------------------------------------------- #
# The profile Telegram shows
# --------------------------------------------------------------------------- #
def test_the_short_description_fits_telegram_s_ceiling():
    short = get_settings().bot_short_description
    assert len(short) <= 120, f"Telegram caps this at 120, this is {len(short)}"
    assert short.startswith("Create, schedule and track")


def test_the_profile_description_stays_inside_512_with_every_link(monkeypatch):
    """Four links plus the sentence must still fit, or Telegram rejects the
    whole call and the profile silently keeps whatever was there before."""
    settings = get_settings()
    for field, (_label, url) in zip(
        ("support_url", "updates_url", "privacy_url", "terms_url"), LINKS, strict=True
    ):
        monkeypatch.setattr(settings, field, url)

    parts = [settings.bot_short_description, "", *(f"{k}: {v}" for k, v in settings.public_links)]
    description = "\n".join(parts)
    assert len(description) <= 512, f"{len(description)} characters would be refused"
    assert "t.me/insightxpro_support" in description


def test_an_unset_link_produces_no_entry(monkeypatch):
    """A button that opens nothing reads as broken software, not as a channel
    that does not exist yet."""
    settings = get_settings()
    monkeypatch.setattr(settings, "support_url", "https://t.me/x")
    monkeypatch.setattr(settings, "updates_url", "")
    monkeypatch.setattr(settings, "privacy_url", "")
    monkeypatch.setattr(settings, "terms_url", "")

    assert settings.public_links == [("Support", "https://t.me/x")]


# --------------------------------------------------------------------------- #
# The role question
# --------------------------------------------------------------------------- #
def test_the_role_screen_offers_both_sides_and_the_links():
    screen = views.roles(links=LINKS)

    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)
    callbacks = [b.callback_data for row in screen.keyboard.inline_keyboard for b in row]
    assert "role:adv" in callbacks
    assert "role:pub" in callbacks
    assert "role:ins" in callbacks

    urls = [b.url for row in screen.keyboard.inline_keyboard for b in row if b.url]
    assert urls == [url for _label, url in LINKS]


def test_the_role_screen_renders_with_no_links_configured():
    screen = views.roles()
    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)
    assert not [b for row in screen.keyboard.inline_keyboard for b in row if b.url]


def test_the_about_screen_says_when_nothing_is_configured():
    """Better than a screen of three buttons that open nothing."""
    screen = views.about()
    assert "No support or updates channel is configured" in screen.text
    assert_valid_markdown_v2(screen.text)

    configured = views.about(links=LINKS)
    assert "No support" not in configured.text
    assert_valid_markdown_v2(configured.text)


# --------------------------------------------------------------------------- #
# The publisher side, described honestly
# --------------------------------------------------------------------------- #
def test_the_publisher_screen_does_not_pretend_to_work():
    """Listings, pricing, held payment and moderation are not built. A screen
    that looked like a feature and did nothing would be worse than this."""
    screen = views.publisher_waitlist(joined=False)

    assert "not open yet" in screen.text
    assert "Advertiser" in screen.text, "and it points at what does work"
    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)


async def test_asking_to_be_told_is_recorded(client, actor, session):
    """Demand over time is the only thing that would justify building it."""
    from app.adminbot import handlers
    from app.repositories import users as user_repo

    await handlers.choose_role(a_callback("role:pub:notify"), user_id=uuid.UUID(actor.id))

    user = await user_repo.get_by_id(session, uuid.UUID(actor.id))
    await session.refresh(user)
    assert user.publisher_interest_at is not None
    assert "You are on the list" in Sent.last()


async def test_asking_twice_keeps_the_first_time(client, actor, session):
    from app.adminbot import handlers
    from app.repositories import users as user_repo

    await handlers.choose_role(a_callback("role:pub:notify"), user_id=uuid.UUID(actor.id))
    user = await user_repo.get_by_id(session, uuid.UUID(actor.id))
    await session.refresh(user)
    first = user.publisher_interest_at

    await handlers.choose_role(a_callback("role:pub:notify"), user_id=uuid.UUID(actor.id))
    await session.refresh(user)
    assert user.publisher_interest_at == first, "when they asked, not when they last tapped"


async def test_insights_shows_the_numbers_the_bot_actually_has(client, actor, state):
    """Named Insights rather than anything implying analysis the code does not
    do — and 'campaign' is not this product's vocabulary."""
    from app.adminbot import handlers

    await connect_bot(actor)
    await handlers.choose_role(a_callback("role:ins"), user_id=uuid.UUID(actor.id))

    assert_valid_markdown_v2(Sent.last())
    assert "campaign" not in Sent.last().lower()


@pytest.mark.parametrize("role", ["adv", "pub", "ins"])
async def test_every_role_lands_on_a_sendable_screen(client, actor, state, role):
    from app.adminbot import handlers

    await handlers.choose_role(a_callback(f"role:{role}"), user_id=uuid.UUID(actor.id))
    text, markup = Sent.messages[-1]
    assert_valid_markdown_v2(text)
    assert_keyboard_is_sendable(markup)
