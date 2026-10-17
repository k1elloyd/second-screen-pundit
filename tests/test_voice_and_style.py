from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace

import pytest

from second_screen_pundit.agents.commentator import (
    console_tool_results,
    live_line_id,
    score_phrase,
    team_display_name,
    validate_plain_spoken_text,
)
from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.delivery import (
    HARRY_PROFILE,
    RYAN_PROFILE,
    VOICE_PROFILES,
    tier_for_event,
    voice_profile,
)
from second_screen_pundit.agents.models import DeliveryTier
from second_screen_pundit.agents.dispatcher import LiveDispatcher
from second_screen_pundit.agents.evidence import check_evidence
from second_screen_pundit.agents.fallbacks import FALLBACK_VARIANTS, fallback_lines
from second_screen_pundit.agents.models import CommentaryLine, CommentaryReply
from second_screen_pundit.agents.pipeline import PunditPipeline, is_must_say
from second_screen_pundit.agents.signals import SIGNAL_TOOL, SignalTracker, signals_tool_result
from second_screen_pundit.agents.speech import build_ssml
from second_screen_pundit.agents.tools.analytics import (
    ToolResult,
    chance_descriptor,
    goal_context,
    is_big_chance,
    key_moments,
    match_context,
    match_state,
    possession_window,
    spoken_value,
)
from second_screen_pundit.agents.tools.parameters import (
    KeyMomentsParams,
    MatchStateParams,
    PossessionWindowParams,
)
from second_screen_pundit.engine.generate import generate_match, get_match_info
from second_screen_pundit.engine.schema import Event, MatchInfo
from second_screen_pundit.engine.teams import label_side, team_name


def _settings() -> AgentSettings:
    return AgentSettings(
        mode="live",
        endpoint=None,
        model_fast=None,
        model_deep=None,
        speech_endpoint=None,
        speech_region=None,
        live_timeout_seconds=15,
        explainer_timeout_seconds=20,
        recap_timeout_seconds=45,
        importance_threshold=0.45,
        cooldown_match_minutes=4,
    )


def _line(text: str, fact_ids: Sequence[str], event_ids: Sequence[str]) -> CommentaryLine:
    return CommentaryLine(
        line_id="m02-l0001-tactical",
        text=text,
        insight_id="m02-i0001",
        tone="measured",
        fact_ids=list(fact_ids),
        event_ids=list(event_ids),
    )


def _boundaries(events: list[Event]) -> list[Event]:
    found: list[Event] = []
    next_boundary = 3
    for event in events:
        clock = event.minute + event.added_minute
        if clock >= next_boundary:
            found.append(event)
            while clock >= next_boundary:
                next_boundary += 3
    return found


def _m02_mandatory_signals() -> tuple[MatchInfo, list[Event], Event, dict[str, object]]:
    info, events = get_match_info("m02"), generate_match("m02")
    boundary = next(event for event in events if event.minute >= 42)
    signals = {
        signal.kind: signal
        for signal in SignalTracker(info).evaluate(events, as_of=boundary.seq)
        if signal.mandatory
    }
    return info, events, boundary, signals


# Team names


def test_every_label_uses_the_full_team_name_and_short_form_is_accepted() -> None:
    info = get_match_info("m02")
    port_calder = team_name(info, "home")
    assert (port_calder.full, port_calder.short) == ("Port Calder FC", "Port Calder")
    assert label_side("Port Calder FC players on the pitch at 42'", info) == "home"
    assert label_side("Port Calder players on the pitch at 42'", info) == "home"
    assert label_side("Merehaven Athletic xG, 30-40'", info) == "away"
    assert label_side("Match-level score through 42'", info) is None

    _, events, boundary, signals = _m02_mandatory_signals()
    for signal in signals.values():
        for fact in signal.facts:
            if not fact.fact_id.endswith("_opponent"):
                assert fact.label.startswith("Port Calder FC "), fact.label


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("red_card", "Port Calder are down to 10 players after a red card."),
        ("formation_change", "Port Calder have changed to a 4-4-1 shape."),
    ],
)
def test_port_calder_signal_lines_pass_the_evidence_check(kind: str, text: str) -> None:
    info, events, boundary, signals = _m02_mandatory_signals()
    signal = signals[kind]
    report = check_evidence(
        _line(text, [fact.fact_id for fact in signal.facts], signal.event_ids[:1]),  # type: ignore[attr-defined]
        events=events,
        as_of=boundary.seq,
        tool_results=[signals_tool_result([signal])],  # type: ignore[list-item]
        info=info,
    )
    assert report.passed, report.failures


