"""The plain-language rule preview shown before activation."""

from __future__ import annotations

from collections.abc import Sequence

#: How many destination names to spell out before summarising.
MAX_NAMED = 4


def _join(names: Sequence[str]) -> str:
    names = list(names)
    if not names:
        return "no destinations"
    if len(names) == 1:
        return names[0]
    # Naming every chat stops being useful somewhere around five; at 500 it
    # produces an unreadable wall of text in the panel and in Telegram.
    if len(names) > MAX_NAMED:
        shown = ", ".join(names[:MAX_NAMED])
        return f"{shown} and {len(names) - MAX_NAMED} more"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _duration(total_s: float) -> str:
    hours, remainder = divmod(int(total_s), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} seconds"


def build_preview(
    *,
    source_titles: Sequence[str],
    destination_titles: Sequence[str],
    forward_mode: str = "forward",
    delay_ms: int = 0,
    has_filters: bool = False,
) -> str:
    if not source_titles:
        return "Add at least one source chat to see a preview."

    sources = _join(list(source_titles))
    destinations = _join(list(destination_titles))
    verb = "forward it to" if forward_mode == "forward" else "post a copy of it to"

    sentence = (
        f"When a new eligible message appears in {sources}, {verb} {destinations}, "
        "subject to the configured filters and platform-safe processing rules."
    )
    if delay_ms:
        seconds = delay_ms / 1000
        pacing = f"{seconds:g} second{'s' if seconds != 1 else ''}"
        sentence += f" Deliveries are paced {pacing} apart"
        # The delay multiplies by destination count, so state the total plainly:
        # "1 second apart" across 500 chats means the last one lands 8 minutes later.
        total_s = seconds * max(len(destination_titles), 1)
        if total_s >= 60:
            sentence += (
                f", so one message takes about {_duration(total_s)} to reach all "
                f"{len(destination_titles)} destinations."
            )
        else:
            sentence += "."
    if has_filters:
        sentence += " Messages that do not match the filters are skipped and recorded."
    return sentence
