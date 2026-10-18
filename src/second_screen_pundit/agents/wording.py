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

from second_screen_pundit.agents.evidence import SCORE_PATTERN, extract_numbers, mentions_score
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
# A peak goal from this minute may give its minute even soon after another clock line.
LATE_DRAMA_MINUTE = 80

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


def clock_budget_allows(lines: Sequence[str], *, late_drama: bool = False) -> bool:
    """True when one more clock line keeps the rule: none in the last 3, at most 1 in 4 overall.

    ``late_drama`` (a peak goal at LATE_DRAMA_MINUTE or later) skips the last-3 rule but not
    the 1-in-4 cap, so the late winner can be given its minute.
    """
    if not late_drama and any(mentions_clock(line) for line in lines[-RECENT_LINES_FOR_CLOCK:]):
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
    # The recap says "a goal back" for a goal that leaves a team behind; live, neither word.
    "live only": ("the winner", "consolation"),
}
# Empty comparisons: a swing in points says nothing a viewer can place.
_BANNED_PATTERNS = (
    (
        "comparison",
        "N-point swing",
        re.compile(r"\b\d+(?:\.\d+)?(?:-|\s)(?:percentage[- ])?points?\s+swing\b", re.IGNORECASE),
    ),
)
# Allowed only for a team that trailed earlier and is now level or ahead.
COMEBACK_PHRASES = ("comeback", "turnaround")
BANNED_OPENERS = (
    "and", "what a", "there it is", "here we go", "the home side", "the away side",
    "the visitors",
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
        f"{group}: {label}" for group, label, pattern in _BANNED_PATTERNS if pattern.search(text)
    )
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


# Phrases that belong to the team that just scored: on a goal line, the nearest team named
# before one of these must be the scorer.
SCORER_PHRASES = (
    "back on terms", "level it", "levelled it", "leveled it", "levels it", "draw level",
    "drawn level", "pull level", "pulled level", "the equaliser", "their equaliser",
    "an equaliser", "the opener", "opening goal", "strike first", "struck first",
    "a goal back",
)
_SCORER = [(phrase, _phrase(phrase)) for phrase in SCORER_PHRASES]


def _team_spans(text: str, info: MatchInfo) -> list[tuple[int, str]]:
    """(start, side) for every team mention, full or short form, in text order."""
    spans: list[tuple[int, int, str]] = []
    for side in ("home", "away"):
        for form in team_name(info, side).forms:
            spans.extend((match.start(), match.end(), side) for match in _phrase(form).finditer(text))
    # A short form inside a full-name match is the same mention.
    kept: list[tuple[int, str]] = []
    covered: list[tuple[int, int]] = []
    for start, end, side in sorted(spans, key=lambda item: (item[0], -item[1])):
        if any(start >= a and end <= b for a, b in covered):
            continue
        covered.append((start, end))
        kept.append((start, side))
    return kept


def scorer_phrase_failures(text: str, info: MatchInfo, scoring_side: str) -> list[str]:
    """A goal line must attach "back on terms", "the opener" and the like to the scorer."""
    failures = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        teams = _team_spans(sentence, info)
        for phrase, pattern in _SCORER:
            for match in pattern.finditer(sentence):
                before = [side for start, side in teams if start < match.start()]
                if before and before[-1] != scoring_side:
                    other = team_name(info, before[-1]).full
                    failures.append(f"scorer phrase {phrase!r} is attached to {other}")
    return failures


_COMPARISON = re.compile(
    r"\b(?:up|down)\s+from\s+\d+(?:\.\d+)?\s*(?:%|per\s*cent)", re.IGNORECASE
)
_COMPARED_PERIOD = re.compile(
    r"\b(?:in|over|during|across|for)\s+the\s+(?:previous|preceding|first|last|earlier|"
    r"opening|\d+)\b|\bbefore that\b|\bbefore the break\b|\bin the first half\b|\bearlier\b",
    re.IGNORECASE,
)


