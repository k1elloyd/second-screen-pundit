from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from typing import Literal

from second_screen_pundit.engine.schema import Event


class Replay:
    def __init__(self, events: Sequence[Event], speed: int) -> None:
        if not 1 <= speed <= 120:
            raise ValueError("Replay speed must be between 1 and 120")
        if not events:
            raise ValueError("Replay requires at least one event")
        self.events = tuple(events)
        self.speed = speed
        self.state: Literal["ready", "running", "paused", "finished", "stopped"] = (
            "ready"
        )
        self.as_of = 0
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscribers: set[asyncio.Queue[Event | None]] = {self._queue}
        self._running = asyncio.Event()
        self._running.set()
        self._task: asyncio.Task[None] | None = None
        self._active_elapsed_seconds = 0.0
        self._active_started_at: float | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Replay has already been started")
        self.state = "running"
        self._active_started_at = time.monotonic()
        self._task = asyncio.create_task(self._publish())

    def pause(self) -> None:
        if self.state == "running":
            self._freeze_active_clock()
            self.state = "paused"
            self._running.clear()

    def resume(self) -> None:
        if self.state == "paused":
            self.state = "running"
            self._active_started_at = time.monotonic()
            self._running.set()

    @property
    def active_elapsed_seconds(self) -> float:
        if self._active_started_at is None:
            return self._active_elapsed_seconds
        return self._active_elapsed_seconds + time.monotonic() - self._active_started_at

    def _freeze_active_clock(self) -> None:
        if self._active_started_at is not None:
            self._active_elapsed_seconds += time.monotonic() - self._active_started_at
            self._active_started_at = None

    async def wait_match_seconds(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("seconds must not be negative")
        await self._wait_interval(seconds / self.speed)

    async def stop(self) -> None:
        if self.state in {"finished", "stopped"}:
            return
        self._freeze_active_clock()
        self.state = "stopped"
        self._running.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        else:
            for subscriber in tuple(self._subscribers):
                await subscriber.put(None)

    async def control(self, action: Literal["pause", "resume"]) -> str:
        if action == "pause":
            self.pause()
        else:
            self.resume()
        return self.state

    async def stream(self) -> AsyncIterator[Event]:
        async for event in self._read_from(self._queue):
            yield event

    def subscribe(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        if self.state in {"finished", "stopped"}:
            queue.put_nowait(None)
        else:
            self._subscribers.add(queue)
        return self._read_from(queue)

    async def _read_from(self, queue: asyncio.Queue[Event | None]) -> AsyncIterator[Event]:
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            if queue is not self._queue:
                self._subscribers.discard(queue)

    async def _wait_interval(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0:
            await self._running.wait()
            started = time.monotonic()
            await asyncio.sleep(min(remaining, 0.05))
            if self._running.is_set():
                remaining -= time.monotonic() - started

    @staticmethod
    def _clock(event: Event) -> int:
        return (event.minute + event.added_minute) * 60 + event.second

    async def _publish(self) -> None:
        previous_clock: int | None = None
        try:
            for event in self.events:
                await self._running.wait()
                if previous_clock is not None:
                    elapsed = max(0, self._clock(event) - previous_clock)
                    await self._wait_interval(elapsed / self.speed)
                await self._running.wait()
                self.as_of = event.seq
                for subscriber in tuple(self._subscribers):
                    await subscriber.put(event)
                previous_clock = self._clock(event)
            self._freeze_active_clock()
            self.state = "finished"
        except asyncio.CancelledError:
            self._freeze_active_clock()
            self.state = "stopped"
            raise
        finally:
            for subscriber in tuple(self._subscribers):
                await subscriber.put(None)


class ReplayManager:
    def __init__(self, max_concurrent: int = 3) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        self.max_concurrent = max_concurrent
        self._replays: set[Replay] = set()

    def start(self, events: Sequence[Event], speed: int) -> Replay:
        replay = self.prepare(events, speed)
        replay.start()
        return replay

    def prepare(self, events: Sequence[Event], speed: int) -> Replay:
        """Reserve a replay slot so subscribers can be attached before playback starts."""
        self._replays = {
            replay for replay in self._replays if replay.state not in {"finished", "stopped"}
        }
        if len(self._replays) >= self.max_concurrent:
            raise RuntimeError("Maximum concurrent replays reached")
        replay = Replay(events, speed)
        self._replays.add(replay)
        return replay
