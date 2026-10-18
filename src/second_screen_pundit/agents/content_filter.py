"""Viewer-facing content filter: real-name denylist and betting terms only.

It runs on model-written text and on viewer questions. It never runs on system notices or
deterministic fact text, and it does not guess at proper names: ordinary football English
(possessives, "xG", "Expected Goals", "against the odds", "the stakes are high") passes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from second_screen_pundit.engine.schema import MatchInfo

_KNOWN_REAL_NAME_FINGERPRINTS = {
    "b93b9776163702f1fad6cbaf815326a41b3285d0961f4e838ebdb8ad52e5f16e",
    "9e861941ad8bf5bcb649e5fde92d712528200a216018c2437371498e6ab7683d",
    "15c6d611193988e468c7431229c59ce13b0407fba24f11d36c42680d7fa11e98",
    "1ecd41c03ef78bd6daeaa6bb008896607a8413bf8ba6266be80327554b370a9e",
    "cd111a88b18961c729263aa10f90848935da373f7480179c402427de838141c7",
    "c762f2e9d54d8014de25fba5bd49265b5e75d504c514be6aac08ead41b71c72a",
    "7c8159dd2e49a576176f5461ea9d8919abdfd0ed4e29e2b4c1c7b98a8060bf40",
}
_MAX_NAME_WORDS = 5

# "odds" is betting talk except in the idioms "against the odds" and "against all odds".
# "stakes" is not matched: "the stakes are high" is ordinary English.
_BETTING_TERMS = re.compile(
    r"\b(?:bet(?:s|ting|tor|tors)?|wager(?:s|ed|ing)?|gambl(?:e|es|ed|er|ers|ing)|"
    r"book(?:maker|makers|ie|ies)|accumulators?|handicap betting|spread betting)\b"
    r"|(?<!against the )(?<!against all )\bodds\b",
    re.IGNORECASE,
)

OUT_OF_SCOPE_ANSWER = (
    "I only talk about this match: no betting or odds, and no real clubs, players or leagues."
)


def _canonical_words(value: str) -> list[str]:
    # Possessives fold away: "Club's" and "Clubs'" both give the bare name.
    value = re.sub(r"['’]s\b|(?<=s)['’]", "", value.casefold())
    return re.sub(r"[^a-z0-9]+", " ", value).split()


def _has_denylisted_name(text: str) -> bool:
    words = _canonical_words(text)
    for start in range(len(words)):
        for end in range(start + 1, min(start + _MAX_NAME_WORDS, len(words)) + 1):
            fingerprint = hashlib.sha256(" ".join(words[start:end]).encode("utf-8")).hexdigest()
            if fingerprint in _KNOWN_REAL_NAME_FINGERPRINTS:
                return True
    return False


def _block_reason(text: str) -> str | None:
    if _BETTING_TERMS.search(text):
        return "betting language"
    if _has_denylisted_name(text):
        return "a blocked real-club name"
    return None


def viewer_output_block_reason(texts: Iterable[str], info: MatchInfo | None = None) -> str | None:
    """Why model-written viewer text must be withheld, or None when it may be shown."""
    del info  # Kept for callers; fictional match names never match the denylist.
    return _block_reason("\n".join(texts))


def restricted_content_failures(*texts: str) -> list[str]:
    """The real-name and betting check as wording failures, for lines and the recap."""
    reason = _block_reason("\n".join(texts))
    return [f"restricted content: {reason}"] if reason else []


def question_scope_reason(question: str) -> str | None:
    """Why a viewer question is out of scope (betting or a real club), or None."""
    return _block_reason(question)
