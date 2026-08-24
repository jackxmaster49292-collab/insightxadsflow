"""Granting operator access from inside the panel.

The whole point is that an owner with more than one Telegram account never
edits a file to use their own tool. What it must not become is a way for
anyone to acquire that access: on an open deployment a stranger gets an account
simply by messaging the bot, so operator status is *granted*, never inferred.
"""

from __future__ import annotations

import uuid

import pytest

from app.adminbot import views
from app.config import get_settings
from app.repositories import users as user_repo
from app.services import users as user_service
from tests.integration.test_bot_flows import (
    ADMIN_CHAT,
    Sent,
    a_callback,
    assert_keyboard_is_sendable,
    assert_valid_markdown_v2,
)

SECOND_ACCOUNT = 7_675_995_840


@pytest.fixture
async def operator(actor, session, monkeypatch):
    """``actor`` is an operator through the settings file."""
    user = await user_repo.get_by_id(session, uuid.UUID(actor.id))
    user.telegram_user_id = ADMIN_CHAT
    await session.commit()
    monkeypatch.setattr(get_settings(), "admin_telegram_ids", str(ADMIN_CHAT), raising=False)
    return user


@pytest.fixture
async def second(other_actor, session):
    """A second account of the owner's — a plain user until granted."""
    user = await user_repo.get_by_id(session, uuid.UUID(other_actor.id))
    user.telegram_user_id = SECOND_ACCOUNT
    await session.commit()
    return user


# --------------------------------------------------------------------------- #
# Who is an operator
# --------------------------------------------------------------------------- #
async def test_the_settings_file_is_the_root_of_trust(client, session, operator):
    assert await user_service.is_operator(session, telegram_user_id=ADMIN_CHAT)


async def test_a_plain_account_is_not_an_operator(client, session, operator, second):
    """The reported situation: ads sent from a second account archived nothing,
    because that account was a user and nothing more."""
    assert not await user_service.is_operator(session, telegram_user_id=SECOND_ACCOUNT)


async def test_a_granted_account_is_an_operator_without_a_restart(
    client, session, operator, second
):
    second.is_operator = True
    await session.commit()
    assert await user_service.is_operator(session, telegram_user_id=SECOND_ACCOUNT)


async def test_an_unknown_account_is_never_an_operator(client, session, operator):
    assert not await user_service.is_operator(session, telegram_user_id=999_000_111)
    assert not await user_service.is_operator(session, telegram_user_id=None)


# --------------------------------------------------------------------------- #
# Granting it
# --------------------------------------------------------------------------- #
async def test_an_operator_grants_a_second_account_in_two_taps(
    client, actor, session, operator, second
):
    from app.adminbot import handlers

    await handlers.user_actions(
        a_callback(f"usr:{second.id}:op"), user_id=uuid.UUID(actor.id), is_operator=True
    )
    assert "make" in Sent.last().lower()

    await handlers.user_actions(
        a_callback(f"usr:{second.id}:opyes"), user_id=uuid.UUID(actor.id), is_operator=True
    )
    await session.refresh(second)
    assert second.is_operator
    assert await user_service.is_operator(session, telegram_user_id=SECOND_ACCOUNT)


async def test_a_non_operator_cannot_grant_it(client, actor, session, operator, second):
    """Otherwise the feature is a way to acquire the access it protects."""
    from app.adminbot import handlers

    await handlers.user_actions(
        a_callback(f"usr:{second.id}:opyes"), user_id=uuid.UUID(actor.id), is_operator=False
    )
    await session.refresh(second)
    assert not second.is_operator
    assert any("not available" in alert for alert in Sent.alerts)


async def test_operator_can_be_taken_back(client, actor, session, operator, second):
    from app.adminbot import handlers

    second.is_operator = True
    await session.commit()

    await handlers.user_actions(
        a_callback(f"usr:{second.id}:unop"), user_id=uuid.UUID(actor.id), is_operator=True
    )
    await session.refresh(second)
    assert not second.is_operator


async def test_you_cannot_remove_your_own_access(client, actor, session, operator):
    """A locked door with the key inside."""
    from app.adminbot import handlers

    await handlers.user_actions(
        a_callback(f"usr:{operator.id}:unop"), user_id=uuid.UUID(actor.id), is_operator=True
    )
    assert any("your own operator access" in alert for alert in Sent.alerts)


async def test_an_id_from_the_settings_file_cannot_be_demoted(
    client, actor, session, operator, second, monkeypatch
):
    """The root of trust stays put, or a deployment can tap itself out of its
    own operator list."""
    from app.adminbot import handlers

    monkeypatch.setattr(
        get_settings(), "admin_telegram_ids", f"{ADMIN_CHAT},{SECOND_ACCOUNT}", raising=False
    )
    second.is_operator = True
    await session.commit()

    await handlers.user_actions(
        a_callback(f"usr:{second.id}:unop"), user_id=uuid.UUID(actor.id), is_operator=True
    )
    assert any("settings file" in alert for alert in Sent.alerts)
    assert await user_service.is_operator(session, telegram_user_id=SECOND_ACCOUNT)


def test_the_confirmation_names_what_is_being_handed_over():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    user = SimpleNamespace(
        id=uuid.uuid4(),
        telegram_user_id=SECOND_ACCOUNT,
        telegram_username=None,
        email=None,
        is_active=True,
        is_operator=False,
        created_at=datetime.now(UTC),
        suspended_reason=None,
        terms_accepted_at=datetime.now(UTC),
    )
    screen = views.confirm_operator(user=user)

    assert "suspend any of them" in screen.text
    assert "not" in screen.text and "messages" in screen.text
    assert_valid_markdown_v2(screen.text)
    assert_keyboard_is_sendable(screen.keyboard)


def test_a_settings_file_operator_gets_no_demote_button():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    user = SimpleNamespace(
        id=uuid.uuid4(),
        telegram_user_id=ADMIN_CHAT,
        telegram_username=None,
        email=None,
        is_active=True,
        is_operator=True,
        created_at=datetime.now(UTC),
        suspended_reason=None,
        terms_accepted_at=datetime.now(UTC),
    )
    activity = {"connections": 0, "broadcasts": 0, "rules": 0}

    from_env = views.user_detail(user=user, activity=activity, from_env=True)
    callbacks = [b.callback_data for row in from_env.keyboard.inline_keyboard for b in row]
    assert not any(c.endswith(":unop") for c in callbacks)
    assert "settings file" in from_env.text

    granted = views.user_detail(user=user, activity=activity, from_env=False)
    callbacks = [b.callback_data for row in granted.keyboard.inline_keyboard for b in row]
    assert any(c.endswith(":unop") for c in callbacks)
