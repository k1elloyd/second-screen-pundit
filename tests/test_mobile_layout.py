"""Mobile session, group 1: possession on the page, short team names, and the page's structure.

The layout itself (no sideways overflow at 360-430px, header height) is checked in a browser;
see docs/DECISIONS.md. These tests pin what the browser check relies on.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from second_screen_pundit.agents.pipeline import PunditPipeline
from second_screen_pundit.agents.tools.analytics import _possession_split, match_snapshot
from second_screen_pundit.api import _released_possession, create_app
from second_screen_pundit.engine.generate import generate_match
from tests.test_api import _settings

WEB = Path("src/second_screen_pundit/web")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
STYLES = (WEB / "styles.css").read_text(encoding="utf-8")


@pytest.mark.parametrize("match_id", ["m01", "m02", "m03"])
def test_every_released_event_carries_a_possession_pair_that_sums_to_100(match_id: str) -> None:
    session = SimpleNamespace(released_passes={"home": 0, "away": 0})
    events = generate_match(match_id)
    seen = 0
    for index, event in enumerate(events):
        pair = _released_possession(session, event)  # type: ignore[arg-type]
        if pair is None:
            continue
        seen += 1
        assert pair["home"] + pair["away"] == 100
        # The same definition as the commentary and Q&A possession facts: the analytics'
        # split (share of successful passes) over the events released so far. Sampled, since
        # the split is recomputed from scratch each time.
        if index % 25 == 0 or index == len(events) - 1:
            split, _ = _possession_split(events[: index + 1])
            assert pair == {"home": split["home"][1], "away": split["away"][1]}
    assert seen > 1000


def test_the_panel_matches_the_snapshot_fact_the_explainer_cites() -> None:
    events = generate_match("m02")
    session = SimpleNamespace(released_passes={"home": 0, "away": 0})
    for event in events[:900]:
        pair = _released_possession(session, event)  # type: ignore[arg-type]
    as_of = events[899].seq
    facts = match_snapshot(events, as_of=as_of)[0].facts
    whole = [
        fact.spoken_value if fact.spoken_value is not None else fact.value
        for fact in facts
        if " possession through " in fact.label
    ]
    assert [round(float(value)) for value in whole] == [pair["home"], pair["away"]]


def test_matches_list_gives_each_teams_short_form() -> None:
    settings = _settings()
    with TestClient(create_app(settings=settings, pipeline=PunditPipeline(settings, None))) as client:
        matches = client.get("/api/matches").json()
    first = next(match for match in matches if match["id"] == "m01")
    assert (first["home_short"], first["away_short"]) == ("Ashbridge", "Port Calder")


def test_pause_and_resume_are_one_toggle_and_the_footer_note_is_in_match_info() -> None:
    assert 'id="pause-button"' in INDEX and 'id="resume-button"' not in INDEX
    info = INDEX[INDEX.index('<aside class="match-info"'):INDEX.index("</aside>")]
    assert "All fixtures and players are fictional." in info
    assert INDEX.count("All fixtures and players are fictional.") == 1
    assert 'id="possession-home"' in info and 'id="possession-away"' in info


def test_the_shell_column_cannot_grow_past_the_screen() -> None:
    # The cause of the iPhone overflow: an implicit auto column grows to its widest child.
    shell = re.search(r"\.app-shell \{(.*?)\}", STYLES, re.S).group(1)
    assert "grid-template-columns: minmax(0, 1fr)" in shell
    assert "min-width: 320px" not in STYLES
    assert "overflow-wrap: anywhere" in STYLES