def test_every_signal_names_its_opponent() -> None:
    for match_id in ("m01", "m02", "m03"):
        info, events = get_match_info(match_id), generate_match(match_id)
        tracker = SignalTracker(info)
        for boundary in _boundaries(events):
            for signal in tracker.evaluate(events, as_of=boundary.seq):
                opponent = [fact for fact in signal.facts if fact.fact_id.endswith("_opponent")]
                assert len(opponent) == 1
                assert opponent[0].value == team_name(info, signal.opponent).full


def test_m02_red_card_and_four_four_one_beats_are_produced_even_when_agents_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info, events = get_match_info("m02"), generate_match("m02")
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]

    class FailingAgent:
        async def run(self, _: str) -> SimpleNamespace:
            return SimpleNamespace(value=None)

    monkeypatch.setattr(pipeline, "_agent", lambda *_args, **_kwargs: FailingAgent())

    async def feed() -> AsyncIterator[Event]:
        for event in events:
            yield event
            await asyncio.sleep(0)

    dispatcher = LiveDispatcher(pipeline, info, events, _settings())
    asyncio.run(dispatcher.run(feed()))

    assert not [outcome.error for outcome in dispatcher.outcomes if outcome.error]
    red_card = [
        outcome
        for outcome in dispatcher.outcomes
        if outcome.path == "tactical"
        and any(signal.kind == "red_card" for signal in outcome.signals)
    ]
    reshape = [
        outcome
        for outcome in dispatcher.outcomes
        if outcome.event.type == "formation_change"
        or any(signal.kind == "formation_change" for signal in outcome.signals)
    ]
    # One line per formation change: the fast path says it, the tactical lane does not.
    assert len(reshape) == 1 and reshape[0].path == "fast_path"
    assert len(red_card) == 1
    for outcome in (*red_card, *reshape):
        result = outcome.result
        assert result is not None and result.status == "commentary"
        assert result.line is not None and result.line.source == "fallback"
        assert result.evidence is not None and result.evidence.passed
    assert "4-4-1" in reshape[0].result.line.text  # type: ignore[union-attr]
    assert "player" in red_card[0].result.line.text  # type: ignore[union-attr]
    assert red_card[0].result.line.delivery_tier == "red_card"  # type: ignore[union-attr]


def test_every_combined_red_card_and_reshape_variant_passes_for_port_calder() -> None:
    info, events, boundary, signals = _m02_mandatory_signals()
    red, reshape = signals["red_card"], signals["formation_change"]
    state = match_state(events, MatchStateParams(minute=42), as_of=boundary.seq)
    score = next(f for f in state.facts if f.label.startswith("Match-level score"))
    results = [ToolResult(tool="match_state", facts=[score]), signals_tool_result([red, reshape])]
    values = {
        **_signal_values(red),
        **_signal_values(reshape),
        "team": "Port Calder",
        "score_phrase": score_phrase(info, str(score.value)),
    }
    fact_ids = [fact.fact_id for result in results for fact in result.facts]
    for template in FALLBACK_VARIANTS["signal_red_card_formation"]:
        text = template.format(**values)
        report = check_evidence(
            _line(text, fact_ids, red.event_ids[:1]),
            events=events, as_of=boundary.seq, tool_results=results, info=info,
        )
        assert report.passed, (text, report.failures)


# Match clock and goal context


def test_goal_context_classifies_the_m01_comeback() -> None:
    events = generate_match("m01")
    goals = [event for event in events if event.type == "goal"]
    assert [goal_context(events, goal) for goal in goals] == [
        "opener",
        "goal that extends the lead",
        "consolation",
        "equaliser",
        "go-ahead goal",
    ]


def test_clock_facts_ground_minutes_and_time_remaining() -> None:
    info, events = get_match_info("m01"), generate_match("m01")
    change = next(
        event for event in events
        if event.type == "formation_change" and event.detail.get("formation") == "3-4-3"
    )
    results = console_tool_results(change, info, events)
    context = next(result for result in results if result.tool == "match_context")
    values = {fact.label.split(" at ")[0]: fact.value for fact in context.facts}
    assert values == {"Match-level clock": 72, "Match-level minutes remaining": 18}
    fact_ids = [fact.fact_id for result in results for fact in result.facts]

    def check(text: str) -> bool:
        return check_evidence(
            _line(text, fact_ids, [change.event_id]),
            events=events, as_of=change.seq, tool_results=results, info=info,
        ).passed

    assert check("Ashbridge Rovers go to a 3-4-3 in the 72nd minute.")
    assert check("Ashbridge Rovers go to a 3-4-3 with 18 minutes left.")
    assert not check("Ashbridge Rovers go to a 3-4-3 with 20 minutes left.")
    assert not check("Ashbridge Rovers go to a 3-4-3 in the 75th minute.")


