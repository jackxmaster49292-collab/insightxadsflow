"""Rule filter evaluation.

Pure functions over an :class:`~app.adapters.base.InboundMessage` and a rule's
filter configuration, so the same code runs at intake time and again at delivery
time when a job predates a rule edit.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.adapters.base import UNSUPPORTED_MEDIA, InboundMessage, MediaType
from app.domain import reasons

#: Customer-facing aliases accepted in ``media_types``.
MEDIA_ALIASES: dict[str, str] = {
    "image": MediaType.photo.value,
    "gif": MediaType.animation.value,
    "file": MediaType.document.value,
}


@dataclass(frozen=True, slots=True)
class FilterConfig:
    keyword_include: Sequence[str] = ()
    keyword_exclude: Sequence[str] = ()
    keyword_match_mode: str = "substring"
    media_types: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class FilterDecision:
    passed: bool
    reason_code: str

    @classmethod
    def allow(cls) -> FilterDecision:
        return cls(True, reasons.OK)

    @classmethod
    def block(cls, reason_code: str) -> FilterDecision:
        return cls(False, reason_code)


def normalize_media_types(values: Sequence[str]) -> set[str]:
    return {MEDIA_ALIASES.get(v.strip().lower(), v.strip().lower()) for v in values if v.strip()}


def _matches(haystack: str, needle: str, mode: str) -> bool:
    needle = needle.strip()
    if not needle:
        return False
    if mode == "word":
        return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack, re.IGNORECASE) is not None
    return needle.casefold() in haystack.casefold()


def evaluate(message: InboundMessage, config: FilterConfig) -> FilterDecision:
    """Decide whether a source message should produce forwarding jobs.

    Order matters: content-safety checks come before customer filters so a
    protected message is always reported as protected, never as "filtered out".
    """
    # 1. Content protection. Refused in both forward and copy mode by design.
    if message.has_protected_content:
        return FilterDecision.block(reasons.PROTECTED_CONTENT)

    # 2. Message types the MVP does not deliver at all.
    if message.media_type in UNSUPPORTED_MEDIA:
        return FilterDecision.block(reasons.UNSUPPORTED_MESSAGE)

    # 3. Media-type filter. An empty list means "no media filtering".
    selected = normalize_media_types(config.media_types)
    if selected and message.media_type.value not in selected:
        return FilterDecision.block(reasons.FILTERED_MEDIA_TYPE)

    haystack = message.text or ""
    mode = config.keyword_match_mode

    # 4. Exclude wins over include.
    if any(_matches(haystack, kw, mode) for kw in config.keyword_exclude):
        return FilterDecision.block(reasons.FILTERED_KEYWORD_EXCLUDE)

    # 5. Include list, when present, must match at least once.
    include = [kw for kw in config.keyword_include if kw.strip()]
    if include and not any(_matches(haystack, kw, mode) for kw in include):
        return FilterDecision.block(reasons.FILTERED_KEYWORD_INCLUDE)

    return FilterDecision.allow()
