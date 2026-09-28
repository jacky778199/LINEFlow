import json
import logging
from typing import Dict
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from ..config import AppConfig
from ..core.engine import LineFlowEngine
from ..db.repository import MessageRepository
from .connection import ConnectionManager

logger = logging.getLogger("lineflow.ws.router")

def create_ws_router(
    config: AppConfig,
    repository: MessageRepository,
    engines: Dict[str, LineFlowEngine],
    manager: ConnectionManager
) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/lineflow")
    async def websocket_endpoint(websocket: WebSocket):
        """
        WebSocket endpoint.

        Auth is performed via a first-message handshake — the token is NEVER
        passed in the URL query string to avoid it appearing in server logs.

        Client must send as its very first message:
            {"action": "auth", "token": "<LINEFLOW_AUTH_TOKEN>", "instance_id": "instance1"}

        Server responds with:
            {"type": "auth_ok"} on success
            {"type": "auth_fail", "reason": "..."} and closes on failure
        """
        await websocket.accept()

        # ── 1. Wait for auth handshake (first message only) ──────────────────
        try:
            raw = await websocket.receive_text()
            auth_data = json.loads(raw)
        except Exception:
            await websocket.send_text(json.dumps({"type": "auth_fail", "reason": "Invalid JSON in auth message"}))
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        if auth_data.get("action") != "auth":
            await websocket.send_text(json.dumps({"type": "auth_fail", "reason": "First message must be auth action"}))
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        token = auth_data.get("token", "")
        instance_id = auth_data.get("instance_id", "instance1")

        # Constant-time comparison to prevent timing attacks
        import hmac
        if not hmac.compare_digest(token, config.server.auth_token):
            logger.warning(f"Unauthorized WebSocket connection attempt (instance='{instance_id}')")
            # accept() already called at L35 — do NOT call it again
            await websocket.send_text(json.dumps({"type": "auth_fail", "reason": "Invalid token"}))
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        engine = engines.get(instance_id)
        if not engine:
            logger.warning(f"Requested instance '{instance_id}' does not exist")
            # accept() already called at L35 — do NOT call it again
            await websocket.send_text(json.dumps({"type": "auth_fail", "reason": f"Unknown instance '{instance_id}'"}))
            await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
            return

        # accept() was already done at the top — just register the connection and send auth_ok
        await manager.register(websocket, instance_id)
        await websocket.send_text(json.dumps({"type": "auth_ok", "instance_id": instance_id}))

        # ── 2. Normal message loop ────────────────────────────────────────────
        try:
            while True:
                data_text = await websocket.receive_text()

                # Basic size guard — reject payloads larger than 64 KB
                if len(data_text) > 65536:
                    logger.warning(f"Oversized message received ({len(data_text)} bytes), dropping")
                    continue

                try:
                    data = json.loads(data_text)
                except Exception:
                    logger.warning("Invalid JSON received from authenticated client")
                    continue

                action = data.get("action")

                # 心跳機制 (Ping / Pong)
                if action == "ping":
                    await manager.send_personal_message({"type": "pong"}, websocket)

                # 斷線補償查詢 (Sync)
                elif action == "sync":
                    since_seq_id = data.get("since_seq_id", 0)
                    # Validate type and range
                    if not isinstance(since_seq_id, int) or since_seq_id < 0:
                        await manager.send_personal_message({"type": "error", "message": "since_seq_id must be a non-negative integer"}, websocket)
                        continue
                    logger.info(f"Received sync request for instance '{instance_id}' since_seq_id: {since_seq_id}")
                    messages = repository.get_messages_since(
                        since_seq_id=since_seq_id,
                        instance_id=instance_id
                    )
                    await manager.send_personal_message({
                        "type": "sync_batch",
                        "since_seq_id": since_seq_id,
                        "count": len(messages),
                        "messages": messages
                    }, websocket)

                # 外部主動發送指令 (send_message)
                elif action == "send_message":
                    request_id = data.get("request_id", "")
                    target = data.get("target", "")
                    message_text = data.get("message", "")

                    if not target or not message_text:
                        await manager.send_personal_message({
                            "type": "send_result",
                            "request_id": request_id,
                            "status": "error",
                            "error_message": "Missing target or message field"
                        }, websocket)
                        continue

                    logger.info(f"Executing send_message [{request_id}] to target '{target}'")
                    success, error = await engine.send_message(target, message_text)

                    await manager.send_personal_message({
                        "type": "send_result",
                        "request_id": request_id,
                        "status": "success" if success else "error",
                        "error_message": error
                    }, websocket)

                else:
                    logger.warning(f"Unknown action received: {action}")

        except WebSocketDisconnect:
            await manager.disconnect(websocket, instance_id)
        except Exception as e:
            logger.error(f"WebSocket unexpected error: {e}", exc_info=True)
            await manager.disconnect(websocket, instance_id)

    return router
