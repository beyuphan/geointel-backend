import httpx
import asyncpg
from fastmcp import FastMCP
from config import settings
from logger import log

mcp = FastMCP(name="City Agent")

async def get_db_connection():
    return await asyncpg.connect(settings.DATABASE_URL)

# --- 1. OSM: AKILLI ALTYAPI ARAMA (MAPPER EKLENDİ) ---
@mcp.tool()
async def search_infrastructure_osm(
    lat: float, 
    lon: float, 
    category: str = "airport" # airport, park, square, mosque, hospital
) -> list:
    """
    Kamusal alanları bulur.
    Kategoriler: 'airport', 'park', 'square', 'mosque', 'hospital', 'center'
    """
    radius = 50000 if category == "airport" else 2000 # Havalimanı için çapı devasa yap
    
    log.info(f"🌍 [OSM] Altyapı Taranıyor: {category} ({lat}, {lon}) - Çap: {radius}m")
    
    # --- TAG MAPPER (SİHİRLİ KISIM) ---
    # LLM'in dilini OSM diline çeviriyoruz
    tag_map = {
        "airport": ['"aeroway"="aerodrome"'],
        "park": ['"leisure"="park"', '"leisure"="garden"'],
        "square": ['"place"="square"', '"landuse"="plaza"'],
        "mosque": ['"amenity"="place_of_worship"["religion"="muslim"]'],
        "hospital": ['"amenity"="hospital"'],
        "center": ['"place"="city"', '"place"="town"']
    }
    
    # Varsayılan olarak amenity=category arar
    tags = tag_map.get(category, [f'"amenity"="{category}"'])
    
    # Sorguyu oluştur (nwr = Node, Way, Relation - Hepsine bak)
    filters = "".join([f'nwr[{t}](around:{radius},{lat},{lon});' for t in tags])
    
    query = f"""
    [out:json][timeout:25];
    (
      {filters}
    );
    out center 5;
    """
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post("https://overpass-api.de/api/interpreter", data=query)
            
            if resp.status_code != 200:
                log.error(f"OSM Hatası: {resp.status_code} - {resp.text}")
                return [{"error": f"OSM Hatası: {resp.status_code}"}]
            
            places = []
            for el in resp.json().get("elements", []):
                tags = el.get("tags", {})
                name = tags.get("name") or tags.get("name:tr") or tags.get("name:en")
                if not name: continue
                
                # Merkez noktasını bul
                plat = el.get("lat") or el.get("center", {}).get("lat")
                plon = el.get("lon") or el.get("center", {}).get("lon")
                
                places.append({
                    "isim": name,
                    "kategori": category,
                    "lat": plat,
                    "lon": plon
                })
            
            log.success(f"✅ [OSM] {len(places)} yer bulundu.")
            return places[:3] # En yakın 3 tanesi yeter
            
        except Exception as e:
            log.error(f"OSM Exception: {e}")
            return [{"error": str(e)}]

# --- 2. GOOGLE: TİCARİ MEKANLAR ---
@mcp.tool()
async def search_places_google(query: str, lat: float = None, lon: float = None) -> list:
    """Restoran, kafe vb. için Google."""
    log.info(f"🔍 [Google] Aranıyor: {query}")
    async with httpx.AsyncClient() as client:
        try:
            params = {"key": settings.GOOGLE_MAPS_API_KEY, "language": "tr", "query": query}
            if lat and lon:
                params["location"] = f"{lat},{lon}"
                params["radius"] = "2000"

            resp = await client.get("https://maps.googleapis.com/maps/api/place/textsearch/json", params=params)
            data = resp.json()
            
            results = []
            if data.get("results"):
                for item in data["results"][:3]:
                    loc = item["geometry"]["location"]
                    results.append({
                        "isim": item["name"],
                        "adres": item["formatted_address"],
                        "puan": item.get("rating", "Yok"),
                        "lat": loc["lat"],
                        "lon": loc["lng"]
                    })
                return results
            return [{"error": "Bulunamadı"}]
        except Exception as e:
            return [{"error": str(e)}]

# --- 3. DİĞERLERİ ---
@mcp.tool()
async def get_route_data(origin: str, destination: str) -> dict:
    log.info(f"🚗 Route: {origin} -> {destination}")
    async with httpx.AsyncClient() as client:
        try:
            # Koordinat temizliği
            o = origin.replace(" ", "")
            d = destination.replace(" ", "")
            
            resp = await client.get("https://router.hereapi.com/v8/routes", params={
                "transportMode": "car", 
                "origin": o, 
                "destination": d, 
                "return": "summary", 
                "apiKey": settings.HERE_API_KEY
            })
            
            if resp.status_code == 200 and resp.json().get("routes"):
                s = resp.json()["routes"][0]["sections"][0]["summary"]
                return {"mesafe_km": round(s["length"]/1000, 2), "sure_dk": round(s["duration"]/60, 0)}
            
            log.error(f"Rota Hatası: {resp.text}")
            return {"error": "Rota bulunamadı"}
        except Exception as e:
            return {"error": str(e)}

@mcp.tool()
async def get_weather(lat: float, lon: float) -> dict:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get("https://api.openweathermap.org/data/3.0/onecall", params={"lat": lat, "lon": lon, "appid": settings.OPENWEATHER_API_KEY, "units": "metric", "exclude": "alerts"})
            curr = resp.json().get("current", {})
            return {"sicaklik": curr.get("temp"), "durum": curr.get("weather", [{}])[0].get("description")}
        except: return {"error": "Hava durumu alınamadı"}

@mcp.tool()
async def save_location(name: str, lat: float, lon: float, category: str = "Genel", note: str = "") -> str:
    conn = await get_db_connection()
    try:
        await conn.execute("INSERT INTO saved_places (name, category, note, geom) VALUES ($1, $2, $3, ST_SetSRID(ST_MakePoint($5, $4), 4326))", name, category, note, lat, lon)
        return f"{name} kaydedildi."
    except Exception as e: return f"Hata: {e}"
    finally: await conn.close()

if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=8000)