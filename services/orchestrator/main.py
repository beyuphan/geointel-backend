import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from logger import log
from core.mcp_client import orchestrator
from api.routes import router as api_router
from api.auth import router as auth_router
from api.profile import router as profile_router
from api.history import router as history_router

try:
    from tools import LOCAL_TOOLS
except ImportError:
    LOCAL_TOOLS = [] 

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Yerel araçları kaydet
    for t_def in LOCAL_TOOLS:
        orchestrator.runtime_tools.append(await orchestrator.create_proxy_tool("orchestrator", t_def))
    
    # Servisleri dinlemeye başla
    services = {
        "city": settings.MCP_CITY_URL,
        "intel": settings.MCP_INTEL_URL,
        "satellite": settings.MCP_SATELLITE_URL
    }
    for name, url in services.items():
        asyncio.create_task(orchestrator.sse_listener_loop(name, f"{url}/sse"))
        
    yield
    orchestrator.runtime_tools.clear()

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

# CORS — Mobil uygulama ve local development için izin verilen origin'ler
# Production'da bu listeyi kendi domain'inize göre güncelleyin
ALLOWED_ORIGINS = [
    "*",
    "http://localhost:3000",       # React Native Metro
    "http://localhost:8501",       # Streamlit Dashboard
    "http://localhost:19006",      # Expo Dev
    "http://geo_dashboard:8501",   # Docker internal
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Session-Id"],
)

# Routers
app.include_router(api_router)                              # Eski: /chat, /health, /ws/chat + Yeni: /api/v1/chat
app.include_router(auth_router, prefix="/api/v1")           # /api/v1/auth/register, /login, /refresh
app.include_router(profile_router, prefix="/api/v1")        # /api/v1/profile, /vehicle, /locations, /preferences
app.include_router(history_router, prefix="/api/v1")        # /api/v1/history/routes, /chat

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)