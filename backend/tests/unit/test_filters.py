from __future__ import annotations

import pytest

from app.adapters.base import ChatRef, InboundMessage, MediaType, PeerKind
from app.domain import reasons
from app.domain.filters import FilterConfig, evaluate, normalize_media_types

SOURCE = ChatRef(PeerKind.channel, -100123)


def message(
    text: str = "", media: MediaType = MediaType.text, protected: bool = False
) -> InboundMessage:
    return InboundMessage(
        source=SOURCE,
        message_ids=[1],
        media_type=media,
        text=text,
        has_protected_content=protected,
    )


def test_no_filters_passes_everything():
    assert evaluate(message("anything"), FilterConfig()).passed


def test_protected_content_is_refused_before_any_other_check():
    """Reported as protected, never as merely 'filtered out' — and refused even
    when the customer's filters would have accepted it."""
    decision = evaluate(message("keep", protected=True), FilterConfig(keyword_include=["keep"]))
    assert not decision.passed
    assert decision.reason_code == reasons.PROTECTED_CONTENT


def test_service_messages_are_skipped():
    decision = evaluate(message(media=MediaType.service), FilterConfig())
    assert not decision.passed
    assert decision.reason_code == reasons.UNSUPPORTED_MESSAGE


def test_media_filter_selects_only_listed_types():
    config = FilterConfig(media_types=["photo", "video"])
    assert evaluate(message(media=MediaType.photo), config).passed
    assert not evaluate(message(media=MediaType.document), config).passed
    assert (
        evaluate(message(media=MediaType.document), config).reason_code
        == reasons.FILTERED_MEDIA_TYPE
    )


def test_media_aliases_are_accepted():
    assert normalize_media_types(["image", "GIF", "file"]) == {"photo", "animation", "document"}
    assert evaluate(message(media=MediaType.photo), FilterConfig(media_types=["image"])).passed


def test_include_list_requires_a_match():
    config = FilterConfig(keyword_include=["launch"])
    assert evaluate(message("product launch today"), config).passed
    decision = evaluate(message("nothing relevant"), config)
    assert decision.reason_code == reasons.FILTERED_KEYWORD_INCLUDE


def test_exclude_list_wins_over_include():
    config = FilterConfig(keyword_include=["launch"], keyword_exclude=["draft"])
    decision = evaluate(message("draft launch notes"), config)
    assert not decision.passed
    assert decision.reason_code == reasons.FILTERED_KEYWORD_EXCLUDE


def test_matching_is_case_insensitive():
    assert evaluate(message("LAUNCH"), FilterConfig(keyword_include=["launch"])).passed


def test_substring_mode_matches_inside_words():
    assert evaluate(message("prelaunch"), FilterConfig(keyword_include=["launch"])).passed


def test_word_mode_does_not_match_inside_words():
    config = FilterConfig(keyword_include=["launch"], keyword_match_mode="word")
    assert not evaluate(message("prelaunch"), config).passed
    assert evaluate(message("the launch is today"), config).passed
    assert evaluate(message("launch!"), config).passed


def test_unicode_keywords_work():
    assert evaluate(message("новый запуск"), FilterConfig(keyword_include=["запуск"])).passed


@pytest.mark.parametrize("blank", [[""], ["   "], []])
def test_blank_include_entries_do_not_block_everything(blank):
    assert evaluate(message("anything"), FilterConfig(keyword_include=blank)).passed


def test_captions_are_searched_for_media_messages():
    config = FilterConfig(keyword_include=["sale"])
    assert evaluate(message("big sale", media=MediaType.photo), config).passed
