"""Personalisation, group 2: the same facts, a different experience.

Mode changes how much a line or an answer says (casual: at most one number, short answers;
analyst: the numbers and the shape). My team changes the framing of answers ("your side"),
the recap's opening, the page's accents and the crowd. The evidence check is the same in
every case: framing words are read as the team's own name, so the facts cited don't move.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request

from second_screen_pundit.agents.colour import build_colour_candidate
from second_screen_pundit.agents.dispatcher import LiveDispatcher
from second_screen_pundit.agents.fallbacks import (
    CASUAL_FALLBACK_VARIANTS,
    FALLBACK_VARIANTS,
    fallback_lines,
)
from second_screen_pundit.agents.models import ExplainerReply, RecapReply
from second_screen_pundit.agents.wording import count_numbers, unframe
from second_screen_pundit.api import create_app
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.profile import ViewerProfile
from tests.test_api import _settings as _api_settings
from tests.test_colour_lines import _pipeline as _checking_pipeline
from tests.test_crowd_bed import NODE, _run as _run_crowd
from tests.test_polish_wording import _first, _scripted, _settings as _live_settings
from tests.test_review2_fixes import _alias, _recap_following_the_prompt, _scripted_pipeline
from tests.test_robustness import _source
from tests.test_stream_resume import _TOKENS, _Connection, _faster_clock, _scope, _stream

WEB = Path(__file__).resolve().parents[1] / "src" / "second_screen_pundit" / "web"
CASUAL = ViewerProfile(mode="casual")
ANALYST = ViewerProfile(mode="analyst")


# Commentary: the same match in each mode


def _deterministic_lines(monkeypatch: pytest.MonkeyPatch, profile: ViewerProfile):
    """Every model line is rejected (a banned phrase), so each beat gets its mode's
    deterministic line: the same moments in both modes, written two ways."""
    pipeline, _ = _scripted(monkeypatch, lambda _prompt: "An incredible moment.")
    events = generate_match("m01")
    dispatcher = LiveDispatcher(
        pipeline, get_match_info("m01"), events, _live_settings(), profile=lambda: profile
    )
    asyncio.run(dispatcher.run(_source(events)))
    return [
        outcome.result
        for outcome in dispatcher.outcomes
        if outcome.result is not None and outcome.result.line is not None
    ]


def test_the_same_match_reads_differently_in_casual_and_analyst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    casual = _deterministic_lines(monkeypatch, CASUAL)
    analyst = _deterministic_lines(monkeypatch, ANALYST)

    # The same beats, every line passing the same evidence check.
    assert [r.line.line_id for r in casual] == [r.line.line_id for r in analyst]
    assert len(casual) >= 10
    assert all(r.evidence is not None and r.evidence.passed for r in [*casual, *analyst])

    casual_numbers = [count_numbers(r.line.text) for r in casual]
    analyst_numbers = [count_numbers(r.line.text) for r in analyst]
    assert max(casual_numbers) <= 1, [r.line.text for r in casual]
    assert max(analyst_numbers) > 1
    assert statistics.mean(analyst_numbers) > statistics.mean(casual_numbers)
    assert statistics.mean(len(r.line.text) for r in analyst) > statistics.mean(
        len(r.line.text) for r in casual
    )
    # The formation changes are where the two differ most: shape in numbers, or in words.
    casual_shape = [r.line.text for r in casual if r.line.delivery_tier == "formation"]
    analyst_shape = [r.line.text for r in analyst if r.line.delivery_tier == "formation"]
    assert casual_shape and all("-" not in text.split(".")[0] for text in casual_shape)
    # Casual says what changed, in words: m01's 4-3-3 to 4-2-3-1, then the back three, both
    # while Ashbridge chase the game.
    assert any("an extra body in midfield as they chase this" in text for text in casual_shape)
    assert any("go to a back three as they chase this" in text for text in casual_shape)
    assert not any("change their shape" in text for text in casual_shape)
    assert any("3-4-3" in text for text in analyst_shape)


def test_every_casual_fallback_template_states_at_most_one_number() -> None:
    values = {
        "team": "Ashbridge Rovers", "player_name": "Juno Kestrelane", "colour": "yellow",
        "player_on_name": "Ivo Brackwater", "player_off_name": "Pell Dunmore",
        "formation": "3-4-3", "previous_formation": "4-2-3-1", "new_formation": "3-4-3",
        "back_line": ", a back three now", "shape_change": "go to a back three as they chase this",
        "chance_descriptor": "a big chance",
        "chance_descriptor_title": "A big chance", "players_on_pitch": 10, "after": 61,
        "before": 45, "window": 10, "window_words": "ten", "recent": 6,
        "score_phrase": "Ashbridge lead 2-1",
    }
    assert set(CASUAL_FALLBACK_VARIANTS) == set(FALLBACK_VARIANTS)
    for kind in CASUAL_FALLBACK_VARIANTS:
        lines = list(fallback_lines(kind, values, line_id="m01-l001", mode="casual"))
        assert lines, kind
        assert all(count_numbers(line) <= 1 for line in lines), (kind, lines)
    # The analyst set is the existing one, numbers and all.
    analyst = list(fallback_lines("formation_change", values, line_id="m01-l001", mode="analyst"))
    assert any(count_numbers(line) > 1 for line in analyst)


def test_a_casual_line_with_two_numbers_is_retried_with_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = get_match_info("m01")
    events, goal = _first("m01", "goal")
    team = info.home.name if goal.team == "home" else info.away.name

    def reply(prompt: dict[str, Any]) -> str:
        if not prompt["retry_feedback"]:
            return f"Goal for {team}, their 3rd chance on target. {prompt['score_phrase']}."
        return f"Goal for {team}. {prompt['score_phrase']}."

    pipeline, prompts = _scripted(monkeypatch, reply)
    result = asyncio.run(
        pipeline.comment_on_window(info, events, [goal], as_of=goal.seq, profile=CASUAL)
    )
    assert [prompt["mode"] for prompt in prompts] == ["casual", "casual"]
    assert "casual mode: the line states 2 numbers" in prompts[1]["retry_feedback"]
    assert result.line is not None and result.line.source == "agent"
    assert count_numbers(result.line.text) == 1
    # The same reply is fine for an analyst.
    pipeline, prompts = _scripted(monkeypatch, reply)
    analyst = asyncio.run(
        pipeline.comment_on_window(info, events, [goal], as_of=goal.seq, profile=ANALYST)
    )
    assert len(prompts) == 1 and analyst.line is not None and analyst.line.source == "agent"


def test_a_casual_line_never_gives_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    info = get_match_info("m01")
    events, goal = _first("m01", "goal")
    pipeline, prompts = _scripted(monkeypatch, lambda p: f"A goal. {p['score_phrase']}.")
    asyncio.run(
        pipeline.comment_on_window(
            info, events, [goal], as_of=goal.seq, clock_allowed=True, profile=CASUAL
        )
    )
    assert prompts[0]["clock_allowed"] is False
    assert not any("clock" in str(fact.get("label", "")).casefold() for fact in prompts[0]["facts"])


@pytest.mark.parametrize("match_id", ["m01", "m03"])
def test_casual_colour_lines_name_the_leader_with_one_number(match_id: str) -> None:
    info = get_match_info(match_id)
    events = generate_match(match_id)
    checker = _checking_pipeline()
    texts: dict[str, list[str]] = {}
    for mode in ("casual", "analyst"):
        used_versions: set[str] = set()
        used_statistics: set[str] = set()
        texts[mode] = []
        for ordinal, share in enumerate((0.2, 0.35, 0.5, 0.65, 0.8, 0.9), 1):
            as_of = int(len(events) * share)
            candidate = build_colour_candidate(
                info, events, as_of=as_of, ordinal=ordinal, used_versions=used_versions,
                used_statistics=used_statistics, mode=mode,
            )
            if candidate is None:
                continue
            result = checker.validate_colour_line(
                candidate.line, candidate.insight, info=info, events=events, as_of=as_of,
                tool_results=list(candidate.tool_results), lane=candidate.lane,
            )
            assert result.status == "commentary", result.evidence
            texts[mode].append(result.line.text)
            used_versions.add(candidate.version)
            used_statistics.update(candidate.statistic_keys)
    assert texts["casual"] and all(count_numbers(text) <= 1 for text in texts["casual"])
    assert any(count_numbers(text) == 2 for text in texts["analyst"])
    assert not any("set in a" in text or "use a" in text for text in texts["casual"])


# Q&A: framing follows My team; the facts cited don't


def _possession_answer(prompt: dict) -> ExplainerReply:
    """Answers from the side the viewer follows, as the instructions ask."""
    my_team = prompt["viewer"]["my_team"]
    fact = _alias(prompt, "Port Calder FC possession through")
    subject = "Your side" if my_team == "Port Calder FC" else "Port Calder FC"
    return ExplainerReply(
        answer=f"{subject} have had {fact['value']}% of the possession so far.",
        fact_ids=[fact["id"]],
    )


def _ask(monkeypatch: pytest.MonkeyPatch, profile: ViewerProfile, replies: list):
    events = generate_match("m01")
    info = get_match_info("m01")
    pipeline, prompts, _ = _scripted_pipeline(monkeypatch, replies)
    result = asyncio.run(
        pipeline.answer_question(
            info, events, "Who has had the ball?", as_of=700, profile=profile
        )
    )
    return result, prompts


def test_answers_are_framed_for_my_team_and_cite_the_same_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    neutral, neutral_prompts = _ask(monkeypatch, ANALYST, [_possession_answer])
    away = ViewerProfile(mode="analyst", team="away")
    framed, framed_prompts = _ask(monkeypatch, away, [_possession_answer])

    assert neutral_prompts[0]["viewer"] == {"mode": "analyst", "my_team": None}
    assert framed_prompts[0]["viewer"] == {"mode": "analyst", "my_team": "Port Calder FC"}
    assert neutral.source == framed.source == "agent"
    assert neutral.explanation.answer.startswith("Port Calder FC have had")
    assert framed.explanation.answer.startswith("Your side have had")
    # The framing changes; the facts and events behind it don't. (A fact ID carries a per-run
    # random part, so the facts are compared by name.)
    def named(fact_ids: list[str]) -> list[str]:
        return [fact_id.rsplit(":", 1)[-1] for fact_id in fact_ids]

    assert named(framed.explanation.fact_ids) == named(neutral.explanation.fact_ids)
    assert named(framed.explanation.fact_ids) == ["port_calder_fc_possession_through_41"]
    assert framed.explanation.event_ids == neutral.explanation.event_ids
    assert [c.text for c in framed.explanation.claims] == [
        c.text for c in neutral.explanation.claims
    ]
    # Without the framing, the number would have no team in its sentence and fail.
    info = get_match_info("m01")
    assert "Port Calder have had" in unframe(framed.explanation.answer, info, "away")


def test_framing_words_without_my_team_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    def framed_anyway(prompt: dict) -> ExplainerReply:
        reply = _possession_answer({**prompt, "viewer": {"my_team": "Port Calder FC"}})
        return reply

    result, prompts = _ask(monkeypatch, ANALYST, [framed_anyway, _possession_answer])
    assert "no My team is chosen" in prompts[1]["retry_feedback"]
    assert result.source == "agent" and "your side" not in result.explanation.answer.casefold()


def test_casual_answers_are_short_and_analyst_answers_may_say_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def three_sentences(prompt: dict) -> ExplainerReply:
        fact = _alias(prompt, "Port Calder FC possession through")
        return ExplainerReply(
            answer=(
                f"Port Calder FC have had {fact['value']}% of the possession. "
                "They have kept the ball well. Ashbridge Rovers have chased."
            ),
            fact_ids=[fact["id"]],
        )

    analyst, analyst_prompts = _ask(monkeypatch, ANALYST, [three_sentences])
    assert analyst.source == "agent" and len(analyst_prompts) == 1

    casual, casual_prompts = _ask(monkeypatch, CASUAL, [three_sentences, _possession_answer])
    assert "casual mode: the answer has 3 sentences; the limit is 2" in casual_prompts[1][
        "retry_feedback"
    ]
    assert casual.source == "agent" and casual.attempts == 2


def test_answer_rules_for_each_profile() -> None:
    from second_screen_pundit.agents.pipeline import _answer_profile_failures

    info = get_match_info("m01")
    stats = "Port Calder FC lead 1-0 with 57% possession, 20 shots and 2.044 xG."
    plain = "Port Calder FC lead 1-0, and they have had more of the ball."
    # Casual: plain words, at most two numbers, no xG.
    casual = _answer_profile_failures(stats, CASUAL, info)
    assert any("states 4 numbers" in failure for failure in casual)
    assert any("no xG" in failure for failure in casual)
    assert _answer_profile_failures(plain, CASUAL, info) == []
    # Analyst: the numbers are welcome.
    assert _answer_profile_failures(stats, ANALYST, info) == []
    # My team set: the answer is framed from that side at least once.
    away = ViewerProfile("analyst", "away")
    assert _answer_profile_failures(stats, away, info) == [
        "my_team is Port Calder FC: call them your side or your team at least once"
    ]
    assert _answer_profile_failures(stats.replace("Port Calder FC", "Your side"), away, info) == []


# Recap: My team's story first


def test_the_recap_leads_with_my_team_when_one_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    events = generate_match("m01")
    info = get_match_info("m01")

    def leads_with_my_team(prompt: dict) -> RecapReply:
        reply = _recap_following_the_prompt(prompt)
        return reply.model_copy(
            update={"summary": f"For {prompt['my_team']}, it ended in defeat. {reply.summary}"}
        )

    pipeline, prompts, _ = _scripted_pipeline(
        monkeypatch, [_recap_following_the_prompt, leads_with_my_team]
    )
    result = asyncio.run(
        pipeline.produce_recap(
            info, events, as_of=events[-1].seq, profile=ViewerProfile("casual", "away")
        )
    )
    assert prompts[0]["my_team"] == "Port Calder FC"
    assert "summary's first sentence must name Port Calder FC" in prompts[1]["retry_feedback"]
    assert result.source == "agent" and result.evidence.passed
    assert result.recap.summary.startswith("For Port Calder FC")
    assert result.recap.headline.startswith("Ashbridge Rovers win 3-2")

    # Neutral: no lead rule, the winner-first recap as before.
    pipeline, prompts, _ = _scripted_pipeline(monkeypatch, [_recap_following_the_prompt])
    neutral = asyncio.run(
        pipeline.produce_recap(info, events, as_of=events[-1].seq, profile=ViewerProfile())
    )
    assert prompts[0]["my_team"] is None and neutral.attempts == 1


# The page: accents, collapsed details, the crowd


def test_commentary_cards_carry_the_side_they_are_about(monkeypatch: pytest.MonkeyPatch) -> None:
    _faster_clock(monkeypatch, 10)
    settings = replace(_api_settings(delay=0.2), mode="cached")
    app = create_app(settings=settings)
    routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}

    async def run() -> list[dict]:
        async with app.router.lifespan_context(app):
            ip = "198.51.100.190"
            started = await routes["/api/matches/{match_id}/start"](
                "m01", Request(_scope("/api/matches/m01/start", ip)), speed=120,
                mode="casual", team="home",
            )
            _TOKENS[started["replay_id"]] = started["replay_token"]
            connection = _Connection(await _stream(routes, ip, started["replay_id"]))
            messages = await connection.read(
                lambda got: sum(n == "commentary" for _, n, _ in got) >= 4, timeout=40
            )
            await connection.drop()
            return [payload for _, name, payload in messages if name == "commentary"]

    lines = asyncio.run(asyncio.wait_for(run(), 50))
    assert all("line_team" in payload for payload in lines)
    assert {payload["line_team"] for payload in lines} & {"home", "away"}


def test_the_page_applies_the_profile_to_the_interface() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    # Why?: facts and events collapsed in Casual, open in Analyst.
    assert '<details class="evidence-details" id="evidence-details" open>' in html
    assert 'el("evidence-details").open = state.profile.mode === "analyst"' in script
    # Stats behind a tap in Casual.
    assert 'id="stats-reveal"' in html
    assert '.app-shell[data-mode="casual"] .stats-block:not(.revealed) .stats-table tbody td' in css
    # My team's accent on its cards and the scoreboard.
    assert 'card.dataset.team = payload.line_team' in script
    assert '.app-shell[data-team="home"] .commentary-card[data-team="home"]' in css
    assert '.app-shell[data-team="away"] .team-away' in css
    # A team name that doesn't fit falls back to Home or Away.
    assert '{ home: "Home", away: "Away" }' in script


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_my_teams_goals_swell_more_and_the_other_sides_less() -> None:
    result = _run_crowd("""
      const levels = {};
      for (const team of ["neutral", "home", "away"]) {
        bed.setMyTeam(team);
        bed.handleEvent({ type: "goal", clock: 30, team: "home" });
        levels[team] = bed.output.gain.value;
        advance(3500);
      }
      console.log(JSON.stringify(levels));
    """)
    assert result["neutral"] == pytest.approx(0.09)
    assert result["home"] == pytest.approx(0.09 * 1.4)
    assert result["away"] == pytest.approx(0.09 * 0.6)
    assert json.dumps(result)  # plain numbers


@pytest.mark.parametrize(
    ("previous", "new", "trailing", "phrase"),
    [
        ("4-2-3-1", "3-4-3", True, "go to a back three as they chase this"),
        ("4-3-3", "4-2-3-1", False, "put an extra body in midfield"),
        ("4-4-2", "4-3-3", False, "push an extra player up front"),
        ("4-2-3-1", "4-4-1", False, "drop a player from midfield"),  # m02's red-card reshape
        ("4-4-2", "4-4-1", False, "drop a player from up front"),
        ("3-4-3", "4-4-2", False, "go back to a back four"),
        (None, "4-4-2", False, "change their shape"),
    ],
)
def test_casual_shape_changes_are_described_without_formation_numbers(
    previous: str | None, new: str, trailing: bool, phrase: str
) -> None:
    from second_screen_pundit.agents.wording import shape_change_phrase

    assert shape_change_phrase(previous, new, trailing=trailing) == phrase
    line = f"Ashbridge Rovers {phrase}. Port Calder lead 2-1."
    # The score is the line's one number; "a back three" is shape, not a statistic.
    assert count_numbers(line) == 1
