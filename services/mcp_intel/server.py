import json
import sys
from fastmcp import FastMCP
from contextlib import asynccontextmanager 
from loguru import logger

# Helper & Tools
from db_helper import DBHelper
from worker import create_scheduler

# Handler'lar (Yedek Kuvvetler)
from tools.fuel import get_fuel_prices_handler
from tools.pharmacy import get_pharmacies_handler
from tools.events import get_events_handler
from tools.sports import get_matches_handler

from safe_tools import safe_tool

# Yeni Modellerimiz
from tools.models import IntelResponse, FuelPrice, Pharmacy, Event, Match

# --- LOG AYARLARI ---
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
    level="INFO",
    colorize=True
)

# --- LIFESPAN (YAŞAM DÖNGÜSÜ) ---
@asynccontextmanager
async def lifespan(request: object):
    logger.info("🕰️ [SYSTEM] Scheduler Başlatılıyor...")
    scheduler = create_scheduler()
    scheduler.start()
    yield
    logger.info("🕰️ [SYSTEM] Scheduler Kapatılıyor...")
    scheduler.shutdown()

# --- MCP KURULUMU ---
mcp = FastMCP(name="Intel Agent", lifespan=lifespan)

# --- STANDARTLAŞTIRICI FONKSİYON (DRY Prensibi) ---
def create_response(data_list, model_class) -> str:
    """Verilen listeyi Pydantic modeline döküp JSON string yapar."""
    if not data_list:
        return json.dumps({"status": "error", "message": "Veri bulunamadı.", "data": []}, ensure_ascii=False)
    
    # Eğer handler direkt hata döndüyse (dict içinde 'error' veya 'bilgi' varsa)
    if isinstance(data_list, list) and len(data_list) > 0 and isinstance(data_list[0], dict):
        if "error" in data_list[0]:
             return json.dumps({"status": "error", "message": data_list[0]["error"], "data": []}, ensure_ascii=False)
        if "bilgi" in data_list[0]:
             return json.dumps({"status": "success", "message": data_list[0]["bilgi"], "data": []}, ensure_ascii=False)

    try:
        validated_data = [model_class(**item).model_dump() for item in data_list]
        return json.dumps({"status": "success", "data": validated_data}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Validasyon Hatası: {e}")
        # Hata olsa bile ham veriyi 'data' olarak dönelim ki sistem durmasın
        return json.dumps({"status": "partial_error", "message": str(e), "data": data_list}, ensure_ascii=False)


# --- TOOL TANIMLARI (HİBRİT MOD + JSON ÇIKIŞ) ---

@mcp.tool()
@safe_tool(fallback_message="Pharmacy data unavailable.")
async def get_pharmacies(city: str, district: str = "") -> str:
    """
    Finds on-duty pharmacies in the specified city/district.

    Args:
        city: City name (e.g. 'Samsun', 'Istanbul').
        district: District name (e.g. 'Atakum', 'Kadikoy').
    """
    logger.info(f"💊 [REQ] Eczane: {city}/{district}")
    
    # 1. DB Kontrol
    data = await DBHelper.read_pharmacies(city, district)
    if data:
        # DB'den gelen veri 'isim', 'adres' formatında, modelimiz 'name', 'address' istiyor.
        # DB Helper'daki sorguları modele uygun hale getirmek en temizi ama
        # şimdilik manuel mapping yapalım:
        mapped_data = []
        for d in data:
            mapped_data.append({
                "name": d.get("isim"),
                "address": d.get("adres"),
                "phone": d.get("tel"),
                "district": d.get("ilce"),
                "coordinates": d.get("koordinat")
            })
        return create_response(mapped_data, Pharmacy)

    # 2. Canlı (Live)
    live_data = await get_pharmacies_handler(city, district)
    
    # Canlı veri geldiyse modele uyarlayalım (Scraper çıktısı modele ne kadar uyuyor?)
    # Scraper: isim, adres, tel, ilce -> Model: name, address, phone, district
    # Basit bir key değişikliği gerekebilir
    if live_data and "error" not in live_data[0]:
        fixed_live = []
        for d in live_data:
            fixed_live.append({
                "name": d.get("isim"),
                "address": d.get("adres"),
                "phone": d.get("tel"),
                "district": d.get("ilce"),
                "coordinates": d.get("koordinat")
            })
        
        # Arka planda kaydet (non-blocking olsun diye create_task kullanılabilir ama şimdilik await kalsın)
        await DBHelper.save_pharmacies(live_data, city)
        return create_response(fixed_live, Pharmacy)
    
    return create_response(live_data, Pharmacy) # Hata mesajını basar

@mcp.tool()
@safe_tool(fallback_message="Fuel price data unavailable.")
async def get_fuel_prices(city: str, district: str) -> str:
    """
    Returns current fuel prices (gasoline, diesel, LPG) for a city/district.
    Use to find cheapest nearby fuel stations.

    Args:
        city: City name.
        district: District name.
    """
    logger.info(f"⛽ [REQ] Yakıt: {city}/{district}")
    
    # 1. DB
    data = await DBHelper.read_fuel_prices(city, district)
    if data:
        # DB verisini modele uydur
        mapped = []
        for d in data:
            mapped.append({
                "company": d.get("firma"),
                "gasoline": float(d.get("benzin", 0)),
                "diesel": float(d.get("motorin", 0)),
                "lpg": float(d.get("lpg", 0)),
                "district": district,
                "city": city
            })
        return create_response(mapped, FuelPrice)
    
    # 2. Canlı
    live_data = await get_fuel_prices_handler(city, district)
    if live_data and "error" not in live_data[0]:
        fixed_live = []
        for d in live_data:
            fixed_live.append({
                "company": d.get("firma"),
                "gasoline": d.get("benzin"),
                "diesel": d.get("motorin"),
                "lpg": d.get("lpg"),
                "district": d.get("ilce", district),
                "city": city
            })
        await DBHelper.save_fuel_prices(live_data, city)
        return create_response(fixed_live, FuelPrice)

    return create_response(live_data, FuelPrice)

@mcp.tool()
async def get_city_events(city: str) -> str:
    """
    Lists upcoming concerts, theater and cultural events in a city.

    Args:
        city: City name to search events for.
    """
    logger.info(f"🎭 [REQ] Etkinlik: {city}")
    
    # DB
    data = await DBHelper.read_events(city)
    if data:
        return create_response(data, Event)
        
    # Canlı
    live_data = await get_events_handler(city)
    if live_data and "error" not in live_data[0]:
        await DBHelper.save_events(live_data, city)
        
    return create_response(live_data, Event)

@mcp.tool()
async def get_sports_events() -> str:
    """
    Returns upcoming football matches with stadium and time info.
    Use to anticipate traffic congestion around stadiums.
    """
    logger.info(f"⚽ [REQ] Maç Fikstürü")
    
    # DB
    data = await DBHelper.read_matches()
    if data:
        # DB -> Model Mapping
        mapped = []
        for d in data:
            mapped.append({
                "match": d.get("mac"),
                "time": d.get("zaman"),
                "stadium": d.get("stadyum"),
                "city": d.get("sehir"),
                "warning": d.get("uyari")
            })
        return create_response(mapped, Match)
        
    # Canlı
    live_data = await get_matches_handler()
    if live_data and "error" not in live_data[0] and "bilgi" not in live_data[0]:
        await DBHelper.save_matches(live_data)
        
    return create_response(live_data, Match)

if __name__ == "__main__":
    logger.info("🚀 [SYSTEM] Intel Ajanı (Hibrit + JSON Mod) Başlatılıyor...")
    mcp.run(transport="sse", host="0.0.0.0", port=8001)