import httpx
import asyncpg
from fastapi import FastAPI
from pydantic import BaseModel
from config import settings
from logger import log  # <--- LOGLAMA EKLENDİ

app = FastAPI(title=settings.APP_NAME)
mcp_api = app 

# --- MODELLER ---
class LocationQuery(BaseModel):
    query: str

class WeatherQuery(BaseModel):
    lat: float
    lon: float

class RouteQuery(BaseModel):
    origin: str
    destination: str

class SavePlaceQuery(BaseModel):
    name: str
    lat: float
    lon: float
    category: str = "Genel"
    note: str = ""

async def get_db_connection():
    return await asyncpg.connect(settings.DATABASE_URL)

# --- ARAÇLAR ---

@app.post("/save_location")
async def save_location(data: SavePlaceQuery):
    conn = await get_db_connection()
    try:
        await conn.execute("""
            INSERT INTO saved_places (name, category, note, geom)
            VALUES ($1, $2, $3, ST_SetSRID(ST_MakePoint($5, $4), 4326))
        """, data.name, data.category, data.note, data.lat, data.lon)
        return {"status": "success", "message": f"{data.name} kaydedildi."}
    finally:
        await conn.close()

@app.post("/get_weather")
async def get_weather(data: WeatherQuery):
    # Timeout eklendi: 30 saniye
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": data.lat, "lon": data.lon, "appid": settings.OPENWEATHER_API_KEY , "units": "metric", "lang": "tr"}
        )
        return resp.json()

@app.post("/search_places_google")
async def search_places(data: LocationQuery):
    params = {"query": data.query, "key": settings.GOOGLE_MAPS_API_KEY, "language": "tr"}
    # Timeout eklendi: 30 saniye
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get("https://maps.googleapis.com/maps/api/place/textsearch/json", params=params)
        return resp.json()

@app.post("/get_route_data")
async def get_route(data: RouteQuery):
    # Print yerine Log kullanımı
    log.info(f"🚗 [HERE ROUTING] İstek: {data.origin} -> {data.destination}")

    if not settings.HERE_API_KEY:
        log.error("❌ [HERE ERROR] API Key Yok!")
        return {"error": "HERE API Key eksik"}
    
    # Timeout eklendi: 30 saniye
    async with httpx.AsyncClient(timeout=30.0) as client:
        url = "https://router.hereapi.com/v8/routes"
        
        params = {
            "transportMode": "car",
            "origin": data.origin.replace(" ", ""),
            "destination": data.destination.replace(" ", ""),
            "return": "summary,polyline",
            "apiKey": settings.HERE_API_KEY
        }
        
        try:
            log.info(f"📡 [HERE REQUEST] Soruluyor...")
            resp = await client.get(url, params=params)
            
            if resp.status_code != 200:
                log.error(f"❌ [HERE ERROR] Hata Döndü: {resp.text}")
                return {"error": f"HERE API Hatası: {resp.status_code} - {resp.text}"}

            res = resp.json()
            
            if not res.get("routes"):
                 log.warning("⚠️ Rota bulunamadı (Google search gerekebilir)")
                 return {"error": "Rota bulunamadı."}

            section = res["routes"][0]["sections"][0]
            
            summary = {
                "distance": f"{section['summary']['length'] / 1000:.2f} km",
                "duration": f"{section['summary']['duration'] / 60:.0f} dk",
                "polyline": section["polyline"],
                "summary": f"Tahmini {section['summary']['duration'] // 60} dakika"
            }
            
            log.success(f"✅ [HERE SUCCESS] Rota Hazır: {summary['distance']}")
            return summary

        except Exception as e:
            log.error(f"☠️ [HERE EXCEPTION] Patladı: {str(e)}")
            return {"error": f"Sunucu Hatası: {str(e)}"}