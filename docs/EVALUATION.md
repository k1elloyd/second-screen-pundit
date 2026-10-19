# Evaluation

Every number below is from a logged gate run in [DECISIONS.md](DECISIONS.md). All runs use live `gpt-5.4-mini` and the fictional fixtures. Each gate is a single replay, so the model results vary a little from run to run.

**How a gate runs.** `python -m second_screen_pundit.agents.gate --live-latency --match m01 --speed 20 [--mode casual|analyst] [--team home|away]` replays a whole match through the real pipeline and Foundry, then reports every line's source, every check that failed and why, tokens and timings. Browser gates use Playwright Chromium against the local or deployed app.

## Headline results

| Measure | Result |
| :-- | :-- |
| Model-written commentary lines, m01 at 20× | **11 of 13** in Casual + Home, **11 of 13** in Analyst + Away (target 9 of 13 for both) |
| False comparisons that reached a viewer | **0** (`comparison_failed` 0 in both modes) |
| Banned-phrase hits in model lines | **0** of 28 Commentator calls (m01 and m02) |
| Lines that reached the viewer before an event they cite | **0**, in every browser gate |
| Typed question, text on screen (deployed app) | **2.9 s** and **3.3 s** in the browser; 7 of 9 answers under 6 s |
| Offline test suite | **602 passed**, 3 live tests deselected (18 Oct 2026) |

## 1. Model-written line share, per mode

The rest of the lines are deterministic fallbacks, used when a model line fails a check twice. A fallback is always correct, but it is plainer.

| Gate (m01, 20×) | Casual + Home | Analyst + Away |
| :-- | :-- | :-- |
| Personalisation, first run | 10 of 13 | 11 of 13 |
| After the comparison check | **11 of 13** | **11 of 13** |

Earlier gates, before the viewer profile existed (analyst-style writing, no My team):

| Gate | m01 | m02 |
| :-- | :-- | :-- |
| Regression fixes (Phase 2c) | 10 of 13 | 8 of 12 |
| Polish follow-up 2 | 11 of 13 | 8 of 11 |
| Polish follow-up 3 | 10 of 13 | 8 of 11 |

Same match, same minutes, two experiences (from the first personalisation gate):

| Minute | Casual + Home | Analyst + Away |
| :-- | :-- | :-- |
| 21' | Ashbridge Rovers have more of the ball now, with longer spells under pressure against Port Calder FC. | Ashbridge Rovers have taken control, up to 53% of the possession from 31% before against Port Calder FC. |
| 67' | Ashbridge Rovers have changed shape, with the midfield tightened up as they chase this. | Ashbridge Rovers shift from a 4-3-3 to a 4-2-3-1, with the midfield tightened up and 23 minutes left. |
| 88' | Juno Kestrelane has put Ashbridge Rovers in front from a big chance. Ashbridge lead 3-2. | A big chance, and Juno Kestrelane has put Ashbridge Rovers in front again. Ashbridge lead 3-2. |

Casual lines held 0–1 numbers each; Analyst lines 0–3.

## 2. Why lines were rejected

Every failed check is logged with its rule and the word or ID that broke it. A rejected line is retried once with that reason as feedback, so most rejections never reach the viewer.

| Gate (m01, 20×) | Casual + Home | Analyst + Away |
| :-- | :-- | :-- |
| Personalisation, first run | event not supplied 6, casual line with more than one number 3, ungrounded number 1, fact not in the insight 1 | event not supplied 6 |
| After the comparison check | event not supplied 5, casual line with more than one number 1 | event not supplied 6 |
| `comparison_failed` | 0 | 0 |

"Event not supplied" means the model cited an event ID it wasn't given; the retry usually fixes it. An earlier analyst-style gate (polish, m01) shows the wider spread the checks catch: event not supplied 5, ungrounded number 3, the away leader's score written the wrong way round 2, score mismatch 2, an xG decimal 1.

The comparison check was added after a live probe found a casual answer saying "your side have had more of the ball" for a side on 43%. That exact case is now a test that fails the answer.

## 3. Wording: banned phrases and house style

| Check | m01 | m02 |
| :-- | :-- | :-- |
| Banned-phrase rejections (hype, betting, invented detail, banned openers) | 0 of 15 Commentator calls | 0 of 13 |
| A minute and "minutes left" in one line | 0 | 0 |
| "FC" after a team's first mention | 0 | 0 |
| Red-card lines | — | 1 (no duplicate) |
| Lines without final punctuation, "consolation", point swings, repeated fallback lines | 0 | 0 |

