from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from second_screen_pundit.agents.config import AgentSettings
from second_screen_pundit.agents.pipeline import LiveWindowResult, PunditPipeline, is_must_say
from second_screen_pundit.agents.signals import SignalTracker, TacticalSignal, formation_key
from second_screen_pundit.engine.broadcast import BroadcastDelay
from second_screen_pundit.engine.schema import Event, MatchInfo


WINDOW_MATCH_MINUTES = 3
PENDING_SIGNAL_EXPIRY_MINUTES = 10
RECENT_LINES_FOR_COMMENTATOR = 3
logger = logging.getLogger(__name__)

BoundaryDecision = Literal["no_signal", "held_cooldown", "held_in_flight", "dispatched"]
OutcomeHandler = Callable[["WindowOutcome"], object]
# Results that carry a line for the viewer: written live, or pre-generated in cached mode.
LINE_STATUSES = frozenset({"commentary", "cached"})


def _has_line(result: LiveWindowResult | None) -> bool:
    return result is not None and result.status in LINE_STATUSES and result.line is not None


@dataclass
class BoundaryRecord:
    minute: int
    as_of: int
    decision: BoundaryDecision
    signal_kinds: tuple[str, ...] = ()


@dataclass
class WindowOutcome:
    path: Literal["fast_path", "tactical"]
    event: Event
    minute: int
    signals: tuple[TacticalSignal, ...] = ()
    result: LiveWindowResult | None = None
    elapsed_ms: int | None = None
    error: str | None = None
    ready_after_event_ms: int | None = None
    released_late_ms: int | None = None


