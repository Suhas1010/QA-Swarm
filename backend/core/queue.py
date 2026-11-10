from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class SuspectJob:
    suspect_id: str
    run_id: str


class SuspectQueue:
    """Async FIFO suspect queue coordinating workers and the replay verifier."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[SuspectJob | None] = asyncio.Queue()

    async def push(self, suspect_id: str, run_id: str) -> None:
        await self._queue.put(SuspectJob(suspect_id=suspect_id, run_id=run_id))

    async def close(self) -> None:
        await self._queue.put(None)

    async def pop(self) -> SuspectJob | None:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

