"""The bot's public face: profile text, the role question, and the links.

Two Telegram facts constrain all of it, and both are checked rather than
assumed: ``setMyDescription`` is plain text with a 512-character ceiling (120
for the short one), and it carries no entities — so no links and no custom
emoji, whatever the panel's own screens can do.
"""

from __future__ import annotations

from app.adminbot import views
from app.config import get_settings
from tests.integration.test_bot_flows import (
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
# The About screen
# --------------------------------------------------------------------------- #
def test_the_about_screen_says_when_nothing_is_configured():
    """Better than a screen of three buttons that open nothing."""
    screen = views.about()
    assert "No support or updates channel is configured" in screen.text
    assert_valid_markdown_v2(screen.text)

    configured = views.about(links=LINKS)
    assert "No support" not in configured.text
    assert_valid_markdown_v2(configured.text)
    urls = [b.url for row in configured.keyboard.inline_keyboard for b in row if b.url]
    assert urls == [url for _label, url in LINKS]
    assert_keyboard_is_sendable(configured.keyboard)
