import asyncio
import sys
sys.path.append('d:/geointel-backend/services/orchestrator')
from core.mcp_client import orchestrator

async def main():
    print("Testing external scrapers and STAC...")
    # Initialize orchestrator manually since we're bypassing FastAPI
    orchestrator.sessions = {
        "mcp_intel": "http://localhost:8001/sse",
        "mcp_satellite": "http://localhost:8002/sse"
    }

    # 1. Pharmacy test
    print("\n--- PHARMACY ---")
    pharmacy = await orchestrator.mcp_rpc_call("mcp_intel", "tools/call", {
        "name": "get_pharmacies",
        "arguments": {"city": "Istanbul", "district": "Kadikoy"}
    })
    print(pharmacy)

    # 2. Events test
    print("\n--- EVENTS ---")
    events = await orchestrator.mcp_rpc_call("mcp_intel", "tools/call", {
        "name": "get_events",
        "arguments": {"city": "Istanbul"}
    })
    print(events)

    # 3. Satellite NDVI test
    print("\n--- SATELLITE NDVI ---")
    ndvi = await orchestrator.mcp_rpc_call("mcp_satellite", "tools/call", {
        "name": "calculate_ndvi",
        "arguments": {"bbox": "29.0,40.9,29.1,41.0", "time_range": "2023-05-01/2023-06-01"}
    })
    print(ndvi)

if __name__ == "__main__":
    asyncio.run(main())
