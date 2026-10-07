import asyncio
from contextlib import asynccontextmanager
import logging
import os
import base64
import hmac
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query, Header
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

from .config import load_config
from .core.engine import LineFlowEngine
from .db.database import Database
from .db.repository import MessageRepository
from .ws.connection import ConnectionManager
from .ws.router import create_ws_router

# 設定日誌格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("lineflow.main")

config = load_config("config.yaml")
db = Database(config.database.path)
repository = MessageRepository(db)
connection_manager = ConnectionManager()
engines: Dict[str, LineFlowEngine] = {}

# 允許的 WebSocket Origin 清單（由 LINEFLOW_ALLOWED_ORIGINS 設定，逗號分隔）
# 例：LINEFLOW_ALLOWED_ORIGINS=https://your-domain.duckdns.org
_origins_env = os.environ.get("LINEFLOW_ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS: List[str] = [
    o.strip() for o in _origins_env.split(",") if o.strip()
] or ["*"]  # 未設定時允許所有來源（僅適合本機開發）

main_loop: Optional[asyncio.AbstractEventLoop] = None

# 註冊新訊息回調函數以進行執行緒安全的 WebSocket 廣播
def make_on_new_message(instance_id: str):
    def on_new_message(msg: dict):
        if main_loop and main_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                connection_manager.broadcast_new_message(msg, instance_id),
                main_loop
            )
        else:
            logger.warning("Main event loop not available for broadcasting")
    return on_new_message

# 初始化各實例引擎
for inst_id, inst_cfg in config.instances.items():
    if inst_cfg.enabled:
        engines[inst_id] = LineFlowEngine(
            instance_id=inst_id,
            config=inst_cfg,
            repository=repository,
            on_new_message=make_on_new_message(inst_id),
            on_sync_event=lambda event, instance=inst_id: connection_manager.broadcast_event(event, instance)
        )

@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()

    # 伺服器啟動時，依序啟動已啟用的實例引擎
    logger.info("Initializing LineFlow engines...")
    for inst_id, engine in engines.items():
        await engine.start()
    yield
    # 伺服器關閉時，安全停止引擎
    logger.info("Shutting down LineFlow engines...")
    for inst_id, engine in engines.items():
        await engine.stop()


# ── Custom CORS middleware ────────────────────────────────────────────────────
# Starlette 1.7+ CORSMiddleware auto-calls websocket.accept() to inspect
# headers, which causes a double-accept RuntimeError in our WS handler.
# This middleware only applies CORS to plain HTTP requests; WebSocket
# connections pass through untouched so router.py handles accept() exactly once.
class HttpOnlyCORSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.scope["type"] == "websocket":
            # Let WS connections bypass CORS entirely — auth is done in-band
            return await call_next(request)

        origin = request.headers.get("origin", "")
        response = await call_next(request)

        if ALLOWED_ORIGINS == ["*"] or origin in ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin or "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "*"

        return response

app = FastAPI(title="LineFlow Gateway Server", version="1.0.0", lifespan=lifespan)
app.add_middleware(HttpOnlyCORSMiddleware)

# 掛載健康檢查端點
@app.get("/health")
async def health_check():
    from fastapi.responses import JSONResponse
    instances = {}
    for inst_id, engine in engines.items():
        try:
            alive = await asyncio.wait_for(asyncio.to_thread(engine.device_mgr.is_alive), 3)
        except Exception:
            alive = False
        groups = engine.sync_status.snapshot()
        monitor_alive = bool(engine._monitor_task and not engine._monitor_task.done())
        poll_alive = bool(engine._poll_task and not engine._poll_task.done())
        healthy = alive and monitor_alive and poll_alive and all(g["effective_level"] == "ok" for g in groups)
        # Detailed group names/reasons are available only through authenticated WS.
        instances[inst_id] = dict(alive=alive, serial=engine.config.adb_serial,
            healthy=healthy, monitor_alive=monitor_alive, poll_alive=poll_alive,
            group_count=len(groups), warning_groups=sum(g["effective_level"] == "warning" for g in groups),
            critical_groups=sum(g["effective_level"] == "critical" for g in groups),
            pending_groups=sum(g["last_complete_at"] is None for g in groups))
    healthy = all(i["healthy"] for i in instances.values())
    return JSONResponse({"status": "ok" if healthy else "degraded", "instances": instances},
                        status_code=200 if healthy else 503)

# 取得目前畫面截圖端點 (HTTP REST)
@app.get("/instances/{instance_id}/screenshot")
async def get_instance_screenshot(
    instance_id: str,
    token: Optional[str] = Query(None, description="Auth token via query parameter"),
    authorization: Optional[str] = Header(None, description="Bearer token"),
    x_auth_token: Optional[str] = Header(None, description="Auth token via custom header"),
    format: str = Query("jpeg", description="Image format: jpeg or png"),
    quality: int = Query(80, ge=1, le=100, description="JPEG quality 1-100"),
    raw: bool = Query(True, description="Return raw image bytes directly if true; JSON with base64 if false")
):
    """取得特定 instance 模擬器的目前螢幕畫面截圖"""
    provided_token = ""
    if token:
        provided_token = token
    elif x_auth_token:
        provided_token = x_auth_token
    elif authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided_token = parts[1]
        else:
            provided_token = authorization

    if not config.server.auth_token or not hmac.compare_digest(provided_token, config.server.auth_token):
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing auth token")

    engine = engines.get(instance_id)
    if not engine:
        raise HTTPException(status_code=404, detail=f"Instance '{instance_id}' not found")

    fmt = format.lower()
    if fmt not in ("jpeg", "png"):
        fmt = "jpeg"

    img_bytes, size, err = await engine.take_screenshot(format=fmt, quality=quality)
    if not img_bytes or not size:
        raise HTTPException(status_code=500, detail=f"Failed to capture screenshot: {err or 'unknown error'}")

    mime_type = "image/jpeg" if fmt == "jpeg" else "image/png"
    if raw:
        return Response(content=img_bytes, media_type=mime_type)

    b64_str = base64.b64encode(img_bytes).decode("ascii")
    return {
        "status": "success",
        "instance_id": instance_id,
        "format": fmt,
        "width": size[0],
        "height": size[1],
        "size_bytes": len(img_bytes),
        "image_base64": b64_str,
        "data_uri": f"data:{mime_type};base64,{b64_str}"
    }

# 掛載 WebSocket 路由
ws_router = create_ws_router(
    config=config,
    repository=repository,
    engines=engines,
    manager=connection_manager
)
app.include_router(ws_router)

def main():
    logger.info(f"Starting LineFlow Server on {config.server.host}:{config.server.port}...")
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level="info"
    )

if __name__ == "__main__":
    main()
