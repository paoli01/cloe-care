"""StatusStreamManager pour pousser les transitions de ticket en SSE.

Séparé du SSE chat de l'intake (intake/chat.stream_assistant_reply).
"""
import asyncio
import json
from collections import defaultdict
from typing import AsyncIterator


class StatusStreamManager:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, ticket_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers[ticket_id].append(queue)
        return queue

    def unsubscribe(self, ticket_id: str, queue: asyncio.Queue) -> None:
        if ticket_id in self._subscribers:
            try:
                self._subscribers[ticket_id].remove(queue)
            except ValueError:
                pass
            if not self._subscribers[ticket_id]:
                del self._subscribers[ticket_id]

    async def publish(self, ticket_id: str, event: dict) -> None:
        if ticket_id not in self._subscribers:
            return
        for queue in list(self._subscribers[ticket_id]):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def stream(self, ticket_id: str) -> AsyncIterator[str]:
        queue = self.subscribe(ticket_id)
        try:
            yield "data: " + json.dumps({"type": "connected"}) + "\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                    yield "data: " + json.dumps(event) + "\n\n"
                    if event.get("is_terminal"):
                        return
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            self.unsubscribe(ticket_id, queue)


STATUS_STREAM = StatusStreamManager()
