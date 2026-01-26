import os
import httpx
import asyncpg
from fastapi import FastAPI
from pydantic import BaseModel
from config import settings

app = FastAPI(title=settings.APP_NAME)
mcp_api = app # Docker uyumluluğu

# Modeller
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

# --- ARAÇLAR (SADECE API, MANTIK YOK) ---

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
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": data.lat, "lon": data.lon, "appid": settings.OPENWEATHER_API_KEY , "units": "metric", "lang": "tr"}
        )
        # Direkt API cevabını dönüyoruz, yorum yok.
        return resp.json()

@app.post("/search_places_google")
async def search_places(data: LocationQuery):
    params = {"query": data.query, "key": settings.GOOGLE_MAPS_API_KEY, "language": "tr"}
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://maps.googleapis.com/maps/api/place/textsearch/json", params=params)
        return resp.json()

# services/mcp_city/server.py dosyasının en altındaki fonksiyon:
@app.post("/get_route_data")
async def get_route(data: RouteQuery):
    print(f"🚗 [HERE ROUTING] İstek: {data.origin} -> {data.destination}", flush=True)

    if not settings.HERE_API_KEY:
        print("❌ [HERE ERROR] API Key Yok!", flush=True)
        return {"error": "HERE API Key eksik"}
    
    # HERE API 'lat,lon' formatını sever. (Örn: 52.5308,13.3847)
    # Eğer LLM bize metin yolladıysa (Rize Kalesi gibi), HERE hata verir.
    # O yüzden LLM'in kesinlikle koordinat yollaması lazım.
    
    async with httpx.AsyncClient() as client:
        url = "https://router.hereapi.com/v8/routes"
        
        params = {
            "transportMode": "car",
            "origin": data.origin.replace(" ", ""),       # Boşlukları temizle
            "destination": data.destination.replace(" ", ""),
            "return": "summary,polyline",
            "apiKey": settings.HERE_API_KEY
        }
        
        try:
            print(f"📡 [HERE REQUEST] Soruluyor: {url}", flush=True)
            resp = await client.get(url, params=params)
            
            if resp.status_code != 200:
                print(f"❌ [HERE ERROR] Hata Döndü: {resp.text}", flush=True)
                return {"error": f"HERE API Hatası: {resp.status_code} - {resp.text}"}

            res = resp.json()
            
            # HERE Cevap Formatı Google'dan farklıdır:
            if not res.get("routes"):
                 return {"error": "Rota bulunamadı."}

            section = res["routes"][0]["sections"][0]
            
            summary = {
                "distance": f"{section['summary']['length'] / 1000:.2f} km", # Metre gelir, km yapalım
                "duration": f"{section['summary']['duration'] / 60:.0f} dk", # Saniye gelir, dk yapalım
                "polyline": section["polyline"], # İşte o meşhur şifreli string
                "summary": f"Tahmini {section['summary']['duration'] // 60} dakika"
            }
            
            print(f"✅ [HERE SUCCESS] Rota Hazır: {summary['distance']}", flush=True)
            return summary

        except Exception as e:
            print(f"☠️ [HERE EXCEPTION] Patladı: {str(e)}", flush=True)
            return {"error": f"Sunucu Hatası: {str(e)}"}