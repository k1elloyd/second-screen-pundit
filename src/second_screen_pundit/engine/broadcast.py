from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable

from second_screen_pundit.engine.schema import Event


class BroadcastDelay:
    """Releases events, and the commentary about them, to viewers after a fixed delay.

    Agents read events as they are published. The delay only governs what the viewer-facing
    stream shows, so agents are still bounded by their own as_of cut-off, not by this clock.
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
        self.released: dict[int, float] = {}

    @property
    def released_as_of(self) -> int:
        return max(self.released, default=0)

    def stamp(self, event: Event) -> None:
        self._seen.setdefault(event.seq, self._clock())

    def seen_at(self, seq: int) -> float:
        return self._seen[seq]

    def release_at(self, seq: int) -> float:
        return self._seen[seq] + self.delay_seconds

    async def wait_released(self, seq: int) -> float:
        """Wait until the event is releasable; return seconds past release when already late."""
        remaining = self.release_at(seq) - self._clock()
        if remaining <= 0:
            return -remaining
        while remaining > 0:
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
