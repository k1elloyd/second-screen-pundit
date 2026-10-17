from __future__ import annotations

import asyncio
from typing import Any

Message = tuple[str, dict[str, Any]]


class ViewerOutbox:
    """Everything one replay sends its viewer, in order, kept for the life of the replay.

    A message's ID is its 1-based position, so a reconnecting stream resumes after the last
    ID the browser received: nothing it already has is sent twice, and nothing is skipped.
    """

    def __init__(self) -> None:
        self._messages: list[Message] = []
        self._changed = asyncio.Event()
        self.closed = False

    def __len__(self) -> int:
        return len(self._messages)

    def put_nowait(self, message: Message) -> None:
        if self.closed:
            return
        self._messages.append(message)
        self._wake()

    async def put(self, message: Message) -> None:
        self.put_nowait(message)

    def close(self) -> None:
        """End every stream reading this outbox, and free what it held; later messages are dropped."""
        self.closed = True
        self._messages.clear()
        self._wake()

    def wake(self) -> None:
        """Return waiting readers early with nothing new, so a stream can check its state."""
        self._wake()

    def _wake(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def after(self, position: int) -> list[tuple[int, str, dict[str, Any]]]:
        return [
            (index + 1, name, payload)
            for index, (name, payload) in enumerate(self._messages[position:], start=position)
        ]

    async def read(
        self, position: int, *, timeout: float | None = None
    ) -> list[tuple[int, str, dict[str, Any]]]:
        """Messages after `position`, waiting for at least one.

        Returns an empty list when the outbox closes or is woken, and raises TimeoutError when
        nothing arrives within `timeout`.
        """
        if len(self._messages) > position or self.closed:
            return self.after(position)
        changed = self._changed
        await asyncio.wait_for(changed.wait(), timeout)
        return self.after(position)