def comparison_failures(text: str) -> list[str]:
    """ "up from 45%" must say which period the 45% belongs to."""
    failures = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        for match in _COMPARISON.finditer(sentence):
            if not _COMPARED_PERIOD.search(sentence[match.end():]):
                failures.append(
                    f"comparison {match.group(0)!r} does not name the period it compares with"
                )
    return failures


# Phase words and the clock they fit: (phrase pattern, fits(minute, period)). Longest first,
# so "early in the second half" is judged before a bare "early".
_PHASES: tuple[tuple[str, re.Pattern[str], object], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE), fits)
    for label, pattern, fits in (
        ("early in the second half", r"\bearly\s+in\s+the\s+second\s+half\b",
         lambda minute, period: period == 2 and minute <= 55),
        ("late in the half", r"\blate\s+in\s+the\s+(?:first\s+)?half\b",
         lambda minute, period: period == 1 and minute >= 35),
        ("before the break", r"\bbefore\s+the\s+break\b",
         lambda minute, period: period == 1 and minute >= 35),
        ("late in the game", r"\blate\s+in\s+the\s+(?:game|match)\b",
         lambda minute, period: minute >= 80),
        ("late on", r"\blate\s+on\b", lambda minute, period: minute >= 80),
        ("early", r"\bearly(?:\s+on)?\b(?!\s+in\s+the\s+second\s+half)",
         lambda minute, period: period == 1 and minute < 15),
    )
)
_DANGLING = {
    "and", "but", "or", "the", "a", "an", "of", "to", "from", "with", "as", "in", "on", "at",
    "for", "by", "so", "is", "are", "was", "their", "his", "her",
}


