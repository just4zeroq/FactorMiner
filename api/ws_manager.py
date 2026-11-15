"""WebSocket fan-out for mining/backtest progress.

A-05: the original ``broadcast`` swallowed *every* exception (including
``CancelledError``) and never removed failed connections, so
``active_connections`` grew monotonically and shutdown could not cancel a
broadcast. Iterating the live list while connect/disconnect mutate it was also
unsafe. We now take a snapshot, drop dead peers, and let cancellation propagate.
"""

from __future__ import annotations

import logging
import threading

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._lock = threading.RLock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        with self._lock:
            self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)

    async def broadcast(self, message: dict):
        with self._lock:
            targets = list(self.active_connections)
        dead: list[WebSocket] = []
        for connection in targets:
            try:
                await connection.send_json(message)
            except Exception as exc:  # a dropped peer must not stop the fan-out
                logger.debug("Dropping websocket peer after send failure: %s", exc)
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)
            try:
                await connection.close()
            except Exception:  # pragma: no cover - already closed
                pass

    async def close_all(self) -> None:
        """Explicit shutdown hook (TCP half-open peers have no disconnect event)."""
        with self._lock:
            targets = list(self.active_connections)
            self.active_connections.clear()
        for connection in targets:
            try:
                await connection.close()
            except Exception:  # pragma: no cover
                pass


manager = ConnectionManager()
