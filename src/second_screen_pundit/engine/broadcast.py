from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable

from second_screen_pundit.engine.schema import Event

# How often a held release checks whether the broadcast has been resumed.
PAUSE_POLL_SECONDS = 0.05


class BroadcastDelay:
    """Releases events, and the commentary about them, to viewers after a fixed delay.

    Agents read events as they are published. The delay only governs what the viewer-facing
    stream shows, so agents are still bounded by their own as_of cut-off, not by this clock.

    Pause holds every release at once, events and lines alike. Resume moves each unreleased
    release time back by the time it spent paused, so the viewer's delay stays the same.
    """

    def __init__(
        self,
        delay_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("Broadcast delay must not be negative")
        self.delay_seconds = delay_seconds
        self._clock = clock
        self._seen: dict[int, float] = {}
        # Seconds each unreleased event's release was pushed back by pauses.
        self._shift: dict[int, float] = {}
        self._paused_at: float | None = None
        self.released: dict[int, float] = {}

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = self._clock()

    def resume(self) -> None:
        if self._paused_at is None:
            return
        now = self._clock()
        for seq, seen in self._seen.items():
            if seq not in self.released:
                # An event that arrived during the pause only waits for the rest of it.
                self._shift[seq] = self._shift.get(seq, 0.0) + now - max(self._paused_at, seen)
        self._paused_at = None

    @property
    def released_as_of(self) -> int:
        return max(self.released, default=0)

    @property
    def has_pending_events(self) -> bool:
        return any(seq not in self.released for seq in self._seen)

    def stamp(self, event: Event) -> None:
        self._seen.setdefault(event.seq, self._clock())

    def seen_at(self, seq: int) -> float:
        return self._seen[seq]

    def release_at(self, seq: int) -> float:
        return self._seen[seq] + self.delay_seconds + self._shift.get(seq, 0.0)

    async def wait_released(self, seq: int) -> float:
        """Wait until the event is releasable; return seconds past release when already late.

        Nothing is released while the broadcast is paused.
        """
        remaining = self.release_at(seq) - self._clock()
        if remaining <= 0 and not self.paused:
            return -remaining
        while self.paused or remaining > 0:
            if self.paused:
                await asyncio.sleep(PAUSE_POLL_SECONDS)
            else:
                await asyncio.sleep(remaining)
            remaining = self.release_at(seq) - self._clock()
        return 0.0

    async def events(self, source: AsyncIterator[Event]) -> AsyncIterator[Event]:
        """Yield each event once it has aged by the delay, stamping arrival without blocking."""
        queue: asyncio.Queue[Event | None] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for event in source:
                    self.stamp(event)
                    await queue.put(event)
            finally:
                await queue.put(None)

        task = asyncio.create_task(pump())
        try:
            while (event := await queue.get()) is not None:
                await self.wait_released(event.seq)
                self.released[event.seq] = self._clock()
                yield event
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
