"""Spoken text: how a displayed line should be read aloud, in British broadcast phrasing.

`to_spoken` runs in exactly one place, `SpeechSynthesizer.synthesize_mp3`, before the SSML is
built. Displayed text, the evidence check and stored lines never see its output.

Azure reads "4-2-3-1" as "four dash two dash three dash one" and "1-0" as "one to nil", so
formations, scores and minute marks are rewritten as words. Rules run most specific first, so
a formation is never read as a score and a minute range never as a score.
"""

from __future__ import annotations

import re

_D = "[-‐‑–—]"  # hyphen, Unicode hyphen, non-breaking hyphen, en and em dash
_APOS = "['’]"

_ONES = (
    "nil", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_ORDINAL_WORDS = {
    "one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
    "nine": "ninth", "twelve": "twelfth",
}


def number_words(value: int) -> str:
    """0-199 as British words; 0 is "nil", as in a scoreline."""
    if not 0 <= value < 200:
        raise ValueError("number_words covers 0-199")
    if value >= 100:
        rest = value - 100
        return "one hundred" + (f" and {number_words(rest)}" if rest else "")
    if value < 20:
        return _ONES[value]
    tens, ones = divmod(value, 10)
    return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")


def ordinal_words(value: int) -> str:
    """1-199 as an ordinal: 1 first, 22 twenty-second, 88 eighty-eighth."""
    if not 1 <= value < 200:
        raise ValueError("ordinal_words covers 1-199")
    words = number_words(value)
    head, separator, last = words.rpartition(" ")
    prefix, hyphen, tail = last.rpartition("-")
    if tail in _ORDINAL_WORDS:
        tail = _ORDINAL_WORDS[tail]
    elif tail.endswith("y"):
        tail = tail[:-1] + "ieth"
    else:
        tail += "th"
    return f"{head}{separator}{prefix}{hyphen}{tail}"


def _digit_word(value: str) -> str:
    return "zero" if value == "0" else _ONES[int(value)]


def _score_words(home: int, away: int) -> str:
    if home == away:
        return "nil-nil" if home == 0 else f"{number_words(home)}-all"
    return f"{number_words(home)}-{number_words(away)}"


# 1. Formation: 3-5 parts, each digit 1-6.
_FORMATION = re.compile(rf"\b[1-6](?:{_D}[1-6]){{2,4}}\b")
# 2. Minute range: "60-75'" or "60-75 minutes"; "between X-Y" becomes "between X and Y".
_RANGE = re.compile(
    rf"\b(?P<between>between\s+)?(?P<a>\d{{1,3}})\s*{_D}\s*(?P<b>\d{{1,3}})"
    rf"(?:(?P<apos>{_APOS})|(?=\s*minutes?\b))",
    re.IGNORECASE,
)
# 3. Stoppage time: "90+3'".
_STOPPAGE = re.compile(rf"\b(45|90|105|120)\+(\d{{1,2}}){_APOS}?")
# 4. Minute mark: "at 88'" and a bare "88'".
_MINUTE_MARK = re.compile(rf"\b(?:(?P<prep>at|in|on)\s+)?(?P<n>\d{{1,3}}){_APOS}(?!\w)", re.IGNORECASE)
# 5. "At N minutes", but not "at 38 minutes left".
_AT_MINUTES = re.compile(
    r"\b(?P<at>[Aa]t) (?P<n>\d{1,3}) minutes\b(?!\s+(?:left|remaining|to go|to play))"
)
# A digit ordinal already followed by "minute": "the 82nd minute".
_ORDINAL_MINUTE = re.compile(r"\b(?P<n>\d{1,3})(?:st|nd|rd|th)(?=\s+minute\b)")
# 6. Score: two numbers 0-15, not part of a decimal, minute, range or longer dash chain.
_SCORE = re.compile(
    rf"(?<![\d.'’\-‐‑–—])(\d{{1,2}})\s*{_D}\s*(\d{{1,2}})"
    rf"(?![\d'’]|\s*{_D}|\s*min)"
)
# 7. Percentage.
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _formation(match: re.Match[str]) -> str:
    return "-".join(_digit_word(digit) for digit in re.findall(r"\d", match.group(0)))


def _range(match: re.Match[str]) -> str:
    joiner = " and " if match.group("between") else " to "
    unit = " minutes" if match.group("apos") else ""
    return f"{match.group('between') or ''}{match.group('a')}{joiner}{match.group('b')}{unit}"


def _stoppage(match: re.Match[str]) -> str:
    return f"{number_words(int(match.group(1)))} plus {number_words(int(match.group(2)))}"


def _minute_mark(match: re.Match[str]) -> str:
    minute = int(match.group("n"))
    if not 1 <= minute < 200:
        return match.group(0)
    spoken = f"{ordinal_words(minute)} minute"
    prep = match.group("prep")
    if prep is None:
        return spoken
    article = "In the" if prep[0].isupper() else "in the"
    return f"{article} {spoken}"


def _at_minutes(match: re.Match[str]) -> str:
    minute = int(match.group("n"))
    if not 1 <= minute < 200:
        return match.group(0)
    article = "In the" if match.group("at") == "At" else "in the"
    return f"{article} {ordinal_words(minute)} minute"


def _ordinal_minute(match: re.Match[str]) -> str:
    minute = int(match.group("n"))
    return ordinal_words(minute) if 1 <= minute < 200 else match.group(0)


def _score(match: re.Match[str]) -> str:
    home, away = int(match.group(1)), int(match.group(2))
    if home > 15 or away > 15:
        return match.group(0)
    return _score_words(home, away)


def to_spoken(text: str) -> str:
    """Rewrite displayed text for speech. Idempotent: to_spoken(to_spoken(x)) == to_spoken(x)."""
    spoken = _FORMATION.sub(_formation, text)
    spoken = _RANGE.sub(_range, spoken)
    spoken = _STOPPAGE.sub(_stoppage, spoken)
    spoken = _MINUTE_MARK.sub(_minute_mark, spoken)
    spoken = _AT_MINUTES.sub(_at_minutes, spoken)
    spoken = _ORDINAL_MINUTE.sub(_ordinal_minute, spoken)
    spoken = _SCORE.sub(_score, spoken)
    return _PERCENT.sub(r"\1 per cent", spoken)
