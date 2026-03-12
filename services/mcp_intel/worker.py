import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from db_helper import DBHelper

# --- SCRAPER HANDLERLARI ---
from tools.fuel import get_fuel_prices_handler
from tools.pharmacy import get_pharmacies_handler
from tools.events import get_events_handler
from tools.sports import get_matches_handler

# --- HEDEF LİSTELERİ ---
# Akaryakıt için kritik ilçeler (Senin rotan ve majör yerler)
TARGET_CITIES_FUEL = [
    ("samsun", "atakum"), ("samsun", "ilkadim"), ("samsun", "havza"),
    ("rize", "merkez"), ("rize", "cayeli"), ("rize", "ardeşen"),
    ("trabzon", "ortahisar"), ("trabzon", "akcaabat"), ("trabzon", "of"),
    ("ankara", "cankaya"), ("ankara", "mamak"),
    ("istanbul", "kadikoy"), ("istanbul", "besiktas"), ("istanbul", "sisli"),
    ("izmir", "konak")
]

# Eczane ve Etkinlik için şehir listesi
TARGET_CITIES_GENERIC = ["samsun", "rize", "trabzon", "ankara", "istanbul", "izmir"]

# --- GÖREVLER (JOBS) ---

async def job_update_fuel():
    logger.info("⛽ [WORKER] Gece Yakıt Operasyonu Başladı...")
    count = 0
    for city, district in TARGET_CITIES_FUEL:
        try:
            # Scraper'ı çalıştır
            data = await get_fuel_prices_handler(city, district)
            
            # Gelen veride city eksik olabilir, biz ekleyelim
            if data and "error" not in data[0]:
                for item in data:
                    item['city'] = city
                
                # DB'ye kaydet
                await DBHelper.save_fuel_prices(data)
                count += 1
            
            # Anti-Ban: Seri istek atmamak için azıcık bekle
            await asyncio.sleep(3) 
            
        except Exception as e:
            logger.error(f"❌ [WORKER HATA] Yakıt ({city}/{district}): {e}")
            
    logger.success(f"⛽ [WORKER] Yakıt Operasyonu Bitti. {count} bölge güncellendi.")

async def job_update_pharmacy():
    logger.info("💊 [WORKER] Eczane Nöbet Değişimi Başladı...")
    for city in TARGET_CITIES_GENERIC:
        try:
            data = await get_pharmacies_handler(city)
            if data and "error" not in data[0]:
                await DBHelper.save_pharmacies(data, city)
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"❌ [WORKER HATA] Eczane ({city}): {e}")
    logger.success("💊 [WORKER] Eczane Listeleri Güncellendi.")

async def job_update_sports():
    logger.info("⚽ [WORKER] Fikstür ve Trafik Analizi Başladı...")
    try:
        data = await get_matches_handler()
        if data and "error" not in data[0]:
            await DBHelper.save_matches(data)
    except Exception as e:
        logger.error(f"❌ [WORKER HATA] Spor: {e}")
    logger.success("⚽ [WORKER] Maç Verileri Güncellendi.")

async def job_update_events():
    logger.info("🎭 [WORKER] Şehir Etkinlikleri Taranıyor...")
    for city in TARGET_CITIES_GENERIC:
        try:
            data = await get_events_handler(city)
            if data and "error" not in data[0]:
                await DBHelper.save_events(data, city)
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"❌ [WORKER HATA] Etkinlik ({city}): {e}")
    logger.success("🎭 [WORKER] Etkinlik Veritabanı Güncellendi.")

# --- ZAMANLAYICI AYARLARI ---

def create_scheduler():
    scheduler = AsyncIOScheduler(timezone="Europe/Istanbul")

    # 1. AKARYAKIT: Her gece 03:30 (Siteler güncellenmiş olur)
    scheduler.add_job(job_update_fuel, 'cron', hour=3, minute=30)
    
    # 2. ECZANE: Her sabah 08:15 (Nöbet listesi kesinleşir)
    scheduler.add_job(job_update_pharmacy, 'cron', hour=8, minute=15)
    
    # 3. SPOR: Cuma ve Pazartesi sabah 09:00 (Hafta sonu öncesi ve sonrası kontrol)
    scheduler.add_job(job_update_sports, 'cron', day_of_week='mon,fri', hour=9, minute=0)
    
    # 4. ETKİNLİK: 3 günde bir gece 04:00'te
    scheduler.add_job(job_update_events, 'interval', days=3, start_date='2026-01-01 04:00:00')

    # --- TEST İÇİN (İstersen açarsın, container kalkınca bir tur çalışır) ---
    # scheduler.add_job(job_update_fuel, 'date')      # Hemen çalıştır
    # scheduler.add_job(job_update_sports, 'date')    # Hemen çalıştır
    
    return scheduler
    logger.info("🕰️ [SYSTEM] Intel Scheduler Kuruldu. İşçiler vardiyaya hazır.")