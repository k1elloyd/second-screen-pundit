"""Deterministic wording rules applied to every line before the evidence check.

From prompts/commentator-persona.md and the polish session:

- The match clock is rationed: at most 1 line in 4 mentions it, routine lines never do, and a
  line that gives the time left doesn't also give the minute. A leading "At 72 minutes," is
  removed rather than rejected.
- A team is named in full the first time in a line and by its short form (engine/teams.py)
  after that.
- "A back three" is said only when the first number of the formation changes.

Every rule here only removes words or swaps one accepted team name for another, so a line that
was grounded stays grounded.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from second_screen_pundit.engine.schema import MatchInfo
from second_screen_pundit.engine.teams import team_name

# A line may mention the clock only if none of the last RECENT_LINES_FOR_CLOCK did, and only
# while clock lines stay at or under 1 in CLOCK_SHARE of all lines.
CLOCK_SHARE = 4
RECENT_LINES_FOR_CLOCK = CLOCK_SHARE - 1
# Delivery tiers whose lines may mention the clock; everything else is routine.
CLOCK_TIERS = frozenset(
    {"goal_peak", "goal_equaliser", "goal", "red_card", "big_chance", "formation"}
)

_LEFT = r"(?:left|remaining|to go|to play)"
_SPELLED = r"[a-z]+(?:-[a-z]+)?"
_ORDINAL = (
    r"(?:\d{1,3}(?:st|nd|rd|th)|(?:[a-z]+-)?(?:first|second|third|fourth|fifth|sixth|"
    r"seventh|eighth|ninth|tenth|eleventh|twelfth|[a-z]+teenth|[a-z]+tieth))"
)
# The minute as a clock reading: "minute 72", "at 72 minutes", "the 72nd minute", "72'".
_MINUTE_CORE = (
    rf"(?:minute\s+\d{{1,3}}\b|\d{{1,3}}\s+minutes\b(?!\s+{_LEFT})|the\s+{_ORDINAL}\s+minute\b"
    r"|\d{1,3}['’])"
)
_MINUTE = re.compile(
    rf"\bminute\s+\d{{1,3}}\b"
    rf"|\b{_ORDINAL}\s+minute\b"
    rf"|\b(?:at|after|on|by|in)\s+\d{{1,3}}\s+minutes\b(?!\s+{_LEFT})"
    r"|\b\d{1,3}\s+minutes?\s+(?:gone|played|on the clock)\b"
    r"|\b\d{1,3}['’]",
    re.IGNORECASE,
)
# "67 minutes gone": as its own sentence or a trailing clause.
_ELAPSED = re.compile(
    r"(?:(?<=[.!?])\s+|,\s*|^)(?:with\s+)?\d{1,3}\s+minutes?\s+(?:gone|played|on the clock)\b[.!]?",
    re.IGNORECASE,
)
_MINUTES_LEFT = re.compile(
    rf"\b(?:\d{{1,3}}|{_SPELLED})\s+minutes?\s+{_LEFT}\b", re.IGNORECASE
)

_SENTENCE_START = r"(?P<start>^|[.!?]\s+)"
_LEADING_MINUTE = re.compile(
    rf"{_SENTENCE_START}(?:at|in|on|after|by)\s+{_MINUTE_CORE}\s*[,:;–—-]?\s*",
    re.IGNORECASE,
)
_LEADING_LEFT = re.compile(
    rf"{_SENTENCE_START}(?:with\s+|at\s+)?(?:just\s+|only\s+)?(?:\d{{1,3}}|{_SPELLED})\s+minutes?\s+"
    rf"{_LEFT}\s*[,:;–—-]\s*",
    re.IGNORECASE,
)
_INNER_MINUTE = re.compile(
    rf",?\s*\b(?:at|in|on|after|by)\s+{_MINUTE_CORE}", re.IGNORECASE
)
_INNER_LEFT = re.compile(
    rf",?\s*\b(?:with\s+|and\s+|at\s+)?(?:just\s+|only\s+)?(?:\d{{1,3}}|{_SPELLED})\s+minutes?\s+"
    rf"{_LEFT}\b",
    re.IGNORECASE,
)

_BACK_LINE = re.compile(r"\b(?:a\s+)?back\s+(?P<count>three|four|five|\d)\b", re.IGNORECASE)
_BACK_WORDS = {"3": "three", "4": "four", "5": "five"}
_COUNT_DIGITS = {"three": "3", "four": "4", "five": "5"}


def mentions_minute(text: str) -> bool:
    return _MINUTE.search(text) is not None


def mentions_minutes_left(text: str) -> bool:
    return _MINUTES_LEFT.search(text) is not None


def mentions_clock(text: str) -> bool:
    """The minute or the time left, as a number; "the last 10 minutes" is a window, not the clock."""
    return mentions_minute(text) or mentions_minutes_left(text)


def _tidy(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r",\s*([,.;:!?])", r"\1", text)
    text = re.sub(r"^[,;:\s]+", "", text)
    # Capitalise the start of every sentence a removal may have exposed.
    return re.sub(
        r"(^|[.!?]\s+)([a-z])", lambda match: match.group(1) + match.group(2).upper(), text
    )


def _restart(match: re.Match[str]) -> str:
    return match.group("start")


def strip_leading_clock(text: str) -> str:
    """Remove a sentence-opening "At 72 minutes," or "At minute 72," (and "In the 72nd minute,")."""
    return _tidy(_LEADING_MINUTE.sub(_restart, text))


_ELAPSED_AND = re.compile(
    r"\bwith\s+\d{1,3}\s+minutes?\s+(?:gone|played)\s+and\s+", re.IGNORECASE
)


def _keep_end(original: str, text: str) -> str:
    """A removal at the end of a line must not take its full stop with it."""
    if text and original.rstrip()[-1:] in ".!?" and text[-1] not in ".!?":
        return text + original.rstrip()[-1]
    return text


def remove_minute(text: str) -> str:
    removed = _ELAPSED_AND.sub("with ", text)
    removed = _ELAPSED.sub("", _INNER_MINUTE.sub("", strip_leading_clock(removed)))
    return _keep_end(text, _tidy(removed))


def remove_clock(text: str) -> str:
    """Remove every clock reading: the minute and the time left."""
    removed = _INNER_LEFT.sub("", _LEADING_LEFT.sub(_restart, remove_minute(text)))
    return _keep_end(text, _tidy(removed))


def clock_budget_allows(lines: Sequence[str]) -> bool:
    """True when one more clock line keeps the rule: none in the last 3, at most 1 in 4 overall."""
    if any(mentions_clock(line) for line in lines[-RECENT_LINES_FOR_CLOCK:]):
        return False
    used = sum(mentions_clock(line) for line in lines)
    return (used + 1) * CLOCK_SHARE <= len(lines) + 1


def apply_clock_rules(text: str, *, clock_allowed: bool) -> str:
    """The clock rules for one line: no leading clock, no minute beside the time left, and no
    clock at all when the line has no clock budget."""
    text = strip_leading_clock(text)
    if not clock_allowed:
        return remove_clock(text)
    if mentions_minutes_left(text) and mentions_minute(text):
        return remove_minute(text)
    return text


def name_teams(text: str, info: MatchInfo) -> str:
    """The first mention of each team in full, later mentions by its short form."""
    forms: list[tuple[str, str, str]] = []  # (spelling, side, kind)
    for side in ("home", "away"):
        name = team_name(info, side)
        forms.append((name.full, side, "full"))
        if name.short != name.full:
            forms.append((name.short, side, "short"))
    forms.sort(key=lambda item: len(item[0]), reverse=True)
    lookup = {spelling.casefold(): side for spelling, side, _ in forms}
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(spelling) for spelling, _, _ in forms) + r")(?!\w)",
        re.IGNORECASE,
    )
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        side = lookup[match.group(1).casefold()]
        name = team_name(info, side)
        if side in seen:
            return name.short
        seen.add(side)
        return name.full

    return pattern.sub(replace, text)


def name_teams_in_phrase_forms(phrase: str, info: MatchInfo) -> tuple[str, ...]:
    """A phrase such as a score phrase as it may appear in a line: in full or in short form."""
    variants = [phrase]
    for side in ("home", "away"):
        name = team_name(info, side)
        variants.extend(variant.replace(name.full, name.short) for variant in list(variants))
    return tuple(dict.fromkeys(variants))


# Banned phrases for model-written lines (prompts/commentator-persona.md, approved list).
# Whole-word, case-insensitive. A hit sends the window to its deterministic line.
BANNED_PHRASES: dict[str, tuple[str, ...]] = {
    "hype": ("unbelievable", "incredible", "sensational", "insane", "what a moment"),
    "betting": ("odds", "betting", "bet", "accumulator", "favourites to win"),
    "invented detail": (
        "the crowd", "the home supporters", "atmosphere", "this afternoon", "tonight", "VAR",
        "injury", "injured",
    ),
    "live only": ("the winner",),
}
# Allowed only for a team that trailed earlier and is now level or ahead.
COMEBACK_PHRASES = ("comeback", "turnaround")
BANNED_OPENERS = (
    "and", "what a", "there it is", "here we go", "the home side", "the visitors",
)


def _phrase(phrase: str) -> re.Pattern[str]:
    words = r"\s+".join(re.escape(word) for word in phrase.split())
    return re.compile(rf"(?<!\w){words}(?!\w)", re.IGNORECASE)


_BANNED = [
    (group, phrase, _phrase(phrase))
    for group, phrases in BANNED_PHRASES.items()
    for phrase in phrases
]
_COMEBACK = [(phrase, _phrase(phrase)) for phrase in COMEBACK_PHRASES]
_OPENERS = [
    (opener, re.compile(rf"^\W*{_phrase(opener).pattern}", re.IGNORECASE))
    for opener in BANNED_OPENERS
]


def comeback_sides(goals: Sequence[str]) -> set[str]:
    """Sides that trailed at some point and are now level or ahead, from goals in order
    ("home"/"away" per goal)."""
    score = {"home": 0, "away": 0}
    trailed: set[str] = set()
    for side in goals:
        score[side] += 1
        for team in ("home", "away"):
            other = "away" if team == "home" else "home"
            if score[team] < score[other]:
                trailed.add(team)
    return {
        team for team in trailed
        if score[team] >= score["away" if team == "home" else "home"]
    }


def banned_phrase_hits(text: str, info: MatchInfo, goals: Sequence[str]) -> list[str]:
    """Each banned phrase the line uses, as "group: phrase"."""
    hits = [f"{group}: {phrase}" for group, phrase, pattern in _BANNED if pattern.search(text)]
    hits.extend(
        f"opener: {opener}" for opener, pattern in _OPENERS if pattern.search(text)
    )
    allowed = comeback_sides(goals)
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        for phrase, pattern in _COMEBACK:
            if not pattern.search(sentence):
                continue
            named = {
                side
                for side in ("home", "away")
                if any(
                    _phrase(form).search(sentence) for form in team_name(info, side).forms
                )
            }
            ok = bool(allowed & named) if named else bool(allowed)
            if not ok:
                hits.append(f"comeback: {phrase}")
    return list(dict.fromkeys(hits))


def back_line_note(previous: str | None, new: str | None) -> str:
    """", a back three now" when the first number of the formation changes; otherwise nothing."""
    if not previous or not new:
        return ""
    before, after = previous.split("-", 1)[0], new.split("-", 1)[0]
    if before == after or after not in _BACK_WORDS:
        return ""
    return f", a back {_BACK_WORDS[after]} now"


def back_line_failures(text: str, changes: Iterable[tuple[str, str]]) -> list[str]:
    """A back line may be described only when a formation change moved its first number."""
    changed = {
        new.split("-", 1)[0]
        for previous, new in changes
        if previous.split("-", 1)[0] != new.split("-", 1)[0]
    }
    failures = []
    for match in _BACK_LINE.finditer(text):
        raw = match.group("count").casefold()
        digit = _COUNT_DIGITS.get(raw, raw)
        if digit not in changed:
            failures.append(
                f"{match.group(0).strip()!r}: describe the back line only when the first number "
                "of the formation changes; otherwise say what changed in midfield or attack, "
                "or leave it out"
            )
    return failures
