import asyncpg
from .config import settings

async def save_location_handler(name: str, lat: float, lon: float, category: str = "Genel", note: str = "") -> str:
    """Konumu PostGIS veritabanına kaydeder."""
    conn = await asyncpg.connect(settings.DATABASE_URL)
    try:
        # PostGIS Geometry oluşturma
        query = """
        INSERT INTO saved_places (name, category, note, geom) 
        VALUES ($1, $2, $3, ST_SetSRID(ST_MakePoint($5, $4), 4326))
        """
        await conn.execute(query, name, category, note, lat, lon)
        return f"💾 Kaydedildi: {name}"
    except Exception as e:
        return f"Veritabanı Hatası: {e}"
    finally:
        await conn.close()