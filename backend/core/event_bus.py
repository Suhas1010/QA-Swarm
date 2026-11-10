from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class EventBus:
    """In-memory pub/sub broadcasting live frames and bug lifecycle events per run."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers[run_id].add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        subs = self._subscribers.get(run_id)
        if subs and q in subs:
            subs.remove(q)
            if not subs:
                self._subscribers.pop(run_id, None)

    async def publish(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        event = {"type": event_type, "run_id": run_id, "payload": payload}
        for q in list(self._subscribers.get(run_id, ())):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

