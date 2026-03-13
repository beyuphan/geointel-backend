import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from logger import log
from core.mcp_client import orchestrator
from api.routes import router as api_router

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

# CORS
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Router
app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)