def _broken(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        words = re.findall(r"[\w'’-]+", sentence)
        if len(words) < 3 or words[-1].casefold() in _DANGLING:
            return True
        if re.search(r"\ban\s+[b-df-hj-np-tv-z]", sentence, re.IGNORECASE):
            return True
    return False


def apply_phase_rules(text: str, *, minute: int, period: int) -> tuple[str, list[str]]:
    """Remove phase words that don't fit the minute; failures when the removal breaks a sentence."""
    removed: list[str] = []
    for label, pattern, fits in _PHASES:
        if fits(minute, period):  # type: ignore[operator]
            continue
        # The phase patterns have no capturing groups: 1 and 2 are the commas around them.
        surround = re.compile(rf"(,\s*)?{pattern.pattern}(\s*,)?", re.IGNORECASE)
        new = surround.sub(
            lambda match: " " if match.group(1) and match.group(2) else "", text
        )
        if new != text:
            removed.append(label)
            text = new
    if not removed:
        return text, []
    tidied = _tidy(text)
    if _broken(tidied):
        return tidied, [
            f"phase words {', '.join(map(repr, removed))} don't fit {minute}' "
            "and can't be removed cleanly"
        ]
    return tidied, []


_POSSESSIVE = re.compile(r"\b([A-Z][\w-]*s)(['’])s\b")


def fix_possessives(text: str) -> str:
    """A name ending in s takes an apostrophe only: "Ashbridge Rovers'", not "Rovers's"."""
    return _POSSESSIVE.sub(r"\1\2", text)


def end_with_punctuation(text: str) -> str:
    text = text.rstrip()
    return text if not text or text[-1] in ".!?" else text + "."


_A_MAN = re.compile(r"\ba man (short|down|light)\b", re.IGNORECASE)


def fix_idioms(text: str) -> str:
    """House wording: "a player short", never "a man short"."""
    return _A_MAN.sub(lambda match: f"a player {match.group(1).lower()}", text)


def apply_house_style(text: str, info: MatchInfo) -> str:
    """The naming pass every published line gets: idioms, possessives, team names, a full stop."""
    return end_with_punctuation(name_teams(fix_possessives(fix_idioms(text)), info))


_SCORE_WORDS = re.compile(r"\b(?:lead|leads|level|ahead|all square)\b", re.IGNORECASE)


def strip_withheld_score(text: str) -> str:
    """With no score_phrase, a trailing score sentence ("The score is level at 0-0.") is removed
    rather than the line rejected. A score anywhere else still fails the line check."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    last = sentences[-1]
    if (
        len(sentences) > 1
        and mentions_score(last)
        and _SCORE_WORDS.search(last)
        and len(last.split()) <= 8
    ):
        return " ".join(sentences[:-1])
    return text


# A score sentence from either side's view: "Port Calder lead 2-1", "Ashbridge trail 1-2".
_REPEAT_SCORE_WORDS = re.compile(
    r"\b(?:lead|leads|level|ahead|all square|trail|trails|behind)\b", re.IGNORECASE
)


def _scores(text: str) -> set[tuple[int, int]]:
    """Each score the text states, either way round ("lead 2-1" and "trail 1-2" are one)."""
    return {
        tuple(sorted((int(match.group("home")), int(match.group("away")))))  # type: ignore[misc]
        for match in SCORE_PATTERN.finditer(text)
    }


def strip_repeated_score(text: str, recent: Iterable[str]) -> str:
    """Drop a short trailing score sentence when a recent line already gave that score.

    The two live lanes write in parallel, so a tactical line written before a goal line was
    published can repeat the score a few seconds after it. Only a trailing sentence like
    strip_withheld_score's is removed; a score inside another sentence stays.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    last = sentences[-1]
    if (
        len(sentences) > 1
        and _REPEAT_SCORE_WORDS.search(last)
        and len(last.split()) <= 8
        and (stated := _scores(last))
        and stated & set().union(*(_scores(line) for line in recent))
    ):
        return " ".join(sentences[:-1])
    return text


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


# Viewer profile: Mode and My team (profile.py).

_FORMATION_TEXT = re.compile(r"\b\d{1,2}(?:-\d{1,2}){2,4}\b")
# A casual line states at most this many numbers; a score or a formation counts as one.
CASUAL_MAX_NUMBERS = 1


def count_numbers(text: str) -> int:
    """How many numbers a line states: each score and each formation once, then each other
    digit or number word (zero to ten) the evidence check would read."""
    count = 0
    for pattern in (_FORMATION_TEXT, SCORE_PATTERN):
        count += len(pattern.findall(text))
        text = pattern.sub(" ", text)
    return count + len(extract_numbers(text))


def casual_number_failures(text: str) -> list[str]:
    """Casual mode: at most one number in a line (the score phrase counts as one)."""
    count = count_numbers(text)
    if count <= CASUAL_MAX_NUMBERS:
        return []
    return [
        f"casual mode: the line states {count} numbers; keep at most one (the score phrase "
        "counts as one) and say the rest in words, such as more of the ball or a change of shape"
    ]


# "Your side" and "your team" stand for My team in a framed answer, with an optional
# possessive. They are framing only: the evidence check reads the team's own name instead.
_FRAMING = re.compile(r"\b(?P<word>[Yy]our) (?:side|team)(?P<possessive>['’]s)?\b")


def has_framing(text: str) -> bool:
    return _FRAMING.search(text) is not None


def unframe(text: str, info: MatchInfo, team: str) -> str:
    """The answer as the evidence check reads it: "your side" becomes My team's name.

    The check grounds a team number only when its team is named in the same sentence, so
    the framed words must stand for the real name there, and nowhere else.
    """
    if team not in ("home", "away"):
        return text
    name = team_name(info, team).short

    def replace(match: re.Match[str]) -> str:
        possessive = match.group("possessive")
        return name + (("'" if name.endswith("s") else "'s") if possessive else "")

    return _FRAMING.sub(replace, text)


def sentence_count(text: str) -> int:
    return len([part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part])
