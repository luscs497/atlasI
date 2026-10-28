"""Barramento de eventos em memória (alimenta o /ws/monitor e os logs)."""

from __future__ import annotations

import asyncio
import time


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def publish(self, event: dict) -> None:
        event = {"ts": round(time.time(), 3), **event}
        for q in list(self._subs):
            if q.full():  # monitor lento não trava o capacete
                q.get_nowait()
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)
