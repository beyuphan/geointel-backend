import asyncio
import httpx
import os

key = "AIzaSyAblsPicgDiC5eiT4yeFJ9YmcTsL4yb8c0"
url = "https://maps.googleapis.com/maps/api/place/textsearch/json"

async def test():
    async with httpx.AsyncClient() as client:
        params = {
            "query": "benzinlik petrol shell opet",
            "key": key,
            "language": "tr",
            "location": "41.0054,39.7301",
            "radius": "50000"
        }
        res = await client.get(url, params=params)
        print("Gas:", len(res.json().get("results", [])))
        
        params["query"] = "restoran lokanta yemek"
        res = await client.get(url, params=params)
        print("Food:", len(res.json().get("results", [])))

asyncio.run(test())
