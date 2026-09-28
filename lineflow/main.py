import asyncio
from contextlib import asynccontextmanager
import logging
import os
from typing import Dict, List, Optional
from fastapi import FastAPI
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
            on_new_message=make_on_new_message(inst_id)
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
def health_check():
    return {
        "status": "ok",
        "instances": {
            inst_id: {
                "alive": engine.device_mgr.is_alive(),
                "serial": engine.config.adb_serial
            }
            for inst_id, engine in engines.items()
        }
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