def test_goal_fast_path_supplies_goal_context_and_peak_tier() -> None:
    info, events = get_match_info("m01"), generate_match("m01")
    late_goal = [event for event in events if event.type == "goal"][-1]
    results = console_tool_results(late_goal, info, events)
    context = {fact.label: fact.value for result in results for fact in result.facts}
    assert context["Ashbridge Rovers goal context at 88'"] == "go-ahead goal"
    assert tier_for_event(late_goal, goal_context="go-ahead goal", clock=88) == "goal_peak"
    equaliser = [event for event in events if event.type == "goal"][-2]
    assert tier_for_event(equaliser, goal_context="equaliser", clock=81) == "goal_equaliser"


# Spoken values, thresholds and descriptors


def test_spoken_values_round_for_speech_and_the_check_accepts_both() -> None:
    assert spoken_value(52.7, "%") == 53
    assert spoken_value(52.5, "%") == 53
    assert spoken_value(53.0, "%") is None
    assert spoken_value(1.43, "actions/min") == 1.4
    assert spoken_value(0.42, "xG") is None
    assert spoken_value(7, "shots") is None

    info, events = get_match_info("m01"), generate_match("m01")
    result = possession_window(
        events, PossessionWindowParams(start=10, end=20), as_of=events[-1].seq
    )
    home = result.facts[0]
    assert home.spoken_value is not None and home.spoken_value != home.value

    def check(text: str) -> bool:
        return check_evidence(
            _line(text, [home.fact_id], [home.event_ids[0]]),
            events=events, as_of=events[-1].seq, tool_results=[result], info=info,
        ).passed

    assert check(f"Ashbridge Rovers have {home.value}% of the possession.")
    assert check(f"Ashbridge Rovers have {home.spoken_value}% of the possession.")
    assert not check(f"Ashbridge Rovers have {int(home.spoken_value) + 1}% of the possession.")


def test_one_big_chance_threshold_everywhere() -> None:
    assert not is_big_chance(0.29) and is_big_chance(0.3)
    assert chance_descriptor(0.29) == "a half-chance"
    assert chance_descriptor(0.3) == "a big chance"
    events = generate_match("m01")
    shot = next(event for event in events if event.type == "shot")
    assert not is_must_say(shot.model_copy(update={"xg": 0.29}))
    assert is_must_say(shot.model_copy(update={"xg": 0.3}))
    moments = key_moments(events, KeyMomentsParams(limit=25), as_of=events[-1].seq)
    by_id = {event.event_id: event for event in events}
    ranked_shots = [row for row in moments.table or [] if row["label"] == "Big chance"]
    assert all(is_big_chance(by_id[row["event_id"]].xg) for row in ranked_shots)
    assert not any(
        event.type == "shot" and not is_big_chance(event.xg)
        and event.event_id in {row["event_id"] for row in ranked_shots}
        for event in events
    )


def test_xg_surge_carries_a_team_level_chance_descriptor_without_numbers() -> None:
    info, events = get_match_info("m01"), generate_match("m01")
    tracker = SignalTracker(info)
    surges = [
        signal
        for boundary in _boundaries(events)
        for signal in tracker.evaluate(events, as_of=boundary.seq)
        if signal.kind == "xg_surge"
    ]
    assert surges
    for signal in surges:
        descriptor = next(f for f in signal.facts if f.fact_id.endswith("_chance_descriptor"))
        assert descriptor.value in {"the better of the chances", "a string of clear chances"}
        assert not re.search(r"\d", str(descriptor.value))


# Recent lines and tiers on the Commentator prompt


def test_dispatcher_passes_the_last_three_lines() -> None:
    from tests.test_tactical import _StubPipeline, _dispatch

    stub = _StubPipeline()
    dispatcher = _dispatch("m01", stub)
    texts = [
        outcome.result.line.text
        for outcome in dispatcher.outcomes
        if outcome.result is not None and outcome.result.line is not None
    ]
    assert len(texts) > 3
    assert stub.recent_lines[0] == ()
    assert all(len(recent) <= 3 for recent in stub.recent_lines)
    assert any(len(recent) == 3 for recent in stub.recent_lines)
    # Each call sees a contiguous run of earlier lines, in the order they were emitted.
    for recent in stub.recent_lines:
        if recent:
            start = texts.index(recent[0])
            assert tuple(texts[start:start + len(recent)]) == recent


