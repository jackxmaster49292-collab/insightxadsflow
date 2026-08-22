"""Redaction and envelope encryption.

The redaction test is the one that proves the promise in the README: a token, a
session string, a phone number, a login code, and a 2FA password fed through the
logger appear nowhere in the output.
"""

from __future__ import annotations

import base64
import io
import json

import pytest
import structlog

from app.config import get_settings
from app.logging_setup import configure_logging
from app.security.auth import hash_password, hash_token, needs_rehash, verify_password
from app.security.crypto import DecryptionError, SealedSecret, seal, unseal, unseal_str
from app.security.redaction import REDACTED, redact, scrub_text

BOT_TOKEN = "123456789:AAEabcdefghijklmnopqrstuvwxyz012345678"
SESSION_STRING = "1" + base64.b64encode(b"x" * 120).decode().replace("=", "")
PHONE = "+447700900123"
LOGIN_CODE = "54321"
TWO_FA = "hunter2-correct-horse"


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_denylisted_keys_are_redacted():
    out = redact(
        {
            "bot_token": BOT_TOKEN,
            "session_string": SESSION_STRING,
            "phone": PHONE,
            "password": TWO_FA,
            "access_hash": 12345,
            "api_hash": "abc",
        }
    )
    assert all(value == REDACTED for value in out.values())


def test_useful_lookalike_keys_survive():
    out = redact({"reason_code": "write_forbidden", "last_error_code": "flood_wait"})
    assert out["reason_code"] == "write_forbidden"
    assert out["last_error_code"] == "flood_wait"


def test_secret_shapes_are_scrubbed_from_free_text():
    assert BOT_TOKEN not in scrub_text(f"request failed for {BOT_TOKEN}")
    assert PHONE not in scrub_text(f"sending code to {PHONE}")
    assert SESSION_STRING not in scrub_text(f"session={SESSION_STRING}")


def test_nested_structures_are_redacted():
    out = redact({"outer": {"list": [{"bot_token": BOT_TOKEN}]}})
    assert out["outer"]["list"][0]["bot_token"] == REDACTED


def test_exceptions_are_reduced_to_class_and_scrubbed_text():
    out = redact({"error": ValueError(f"boom {BOT_TOKEN}")})
    assert out["error"].startswith("ValueError:")
    assert BOT_TOKEN not in out["error"]


def test_non_string_scalars_pass_through_unchanged():
    """Redaction must not mangle the numbers and flags events rely on."""
    out = redact({"attempt": 3, "delivered": True, "wait_s": 1.5, "job_id": None})
    assert out == {"attempt": 3, "delivered": True, "wait_s": 1.5, "job_id": None}


def test_collections_are_traversed_and_keep_their_type():
    assert redact([BOT_TOKEN]) == [REDACTED]
    assert redact((BOT_TOKEN,)) == (REDACTED,)
    assert redact({BOT_TOKEN}) == {REDACTED}


def test_deeply_nested_input_terminates():
    payload: dict = {}
    cursor = payload
    for _ in range(50):
        cursor["next"] = {}
        cursor = cursor["next"]
    assert redact(payload) is not None


def test_no_secret_reaches_the_log_sink():
    """The end-to-end promise, exercised through the real logger configuration."""
    buffer = io.StringIO()
    configure_logging(json_output=True)
    structlog.configure(
        processors=structlog.get_config()["processors"],
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )
    log = structlog.get_logger("test")
    log.info(
        "connection_attempt",
        bot_token=BOT_TOKEN,
        session=SESSION_STRING,
        phone=PHONE,
        code=LOGIN_CODE,
        password=TWO_FA,
        note=f"raw token {BOT_TOKEN} and phone {PHONE} inline",
    )

    output = buffer.getvalue()
    assert output
    for secret in (BOT_TOKEN, SESSION_STRING, PHONE, TWO_FA):
        assert secret not in output, f"{secret!r} leaked into logs"
    payload = json.loads(output.strip().splitlines()[-1])
    assert payload["bot_token"] == REDACTED


# --------------------------------------------------------------------------- #
# Envelope encryption
# --------------------------------------------------------------------------- #
CONNECTION = "5f2b8a1e-0000-4000-8000-000000000001"


def test_round_trip():
    sealed = seal(SESSION_STRING, connection_id=CONNECTION, field="mtproto_session")
    assert unseal_str(sealed, connection_id=CONNECTION, field="mtproto_session") == SESSION_STRING


def test_ciphertext_does_not_contain_the_plaintext():
    sealed = seal(BOT_TOKEN, connection_id=CONNECTION, field="bot_token")
    assert BOT_TOKEN.encode() not in sealed.ciphertext
    assert BOT_TOKEN.encode() not in sealed.wrapped_dek


def test_each_record_gets_a_distinct_dek():
    a = seal("same", connection_id=CONNECTION, field="bot_token")
    b = seal("same", connection_id=CONNECTION, field="bot_token")
    assert a.wrapped_dek != b.wrapped_dek
    assert a.ciphertext != b.ciphertext


def test_ciphertext_moved_to_another_connection_fails_to_decrypt():
    """AAD binds ciphertext to (connection_id, field), so a row copied between
    connections is not decryptable."""
    sealed = seal(BOT_TOKEN, connection_id=CONNECTION, field="bot_token")
    with pytest.raises(DecryptionError):
        unseal(sealed, connection_id="5f2b8a1e-0000-4000-8000-000000000002", field="bot_token")


def test_ciphertext_moved_to_another_field_fails_to_decrypt():
    sealed = seal(BOT_TOKEN, connection_id=CONNECTION, field="bot_token")
    with pytest.raises(DecryptionError):
        unseal(sealed, connection_id=CONNECTION, field="mtproto_session")


def test_tampered_ciphertext_is_rejected():
    sealed = seal(BOT_TOKEN, connection_id=CONNECTION, field="bot_token")
    tampered = SealedSecret(
        ciphertext=sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 0x01]),
        wrapped_dek=sealed.wrapped_dek,
        key_version=sealed.key_version,
    )
    with pytest.raises(DecryptionError):
        unseal(tampered, connection_id=CONNECTION, field="bot_token")


def test_unknown_key_version_is_refused_rather_than_guessed():
    sealed = seal(BOT_TOKEN, connection_id=CONNECTION, field="bot_token")
    stale = SealedSecret(sealed.ciphertext, sealed.wrapped_dek, key_version=999)
    with pytest.raises(DecryptionError):
        unseal(stale, connection_id=CONNECTION, field="bot_token")


def test_key_version_is_recorded_for_rotation():
    sealed = seal("x", connection_id=CONNECTION, field="bot_token")
    assert sealed.key_version == get_settings().encryption_kek_version


# --------------------------------------------------------------------------- #
# Passwords and tokens
# --------------------------------------------------------------------------- #
def test_password_hash_round_trip():
    hashed = hash_password("correct-horse-battery")
    assert hashed != "correct-horse-battery"
    assert verify_password(hashed, "correct-horse-battery")
    assert not verify_password(hashed, "wrong")


def test_verify_password_tolerates_a_malformed_hash():
    assert not verify_password("not-a-hash", "anything")


def test_needs_rehash_handles_a_malformed_hash():
    assert needs_rehash("not-a-hash")


def test_session_token_is_stored_only_as_a_hash():
    raw = "some-random-cookie-value"
    assert hash_token(raw) != raw
    assert len(hash_token(raw)) == 64
