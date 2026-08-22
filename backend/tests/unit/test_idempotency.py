from __future__ import annotations

import uuid

import pytest

from app.adapters.base import ChatRef, PeerKind
from app.domain.idempotency import build_idempotency_key, build_mtproto_random_id

RULE = uuid.UUID("11111111-1111-1111-1111-111111111111")
SOURCE = ChatRef(PeerKind.channel, -1001234567890)
DEST_A = ChatRef(PeerKind.channel, -1009876543210)
DEST_B = ChatRef(PeerKind.chat, -400500600)


def key(rule=RULE, source=SOURCE, ids=(10,), dest=DEST_A) -> str:
    return build_idempotency_key(
        rule_id=rule, source=source, message_ids=list(ids), destination=dest
    )


def test_key_is_stable_across_calls():
    assert key() == key()


def test_key_differs_per_destination():
    assert key(dest=DEST_A) != key(dest=DEST_B)


def test_key_differs_per_rule():
    assert key() != key(rule=uuid.uuid4())


def test_key_differs_per_message():
    assert key(ids=(10,)) != key(ids=(11,))


def test_peer_type_participates_so_overlapping_ids_cannot_collide():
    """Telegram's user/chat/channel id sequences overlap, so the same numeric id
    with a different peer type must produce a different key."""
    as_channel = key(dest=ChatRef(PeerKind.channel, 777))
    as_chat = key(dest=ChatRef(PeerKind.chat, 777))
    as_user = key(dest=ChatRef(PeerKind.user, 777))
    assert len({as_channel, as_chat, as_user}) == 3


def test_album_anchors_on_the_lowest_id_regardless_of_arrival_order():
    assert key(ids=(12, 10, 11)) == key(ids=(10, 11, 12)) == key(ids=(10,))


def test_access_hash_does_not_affect_the_key():
    """access_hash is account-specific and can be refreshed; it must not change
    a delivery's identity."""
    with_hash = ChatRef(PeerKind.channel, -1009876543210, access_hash=999)
    assert key(dest=with_hash) == key(dest=DEST_A)


def test_empty_message_ids_is_rejected():
    with pytest.raises(ValueError):
        build_idempotency_key(rule_id=RULE, source=SOURCE, message_ids=[], destination=DEST_A)


def test_random_id_is_deterministic_and_fits_63_bits():
    k = key()
    first = build_mtproto_random_id(k)
    assert first == build_mtproto_random_id(k)
    assert 0 <= first <= 0x7FFF_FFFF_FFFF_FFFF


def test_random_id_differs_per_delivery():
    assert build_mtproto_random_id(key(dest=DEST_A)) != build_mtproto_random_id(key(dest=DEST_B))