def test_commentator_prompt_has_recent_lines_and_tier_and_tier_is_server_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info, events = get_match_info("m01"), generate_match("m01")
    goal = [event for event in events if event.type == "goal"][-1]
    pipeline = PunditPipeline(_settings(), object())  # type: ignore[arg-type]
    supplied: list[dict[str, object]] = []

    class StubAgent:
        async def run(self, prompt: str) -> SimpleNamespace:
            payload = json.loads(prompt)
            supplied.append(payload)
            insight = payload["insight"]
            return SimpleNamespace(
                value=CommentaryReply(
                    text=f"Juno Kestrelane puts them in front late on. {payload['score_phrase']}.",
                    fact_ids=insight["fact_ids"],
                    event_ids=insight["event_ids"],
                )
            )

    monkeypatch.setattr(pipeline, "_agent", lambda *_args, **_kwargs: StubAgent())
    recent = ("One line.", "Two lines.", "Three lines.", "Four lines.")
    result = asyncio.run(
        pipeline.comment_on_window(info, events, [goal], as_of=goal.seq, recent_lines=recent)
    )

    assert supplied[0]["recent_lines"] == list(recent[-3:])
    assert "required_delivery_tier" not in supplied[0]
    assert result.line is not None and result.line.delivery_tier == "goal_peak"
    assert result.evidence is not None and result.evidence.passed


# Fallback variants


def test_fallback_variant_choice_is_random_but_seeded_by_line_id() -> None:
    values = {"team": "Port Calder", "score_phrase": "Port Calder lead 1-0", "player_name": "Quen Rillstone"}
    first = list(fallback_lines("red_card", values, line_id="m02-l0700-fast_path"))
    again = list(fallback_lines("red_card", values, line_id="m02-l0700-fast_path"))
    assert first == again
    chosen = {
        next(fallback_lines("red_card", values, line_id=f"m02-l{seq:04d}-fast_path"))
        for seq in range(1, 40)
    }
    assert len(chosen) == len(FALLBACK_VARIANTS["red_card"])


def test_variants_skip_templates_whose_placeholders_are_missing() -> None:
    values = {"team": "Port Calder", "score_phrase": "Port Calder lead 1-0"}
    lines = list(fallback_lines("goal", values, line_id="x"))
    assert lines == ["Goal for Port Calder. Port Calder lead 1-0."]


def _signal_values(signal: object) -> dict[str, object]:
    prefix = f"{SIGNAL_TOOL}:{signal.key}_"  # type: ignore[attr-defined]
    values: dict[str, object] = {}
    for fact in signal.facts:  # type: ignore[attr-defined]
        if fact.fact_id.startswith(prefix):
            name = fact.fact_id[len(prefix):].split("_", 1)[1]
            values[name] = fact.spoken_value if fact.spoken_value is not None else fact.value
    return values


_SIGNAL_KINDS = {
    "red_card": "signal_red_card",
    "formation_change": "signal_formation_change",
    "possession_swing": "possession_swing",
    "high_turnover_surge": "high_turnover_surge",
    "xg_surge": "xg_surge",
}


def test_every_signal_fallback_variant_passes_for_every_team() -> None:
    checked = 0
    for match_id in ("m01", "m02", "m03"):
        info, events = get_match_info(match_id), generate_match(match_id)
        tracker = SignalTracker(info)
        for boundary in _boundaries(events):
            for signal in tracker.evaluate(events, as_of=boundary.seq):
                kind = _SIGNAL_KINDS.get(signal.kind)
                if kind is None:
                    continue
                state = match_state(
                    events, MatchStateParams(minute=min(90, boundary.minute)), as_of=boundary.seq
                )
                score = next(f for f in state.facts if f.label.startswith("Match-level score"))
                results = [
                    ToolResult(tool="match_state", facts=[score]),
                    match_context(events, as_of=boundary.seq),
                    signals_tool_result([signal]),
                ]
                values = {
                    **_signal_values(signal),
                    "team": team_display_name(info, signal.team),
                    "score_phrase": score_phrase(info, str(score.value)),
                }
                fact_ids = [fact.fact_id for result in results for fact in result.facts]
                for template in FALLBACK_VARIANTS[kind]:
                    text = template.format(**values)
                    validate_plain_spoken_text(text)
                    assert len(text) <= 200, text
                    report = check_evidence(
                        _line(text, fact_ids, signal.event_ids[:1]),
                        events=events, as_of=boundary.seq, tool_results=results, info=info,
                    )
                    assert report.passed, (match_id, boundary.minute, text, report.failures)
                    checked += 1
    assert checked > 100


