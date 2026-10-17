"""Spoken-text step (review 2, finding 8), with British phrasing: one-nil, one-all, nil-nil,
four-two-three-one, eighty-eighth minute, per cent."""

from __future__ import annotations

import re

import pytest

from second_screen_pundit.agents.spoken import number_words, ordinal_words, to_spoken

CASES = [
    ("Port Calder lead 1-0", "Port Calder lead one-nil"),
    ("The score is level at 1-1", "The score is level at one-all"),
    ("The score is level at 0-0", "The score is level at nil-nil"),
    ("Merehaven Athletic lead 2-1.", "Merehaven Athletic lead two-one."),
    ("Port Calder trail 0-2", "Port Calder trail nil-two"),
    ("It finished 10-0", "It finished ten-nil"),
    ("They lead 3–2", "They lead three-two"),
    ("It is 2 - 2 now", "It is two-all now"),
    (
        "switch from a 4-2-3-1 to a 4-4-1",
        "switch from a four-two-three-one to a four-four-one",
    ),
    ("now a 3-4-3, 2-1 up", "now a three-four-three, two-one up"),
    ("a 4-4-2 at 1-1", "a four-four-two at one-all"),
    ("a 3–5–2 shape", "a three-five-two shape"),
    ("65% possession, up from 44%", "65 per cent possession, up from 44 per cent"),
    ("54.6% of the ball", "54.6 per cent of the ball"),
    (
        "At 27 minutes, Port Calder have the opener",
        "In the twenty-seventh minute, Port Calder have the opener",
    ),
    ("scored at 88'", "scored in the eighty-eighth minute"),
    ("scored at 88’", "scored in the eighty-eighth minute"),
    ("in the 82nd minute", "in the eighty-second minute"),
    ("on 21'", "in the twenty-first minute"),
    ("at 22'", "in the twenty-second minute"),
    ("at 23'", "in the twenty-third minute"),
    ("at 11'", "in the eleventh minute"),
    ("at 12'", "in the twelfth minute"),
    ("at 13'", "in the thirteenth minute"),
    ("at 1'", "in the first minute"),
    ("At 40' they went ahead", "In the fortieth minute they went ahead"),
    ("a 70' substitution", "a seventieth minute substitution"),
    ("at 90+3'", "at ninety plus three"),
    ("possession in 60-75'", "possession in 60 to 75 minutes"),
    ("between 60-75 minutes", "between 60 and 75 minutes"),
    ("they have 52 minutes left", "they have 52 minutes left"),
    ("At 38 minutes left", "At 38 minutes left"),
    ("0.31 xG", "0.31 xG"),
    ("1st half", "1st half"),
    ("Port Calder FC", "Port Calder FC"),
    ("Merehaven have 19 shots", "Merehaven have 19 shots"),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_to_spoken(text: str, expected: str) -> None:
    assert to_spoken(text) == expected


@pytest.mark.parametrize(("text", "_expected"), CASES)
def test_to_spoken_is_idempotent(text: str, _expected: str) -> None:
    once = to_spoken(text)
    assert to_spoken(once) == once


@pytest.mark.parametrize(("text", "_expected"), CASES)
def test_spoken_text_has_no_dashed_numbers(text: str, _expected: str) -> None:
    assert not re.search(r"\d\s*[-–]\s*\d", to_spoken(text))


def test_formation_is_never_read_as_a_score() -> None:
    assert to_spoken("a 4-4-2") == "a four-four-two"
    assert "all" not in to_spoken("a 4-4-2")


@pytest.mark.parametrize(
    ("value", "words", "ordinal"),
    [
        (1, "one", "first"),
        (2, "two", "second"),
        (3, "three", "third"),
        (5, "five", "fifth"),
        (12, "twelve", "twelfth"),
        (20, "twenty", "twentieth"),
        (21, "twenty-one", "twenty-first"),
        (45, "forty-five", "forty-fifth"),
        (88, "eighty-eight", "eighty-eighth"),
        (90, "ninety", "ninetieth"),
        (101, "one hundred and one", "one hundred and first"),
    ],
)
def test_number_and_ordinal_words(value: int, words: str, ordinal: str) -> None:
    assert number_words(value) == words
    assert ordinal_words(value) == ordinal
