"""Envelope encryption for Telegram secrets.

    KEK (env, versioned)  --wraps-->  DEK (random per record)  --AES-GCM-->  plaintext

A database backup alone is therefore not sufficient to decrypt session material:
the KEK lives only in the process environment.

GCM additional-authenticated-data binds each ciphertext to
``(connection_id, field_name)``, so a ciphertext cannot be moved between rows or
columns and still decrypt. See docs/SECURITY.md §4.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

_NONCE_BYTES: Final = 12
_DEK_BYTES: Final = 32


class DecryptionError(Exception):
    """Raised when ciphertext cannot be authenticated. Never carries plaintext."""


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """What gets persisted. ``wrapped_dek`` and ``ciphertext`` are separate columns."""

    ciphertext: bytes
    wrapped_dek: bytes
    key_version: int


def _aad(connection_id: str, field: str) -> bytes:
    return f"{connection_id}|{field}".encode()


def _kek(version: int | None = None) -> bytes:
    settings = get_settings()
    if version is not None and version != settings.encryption_kek_version:
        # Rotation support: an older version would be looked up from a keyring
        # here. Until a second key exists, refusing is the honest behaviour.
        raise DecryptionError(f"No key material for key_version={version}")
    return settings.kek_bytes


def seal(plaintext: str | bytes, *, connection_id: str, field: str) -> SealedSecret:
    """Encrypt with a fresh per-record DEK, then wrap that DEK with the KEK."""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode()
    settings = get_settings()

    dek = os.urandom(_DEK_BYTES)
    data_nonce = os.urandom(_NONCE_BYTES)
    ciphertext = data_nonce + AESGCM(dek).encrypt(data_nonce, plaintext, _aad(connection_id, field))

    dek_nonce = os.urandom(_NONCE_BYTES)
    wrapped = dek_nonce + AESGCM(_kek()).encrypt(dek_nonce, dek, _aad(connection_id, field))

    return SealedSecret(
        ciphertext=ciphertext,
        wrapped_dek=wrapped,
        key_version=settings.encryption_kek_version,
    )


def unseal(sealed: SealedSecret, *, connection_id: str, field: str) -> bytes:
    """Unwrap the DEK and decrypt. Raises :class:`DecryptionError` on any mismatch."""
    aad = _aad(connection_id, field)
    try:
        kek = _kek(sealed.key_version)
        dek = AESGCM(kek).decrypt(
            sealed.wrapped_dek[:_NONCE_BYTES], sealed.wrapped_dek[_NONCE_BYTES:], aad
        )
        return AESGCM(dek).decrypt(
            sealed.ciphertext[:_NONCE_BYTES], sealed.ciphertext[_NONCE_BYTES:], aad
        )
    except InvalidTag as exc:
        raise DecryptionError("Ciphertext failed authentication") from exc
    except DecryptionError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise DecryptionError("Unable to decrypt") from exc


def unseal_str(sealed: SealedSecret, *, connection_id: str, field: str) -> str:
    return unseal(sealed, connection_id=connection_id, field=field).decode()