Every cached line, recap and answer is also run through the banned-phrase and evidence checks by a test.

## 4. Questions: timing and answers

**Deployed app, browser, typed question to answer text on screen:** 2.87 s and 3.28 s. Script runs: 2.28, 3.44, 2.77, 4.36, 6.27 and 3.61 s. Across all 9, 2 took over 6 s, and both were evidence-check retries: the first answer failed the check (one stated an ungrounded "57"), the retry passed, and the page showed "Checking the facts…" in between.

**Eight questions on m02 at 20× (the review 2 set, after its fixes):**

| Viewer clock | Question | Source | Time to text |
| :-- | :-- | :-- | --: |
| 15' | Who has had the better of the chances so far? | model | 4.3 s |
| 30' | How did Port Calder score their goal? | model | 3.0 s |
| 42' | *spoken:* What changed for Port Calder after the red card? | model | 6.3 s (transcript 3.4 s) |
| 50' | What changed for Port Calder after the red card? | model | 3.4 s |
| 58' | What are the odds Merehaven win this? | fixed reply, no model call | 0.004 s |
| 66' | A question comparing Merehaven with a real club | fixed reply, no model call | 0.006 s |
| 74' | Why are Merehaven camped in the final third? | model | 3.2 s |
| 84' | What formation are Port Calder playing now? | model | 2.5 s |

6 of 8 were answered by the model, all on the first attempt; the other 2 are refused by design (betting and real clubs).

**The same question in each mode** (m01, viewer's 60th minute, "Who has had the better of the match so far, and why?"), after the comparison check:
- **Casual + Home**, model, first attempt: "Port Calder FC have had the better of it so far: they lead 1-0 and have had the slight edge in possession and shots. Your side, Ashbridge Rovers, have had plenty of volume, but not the advantage."
- **Analyst + Away**, model, second attempt: "Port Calder FC lead 1-0, and your team have had the slight edge overall: Port Calder FC have 57% possession, 20 shots and 2.044 xG. Ashbridge Rovers have 43% possession, 19 shots and 2.037 xG…"

## 5. No spoilers

The viewer sees each event 10 seconds after the agents do, like a broadcast. The tests and gates check that nothing crosses that line.

**Browser gates:**

| Gate | Result |
| :-- | :-- |
| Session B, m01 (13 lines) and m02 (12 lines) | 0 lines arrived before an event they cite |
| Phase 4, deployed app, m01 in full across a stream reconnect | 1,548 events, each once and in order; 13 lines, none before an event it cites |
| Broadcast delay, m01 | all 1,548 events released 10.000–10.469 s after the agents saw them |

**Tests** (offline, in CI):
- `test_a_line_never_reaches_the_viewer_before_its_event` (`tests/test_trust_and_accuracy.py`)
- `test_commentary_waits_for_its_events_release_and_reports_lateness` (`tests/test_broadcast.py`)
- `test_typed_question_uses_viewer_release_not_newer_agent_position` and `test_spoken_question_uses_the_same_viewer_cutoff_and_returns_server_audio` (`tests/test_api.py`)
- `test_snapshot_reflects_the_viewer_position_only` (`tests/test_review2_fixes.py`)
- `test_an_insight_is_sent_only_after_its_event_is_released` (`tests/test_review3_fixes.py`)
- `test_future_event_is_rejected` and `test_event_id_may_come_from_insight_but_must_be_visible` (`tests/test_agents.py`)
- `test_signals_never_cite_events_after_as_of` (`tests/test_tactical.py`)
- `test_no_viewer_event_is_released_while_paused` and `test_resume_pushes_unreleased_events_back_by_the_pause` (`tests/test_trust_and_accuracy.py`)

The line test was checked against the bug it guards: with the release wait removed, it fails.

## 6. Line latency

From the event reaching the agents to the checked line, m01 at 20× (Phase 2c regression-fix gate): fast path median 3.9 s, slowest 6.5 s; tactical windows median 8.7 s, slowest 12.7 s, all inside the 15 s budget. The 10-second broadcast delay hides most of it: in that gate, 2 lines per match were ready after their event's release, at most 3.1 s late.

## What isn't measured

- **No standalone evaluation runner.** The build prompt's `evaluation/cases.yaml` set and its runner weren't built; the gates above take their place.
- **No cost per answer in the interface,** and no saved KQL queries. Token counts are on every `chat` span and every `pundit.turn` span in Application Insights.
- **One run per gate.** The model share moves by a line or two between runs of the same gate.
