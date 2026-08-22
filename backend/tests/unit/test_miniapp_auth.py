"""Mini App initData verification.

This replaces password authentication for the Telegram surface, so a bug here is
an authentication bypass. Adversarial cases get as much attention as happy paths.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import pytest

from app.security.miniapp import InitDataError, build_init_data, verify_init_data

TOKEN = "123456789:AAEadminBotTokenValueThatIsLongEnough"
OTHER_TOKEN = "987654321:AAEdifferentBotTokenValueLongEnough"


def payload(**overrides: str) -> dict[str, str]:
    base = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": 4242, "username": "admin", "first_name": "Ada"}),
    }
    base.update(overrides)
    return base


def signed(**overrides: str) -> str:
    return build_init_data(payload(**overrides), bot_token=TOKEN)


def test_valid_init_data_yields_the_identity():
    identity = verify_init_data(signed(), bot_token=TOKEN)
    assert identity.telegram_user_id == 4242
    assert identity.username == "admin"
    assert identity.first_name == "Ada"


def test_signature_from_a_different_bot_token_is_rejected():
    """The whole point: only *our* bot can mint a valid launch."""
    forged = build_init_data(payload(), bot_token=OTHER_TOKEN)
    with pytest.raises(InitDataError, match="signature mismatch"):
        verify_init_data(forged, bot_token=TOKEN)


def test_tampering_with_the_user_id_is_rejected():
    """The attack that matters: swap yourself for the admin's id."""
    fields = payload()
    raw = build_init_data(fields, bot_token=TOKEN)
    tampered = raw.replace(
        urlencode({"user": fields["user"]}).split("=", 1)[1],
        urlencode({"user": json.dumps({"id": 1, "username": "attacker"})}).split("=", 1)[1],
    )
    with pytest.raises(InitDataError):
        verify_init_data(tampered, bot_token=TOKEN)


def test_any_field_change_invalidates_the_signature():
    raw = signed()
    with pytest.raises(InitDataError, match="signature mismatch"):
        verify_init_data(raw.replace("query_id=", "query_id=x"), bot_token=TOKEN)


def test_missing_hash_is_rejected():
    with pytest.raises(InitDataError, match="no hash"):
        verify_init_data(urlencode(payload()), bot_token=TOKEN)


def test_empty_init_data_is_rejected():
    with pytest.raises(InitDataError, match="Empty"):
        verify_init_data("", bot_token=TOKEN)


def test_missing_bot_token_is_rejected_rather_than_defaulting_open():
    with pytest.raises(InitDataError, match="No admin bot token"):
        verify_init_data(signed(), bot_token="")


def test_stale_init_data_is_rejected():
    old = str(int(time.time()) - 90_000)
    with pytest.raises(InitDataError, match="expired"):
        verify_init_data(signed(auth_date=old), bot_token=TOKEN, max_age_s=86_400)


def test_replay_within_the_window_is_still_accepted():
    recent = str(int(time.time()) - 60)
    assert verify_init_data(signed(auth_date=recent), bot_token=TOKEN).telegram_user_id == 4242


def test_future_auth_date_is_rejected():
    future = str(int(time.time()) + 4_000)
    with pytest.raises(InitDataError, match="future"):
        verify_init_data(signed(auth_date=future), bot_token=TOKEN)


def test_small_clock_skew_is_tolerated():
    slight = str(int(time.time()) + 60)
    assert verify_init_data(signed(auth_date=slight), bot_token=TOKEN)


def test_missing_auth_date_is_rejected():
    fields = payload()
    del fields["auth_date"]
    with pytest.raises(InitDataError, match="no auth_date"):
        verify_init_data(build_init_data(fields, bot_token=TOKEN), bot_token=TOKEN)


def test_non_numeric_auth_date_is_rejected():
    with pytest.raises(InitDataError, match="not an integer"):
        verify_init_data(signed(auth_date="tomorrow"), bot_token=TOKEN)


def test_missing_user_is_rejected():
    fields = payload()
    del fields["user"]
    with pytest.raises(InitDataError, match="no user"):
        verify_init_data(build_init_data(fields, bot_token=TOKEN), bot_token=TOKEN)


def test_malformed_user_json_is_rejected():
    with pytest.raises(InitDataError, match="valid JSON"):
        verify_init_data(signed(user="{not json"), bot_token=TOKEN)


def test_user_without_a_numeric_id_is_rejected():
    with pytest.raises(InitDataError, match="numeric id"):
        verify_init_data(signed(user=json.dumps({"username": "nobody"})), bot_token=TOKEN)


def test_signature_field_is_excluded_from_the_check_string():
    """Telegram's `signature` is for third parties; including it would break
    verification for every launch that carries one."""
    fields = payload()
    raw = build_init_data(fields, bot_token=TOKEN)
    with_signature = raw + "&signature=" + "a" * 64
    assert verify_init_data(with_signature, bot_token=TOKEN).telegram_user_id == 4242


def test_blank_valued_fields_still_participate_in_the_signature():
    fields = payload(start_param="")
    assert verify_init_data(build_init_data(fields, bot_token=TOKEN), bot_token=TOKEN)
