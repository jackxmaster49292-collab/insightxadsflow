"""Idempotency key construction.

    key = sha256(rule ‖ source peer ‖ source message ‖ destination peer)

``rule_version`` is deliberately **excluded**: editing a rule must never cause an
already-delivered message to be delivered again. The worker instead compares the
job's stored ``rule_version`` against the rule's current version and re-evaluates
filters before sending (docs/OPERATIONS.md §5).

100% coverage is required here (docs/TESTING.md §5).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

from app.adapters.base import ChatRef

_SEPARATOR = "|"


def build_idempotency_key(
    *,
    rule_id: uuid.UUID | str,
    source: ChatRef,
    message_ids: Sequence[int],
    destination: ChatRef,
) -> str:
    """Stable across restarts, reconnects, retries and rule edits."""
    if not message_ids:
        raise ValueError("message_ids must not be empty")

    # An album is one logical message; anchor on its lowest id so a re-delivered
    # album with a different arrival order still produces the same key.
    anchor = min(message_ids)

    material = _SEPARATOR.join(
        [
            str(rule_id),
            source.peer_type.value,
            str(source.peer_id),
            str(anchor),
            destination.peer_type.value,
            str(destination.peer_id),
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def build_mtproto_random_id(idempotency_key: str) -> int:
    """A deterministic 63-bit ``random_id`` derived from the idempotency key.

    MTProto deduplicates identical ``random_id`` values server-side, so deriving
    it deterministically makes a retry after an ambiguous timeout genuinely
    idempotent rather than merely hopeful.
    """
    digest = hashlib.sha256(f"random_id{_SEPARATOR}{idempotency_key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF
