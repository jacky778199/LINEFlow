import asyncio
import json
import logging
from typing import Dict, List, Set
from fastapi import WebSocket

logger = logging.getLogger("lineflow.ws.connection")

class ConnectionManager:
    def __init__(self):
        # instance_id -> Set of WebSockets
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, instance_id: str):
        await websocket.accept()
        await self.register(websocket, instance_id)

    async def register(self, websocket: WebSocket, instance_id: str):
        """Register an already-accepted WebSocket (do NOT call accept() again)."""
        async with self._lock:
            if instance_id not in self.active_connections:
                self.active_connections[instance_id] = set()
            self.active_connections[instance_id].add(websocket)
        logger.info(f"WebSocket client connected for instance '{instance_id}'. Total: {len(self.active_connections[instance_id])}")

    async def disconnect(self, websocket: WebSocket, instance_id: str):
        async with self._lock:
            if instance_id in self.active_connections:
                self.active_connections[instance_id].discard(websocket)
                if not self.active_connections[instance_id]:
                    del self.active_connections[instance_id]
        logger.info(f"WebSocket client disconnected for instance '{instance_id}'")

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        try:
            await websocket.send_text(json.dumps(message, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"Failed to send message to client: {e}")

    async def broadcast_new_message(self, message: dict, instance_id: str):
        """廣播新訊息給所有監聽該 instance 的連線"""
        payload = {
            "type": "new_message",
            "data": message
        }
        await self.broadcast_event(payload, instance_id)

    async def broadcast_event(self, payload: dict, instance_id: str):
        text = json.dumps(payload, ensure_ascii=False)
        async with self._lock:
            sockets = list(self.active_connections.get(instance_id, []))

        async def send(ws):
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=2)
            except Exception as e:
                logger.warning(f"Error broadcasting to socket: {e}")
        await asyncio.gather(*(send(ws) for ws in sockets))