@dataclass
class LiveDispatcher:
    """Turns a replay into live windows: a fast lane for must-say events and a tactical lane.

    The tactical lane evaluates deterministic signals every WINDOW_MATCH_MINUTES. Red cards and
    formation changes are mandatory and skip the Director model; the other signals go to the
    Director, but only when the tactical cooldown allows. A tactical run is stale once a newer
    tactical window has been dispatched, and a stale result is dropped.
    """

    pipeline: PunditPipeline
    info: MatchInfo
    events: list[Event]
    settings: AgentSettings
    delay: BroadcastDelay | None = None
    on_outcome: OutcomeHandler | None = None
    # Called, without awaiting, as soon as a line is ready and before it waits for release,
    # so audio can be synthesised during the broadcast delay.
    on_line_ready: OutcomeHandler | None = None
    outcomes: list[WindowOutcome] = field(default_factory=list)
    boundaries: list[BoundaryRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._tracker = SignalTracker(self.info)
        self._pending: dict[str, TacticalSignal] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._next_boundary = WINDOW_MATCH_MINUTES
        self._last_boundary_seq = 0
        self._last_dispatch_minute: int | None = None
        self._last_line_minute: int | None = None
        self._generation = 0
        self._in_flight = 0
        self._recent_lines: list[str] = []
        # Lines already sent. In cached mode a pre-generated line can match two windows (the
        # goal's fast path and the next tactical window, which holds the goal's shot).
        self._published_line_ids: set[str] = set()
        # Formation changes the fast path has announced; the tactical lane skips their signal,
        # so each change is heard once.
        self._announced_formations: set[str] = set()

    async def run(
        self,
        source: AsyncIterator[Event],
        *,
        replay_stopped: Callable[[], bool] | None = None,
    ) -> None:
        try:
            async for event in source:
                await self._on_event(event)
            if replay_stopped is not None and replay_stopped():
                await self.cancel()
            else:
                await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception:
            await self.cancel()
            raise

    async def cancel(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _clock(event: Event) -> int:
        return event.minute + event.added_minute

    async def _on_event(self, event: Event) -> None:
        if self.delay is not None:
            self.delay.stamp(event)
        must_say = is_must_say(event, self.events)
        if must_say and event.type == "formation_change":
            self._announced_formations.add(formation_key(event.seq))
        clock = self._clock(event)
        if clock >= self._next_boundary:
            await self._tactical_boundary(event, min(90, clock))
            while clock >= self._next_boundary:
                self._next_boundary += WINDOW_MATCH_MINUTES
            self._last_boundary_seq = event.seq
        if must_say:
            self._tasks.append(asyncio.create_task(self._run_fast(event, min(90, clock))))

    @staticmethod
    def _gap(now: int, last: int | None) -> float:
        return float("inf") if last is None else now - last

    async def _tactical_boundary(self, event: Event, now: int) -> None:
        """Evaluate the boundary's signals and maybe dispatch a tactical window.

        The signal evaluation and the window scan are CPU work (review 2 measured up to 0.4 s
        at a boundary), so they run in a worker thread and the event loop keeps serving every
        replay's stream. Only this coroutine touches the tracker, and it is awaited before the
        next event is read, so the thread never races the loop.
        """
        current: list[TacticalSignal] = []
        evaluated = await asyncio.to_thread(self._tracker.evaluate, self.events, as_of=event.seq)
        for signal in evaluated:
            if signal.kind == "formation_change" and signal.key in self._announced_formations:
                continue
            if signal.one_shot:
                self._pending[signal.key] = signal
            else:
                current.append(signal)
        self._pending = {
            key: signal
            for key, signal in self._pending.items()
            if now - signal.clock <= PENDING_SIGNAL_EXPIRY_MINUTES
        }
        candidates = [*self._pending.values(), *current]
        if not candidates:
            self.boundaries.append(BoundaryRecord(now, event.seq, "no_signal"))
            return

        since_dispatch = self._gap(now, self._last_dispatch_minute)
        since_line = self._gap(now, self._last_line_minute)
        mandatory = [signal for signal in candidates if signal.mandatory]
        others = [signal for signal in candidates if not signal.mandatory]
        dispatch_ok = since_dispatch >= self.settings.cooldown_match_minutes
        line_ok = since_line >= self.settings.tactical_cooldown_match_minutes
        rate_ok = dispatch_ok and line_ok and self._in_flight == 0
        if mandatory and dispatch_ok:
            chosen = mandatory + (others if rate_ok else [])
        elif not mandatory and others and rate_ok:
            chosen = others
        else:
            held: BoundaryDecision = (
                "held_in_flight" if dispatch_ok and line_ok and self._in_flight else "held_cooldown"
            )
            self.boundaries.append(
                BoundaryRecord(now, event.seq, held, tuple(signal.kind for signal in candidates))
            )
            return

        self._pending = {
            key: signal for key, signal in self._pending.items() if signal not in chosen
        }
        self._generation += 1
        generation = self._generation
        self._last_dispatch_minute = now
        self.boundaries.append(
            BoundaryRecord(now, event.seq, "dispatched", tuple(signal.kind for signal in chosen))
        )
        window = await asyncio.to_thread(self._tactical_window, self._last_boundary_seq, event.seq)
        self._in_flight += 1
        self._tasks.append(
            asyncio.create_task(self._run_tactical(event, now, window, chosen, generation))
        )

    def _tactical_window(self, after_seq: int, as_of: int) -> list[Event]:
        """The ordinary (not must-say) events since the last boundary, or the latest one."""
        return [
            candidate
            for candidate in self.events
            if after_seq < candidate.seq <= as_of and not is_must_say(candidate, self.events)
        ] or [
            next(
                candidate
                for candidate in reversed(self.events)
                if candidate.seq <= as_of and not is_must_say(candidate, self.events)
            )
        ]

    async def _run_fast(self, event: Event, now: int) -> None:
        outcome = WindowOutcome("fast_path", event, now)
        await self._run(outcome, [event], (), None)

    async def _run_tactical(
        self,
        event: Event,
        now: int,
        window: Sequence[Event],
        signals: list[TacticalSignal],
        generation: int,
    ) -> None:
        outcome = WindowOutcome("tactical", event, now, tuple(signals))
        try:
            await self._run(outcome, window, signals, lambda: self._generation != generation)
        finally:
            self._in_flight -= 1
        if _has_line(outcome.result):
            self._last_line_minute = now

    async def _run(
        self,
        outcome: WindowOutcome,
        window: Sequence[Event],
        signals: Sequence[TacticalSignal],
        is_stale: Callable[[], bool] | None,
    ) -> None:
        started = time.monotonic()
        try:
            outcome.result = await self.pipeline.comment_on_window(
                self.info,
                self.events,
                window,
                as_of=outcome.event.seq,
                signals=signals,
                is_stale=is_stale,
                recent_lines=tuple(self._recent_lines[-RECENT_LINES_FOR_COMMENTATOR:]),
            )
            line = outcome.result.line if _has_line(outcome.result) else None
            if line is not None and line.line_id in self._published_line_ids:
                outcome.result = replace(
                    outcome.result,
                    status="skipped",
                    line=None,
                    notice="This line was already published for an earlier window.",
                )
            elif line is not None:
                self._published_line_ids.add(line.line_id)
                self._recent_lines.append(line.text)
        except Exception as exc:  # noqa: BLE001 - one failed window must not stop the replay
            outcome.error = f"{type(exc).__name__}: {str(exc)[:160]}"
            logger.warning(
                "commentary_window_failed match_id=%s event_id=%s error=%s",
                self.info.match_id,
                outcome.event.event_id,
                type(exc).__name__,
            )
        outcome.elapsed_ms = round((time.monotonic() - started) * 1000)
        if self.on_line_ready is not None and _has_line(outcome.result):
            self.on_line_ready(outcome)
        if self.delay is not None and _has_line(outcome.result):
            seen = self.delay.seen_at(outcome.event.seq)
            outcome.ready_after_event_ms = round((time.monotonic() - seen) * 1000)
            outcome.released_late_ms = round(
                await self.delay.wait_released(outcome.event.seq) * 1000
            )
        self.outcomes.append(outcome)
        if self.on_outcome is not None:
            result = self.on_outcome(outcome)
            if asyncio.iscoroutine(result):
                await result
