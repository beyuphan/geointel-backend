from fastmcp import FastMCP
from db_helper import DBHelper
from worker import create_scheduler
from contextlib import asynccontextmanager 
from loguru import logger
import sys

# --- FALLBACK İÇİN SCRAPER HANDLERLARI (Yedek Kuvvetler) ---
from tools.fuel import get_fuel_prices_handler
from tools.pharmacy import get_pharmacies_handler
from tools.events import get_events_handler
from tools.sports import get_matches_handler

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
    scheduler.start() # <--- ARTIK LOOP İÇİNDEYİZ, GÜVENLE BAŞLATABİLİRİZ
    yield
    logger.info("🕰️ [SYSTEM] Scheduler Kapatılıyor...")
    scheduler.shutdown()

# --- MCP KURULUMU ---
mcp = FastMCP(name="Intel Agent", lifespan=lifespan)

# --- TOOL TANIMLARI (HİBRİT MOD) ---

@mcp.tool()
async def get_pharmacies(city: str, district: str = "") -> list:
    """Nöbetçi eczaneleri bulur. Önce veritabanına bakar, yoksa canlı çeker."""
    logger.info(f"💊 [REQ] Eczane: {city}/{district}")
    
    # 1. Önce Veritabanına Bak
    data = await DBHelper.read_pharmacies(city, district)
    
    if data:
        logger.success(f"   ✅ [CACHE] DB'den {len(data)} eczane döndü.")
        return data

    # 2. Veritabanında Yoksa Canlıya Git
    logger.warning(f"   ⚠️ [MISS] DB'de yok, sahaya çıkılıyor...")
    
    try:
        live_data = await get_pharmacies_handler(city, district)
        
        # Hata dönmediyse ve veri varsa hemen kaydet
        if live_data and "error" not in live_data[0]:
            await DBHelper.save_pharmacies(live_data, city)
            logger.info("   💾 [SAVE] Canlı veri DB'ye işlendi.")
            
        return live_data
    except Exception as e:
        logger.error(f"   🔥 [ERR] Canlı çekim hatası: {e}")
        return [{"bilgi": "Eczane verisi ne DB'de ne de canlı kaynakta bulunamadı."}]

@mcp.tool()
async def get_fuel_prices(city: str, district: str) -> list:
    """Akaryakıt fiyatlarını getirir. Önce veritabanına bakar, yoksa canlı çeker."""
    logger.info(f"⛽ [REQ] Yakıt: {city}/{district}")
    
    # 1. Önce Veritabanı
    data = await DBHelper.read_fuel_prices(city, district)
    
    if data:
        logger.success(f"   ✅ [CACHE] DB'den {len(data)} istasyon döndü.")
        return data
    
    # 2. Canlı Tarama (Fallback)
    logger.warning(f"   ⚠️ [MISS] DB'de yok, pompa fiyatlarına bakılıyor...")
    
    try:
        live_data = await get_fuel_prices_handler(city, district)
        
        if live_data and "error" not in live_data[0]:
            # Scraper'dan gelen veride 'city' eksik olabilir, tamamlayalım
            for item in live_data:
                item['city'] = city
                
            await DBHelper.save_fuel_prices(live_data)
            logger.info("   💾 [SAVE] Canlı veri DB'ye işlendi.")
            
        return live_data
    except Exception as e:
        logger.error(f"   🔥 [ERR] Canlı çekim hatası: {e}")
        return [{"bilgi": "Yakıt fiyatlarına şu an ulaşılamıyor."}]

@mcp.tool()
async def get_city_events(city: str) -> list:
    """Şehir etkinlikleri. Hibrit çalışır."""
    logger.info(f"🎭 [REQ] Etkinlik: {city}")
    
    data = await DBHelper.read_events(city)
    
    if data:
        logger.success(f"   ✅ [CACHE] DB'den {len(data)} etkinlik döndü.")
        return data
        
    logger.warning(f"   ⚠️ [MISS] DB'de yok, bilet siteleri taranıyor...")
    
    try:
        live_data = await get_events_handler(city)
        if live_data and "error" not in live_data[0]:
            await DBHelper.save_events(live_data, city)
        return live_data
    except Exception as e:
        return [{"bilgi": "Etkinlik bulunamadı."}]

@mcp.tool()
async def get_sports_events() -> list:
    """Maç fikstürü. Hibrit çalışır."""
    logger.info(f"⚽ [REQ] Maç Fikstürü")
    
    data = await DBHelper.read_matches()
    
    if data:
        logger.success(f"   ✅ [CACHE] DB'den {len(data)} maç döndü.")
        return data
        
    logger.warning(f"   ⚠️ [MISS] DB boş, TFF taranıyor...")
    
    try:
        live_data = await get_matches_handler()
        if live_data and "error" not in live_data[0]:
            await DBHelper.save_matches(live_data)
        return live_data
    except Exception as e:
        return [{"bilgi": "Maç verisi bulunamadı."}]

if __name__ == "__main__":
    logger.info("🚀 [SYSTEM] Intel Ajanı (Hibrit Mod) Başlatılıyor...")
        
    # 2. Sunucuyu aç
    mcp.run(transport="sse", host="0.0.0.0", port=8001)