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

_BETTING_TERMS = re.compile(
    r"\b(?:bet(?:s|ting)?|wager(?:s|ed|ing)?|odds|gambl(?:e|es|ed|ing)|"
    r"stake(?:s|d)?|accumulator|handicap|spread betting|bookmaker)\b",
    re.IGNORECASE,
)
_CAPITALIZED_WORD = re.compile(r"[A-Z][A-Za-z'’.-]*")
_SENTENCE_START_WORDS = {
    "a",
    "about",
    "after",
    "although",
    "an",
    "and",
    "as",
    "at",
    "before",
    "but",
    "by",
    "during",
    "for",
    "from",
    "he",
    "here",
    "how",
    "if",
    "in",
    "it",
    "live",
    "later",
    "earlier",
    "meanwhile",
    "nevertheless",
    "no",
    "of",
    "on",
    "or",
    "our",
    "second",
    "first",
    "half",
    "full",
    "she",
    "so",
    "that",
    "the",
    "their",
    "there",
    "they",
    "this",
    "though",
    "to",
    "today",
    "we",
    "when",
    "where",
    "while",
    "with",
    "yes",
    "you",
}
_COMMON_FOOTBALL_PHRASES = {
    "added time",
    "extra time",
    "final third",
    "first half",
    "full time",
    "goal difference",
    "half time",
    "injury time",
    "second half",
}


def _canonical_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _name_tokens(info: MatchInfo) -> set[str]:
    names = [info.home.name, info.away.name, info.stadium]
    names.extend(
        player["name"]
        for squad in (info.home, info.away)
        for player in squad.players
        if isinstance(player.get("name"), str)
    )
    return {
        token
        for name in names
        for token in _canonical_name(name).split()
    }


def _unapproved_name(text: str, info: MatchInfo) -> bool:
    allowed = _name_tokens(info)
    matches = list(_CAPITALIZED_WORD.finditer(text))
    group: list[re.Match[str]] = []

    def group_is_unapproved(words: list[re.Match[str]]) -> bool:
        if not words:
            return False
        if _canonical_name(" ".join(word.group() for word in words)) in _COMMON_FOOTBALL_PHRASES:
            return False
        unknown = [
            _canonical_name(word.group())
            for word in words
            if _canonical_name(word.group()) not in allowed
            and _canonical_name(word.group()) not in _SENTENCE_START_WORDS
        ]
        if not unknown:
            return False
        if len(words) > 1:
            return True
        word = words[0]
        at_sentence_start = word.start() == 0 or text[: word.start()].rstrip().endswith(
            (".", "!", "?")
        )
        return not at_sentence_start

    previous: re.Match[str] | None = None
    for match in matches:
        gap = "" if previous is None else text[previous.end() : match.start()]
        if previous is not None and (
            len(gap) > 3 or any(character.isalnum() for character in gap)
        ):
            if group_is_unapproved(group):
                return True
            group = []
        group.append(match)
        previous = match
    if group_is_unapproved(group):
        return True
    return False


def viewer_output_block_reason(texts: Iterable[str], info: MatchInfo) -> str | None:
    combined = "\n".join(texts)
    if _BETTING_TERMS.search(combined):
        return "betting language"
    canonical = _canonical_name(combined)
    words = canonical.split()
    for start in range(len(words)):
        for end in range(start + 1, min(start + 6, len(words) + 1)):
            fingerprint = hashlib.sha256(" ".join(words[start:end]).encode("utf-8")).hexdigest()
            if fingerprint in _KNOWN_REAL_NAME_FINGERPRINTS:
                return "a blocked real-club name"
    if _unapproved_name(combined, info):
        return "an unapproved proper name"
    return None
