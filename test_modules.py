import asyncio
import sys
import json

sys.path.append('d:/geointel-backend/services/mcp_intel')
from tools.pharmacy import get_pharmacies_handler
from tools.events import get_events_handler

sys.path.append('d:/geointel-backend/services/mcp_satellite')
from tools.stac_client import SatelliteClient

async def test_pharmacy():
    print("\n--- TEST: PHARMACY ---")
    try:
        # Kadıköy coords approx: 40.990, 29.020
        res = await get_pharmacies_handler(lat=40.990, lon=29.020)
        print("Pharmacy Result:", json.dumps(res, indent=2, ensure_ascii=False)[:500] + "...")
    except Exception as e:
        print("Pharmacy Error:", e)

async def test_events():
    print("\n--- TEST: EVENTS ---")
    try:
        res = await get_events_handler(city="istanbul")
        print("Events Result:", json.dumps(res, indent=2, ensure_ascii=False)[:500] + "...")
    except Exception as e:
        print("Events Error:", e)

async def test_ndvi():
    print("\n--- TEST: NDVI (Satellite) ---")
    try:
        # A park in Istanbul
        bbox = [29.0, 40.9, 29.1, 41.0]
        client = SatelliteClient(bbox=bbox, time_range="2023-05-01/2023-06-01")
        res = client.calculate_ndvi()
        print("NDVI Result:", json.dumps(res, indent=2, ensure_ascii=False)[:500] + "...")
    except Exception as e:
        print("NDVI Error:", e)

async def main():
    await test_pharmacy()
    await test_events()
    await test_ndvi()

if __name__ == "__main__":
    asyncio.run(main())