# SSML


_SSML_NS = {"s": "http://www.w3.org/2001/10/synthesis", "m": "http://www.w3.org/2001/mstts"}


def test_ssml_escapes_inserted_text_and_the_voice_name() -> None:
    ssml = build_ssml(
        'Rovers & "friends" <script> 1-0',
        voice='en-GB-"Sonia"Neural',
        tier="red_card",
    )
    root = ElementTree.fromstring(ssml)
    voice = root.find("s:voice", _SSML_NS)
    assert voice is not None and voice.get("name") == 'en-GB-"Sonia"Neural'
    # A voice without a profile gets no style at all.
    assert voice.find("m:express-as", _SSML_NS) is None
    assert voice.text == 'Rovers & "friends" <script> 1-0'
    assert "<script>" not in ssml


def test_ssml_carries_harrys_tier_style_without_a_style_degree() -> None:
    ssml = build_ssml(
        'Rovers & "friends" <script> 1-0',
        voice="en-GB-Harry:MAI-Voice-2.1-Flash",
        tier="red_card",
    )
    root = ElementTree.fromstring(ssml)
    voice = root.find("s:voice", _SSML_NS)
    assert voice is not None and voice.get("name") == "en-GB-Harry:MAI-Voice-2.1-Flash"
    express = voice.find("m:express-as", _SSML_NS)
    assert express is not None
    assert express.get("style") == "surprised"
    assert express.get("styledegree") is None
    assert express.text == 'Rovers & "friends" <script> 1-0'
    assert "<script>" not in ssml


@pytest.mark.parametrize(
    ("tier", "style"),
    [
        ("goal", "joyful"),
        ("goal_equaliser", "joyful"),
        ("goal_peak", "surprised"),
        ("big_chance", "surprised"),
        ("red_card", "surprised"),
        ("tactical", "narrator"),
        ("formation", "narrator"),
        ("substitution", "neutral"),
        ("booking", "neutral"),
        ("routine", "neutral"),
    ],
)
def test_harry_energy_tiers(tier: DeliveryTier, style: str) -> None:
    chosen = voice_profile("en-GB-Harry:MAI-Voice-2.1-Flash").style_for(tier)
    assert chosen is not None and chosen.style == style


def test_ryan_gets_only_his_documented_styles() -> None:
    ryan = voice_profile("en-GB-RyanNeural")
    assert ryan is RYAN_PROFILE
    styled = build_ssml("Port Calder lead one-nil.", voice="en-GB-RyanNeural", tier="goal")
    assert 'style="cheerful" styledegree="1.2"' in styled
    # Ryan has no style that fits a red card: none is sent.
    assert "express-as" not in build_ssml("Sent off.", voice="en-GB-RyanNeural", tier="red_card")


def test_an_unsupported_style_is_never_sent() -> None:
    from second_screen_pundit.agents.delivery import TierStyle, VoiceProfile

    profile = VoiceProfile(frozenset({"neutral"}), {"goal": TierStyle("joyful")})
    assert profile.style_for("goal") is None
    assert profile.style_for(None) is None


def test_ssml_without_a_tier_has_no_style() -> None:
    ssml = build_ssml("Plain line.", voice="en-GB-SoniaNeural")
    assert "express-as" not in ssml
    ElementTree.fromstring(ssml)


def test_every_profile_style_is_supported_by_its_voice() -> None:
    for voice, profile in VOICE_PROFILES.items():
        for tier, style in profile.tier_styles.items():
            assert style.style in profile.supported_styles, (voice, tier)
            if style.style_degree is not None:
                assert 0.01 <= style.style_degree <= 2, (voice, tier)
    # Harry has a style for every tier, and MAI voices get no styledegree.
    assert set(HARRY_PROFILE.tier_styles) == set(DeliveryTier.__args__)  # type: ignore[attr-defined]
    assert all(style.style_degree is None for style in HARRY_PROFILE.tier_styles.values())
