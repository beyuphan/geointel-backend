"""
db.py — v3.0 (Connection Pool + Lifecycle Management)

Kritik Değişiklikler:
- asyncpg.connect() → asyncpg.Pool (her sorguda yeni bağlantı yerine pool)
- Pool startup/shutdown lifecycle'a bağlandı
- Saved locations sorgusu eklendi (orchestrator'dan döngüsel import kesildi)
"""
import asyncpg
import json
from .config import settings
from loguru import logger as log
from google.generativeai import configure, embed_content as generate_embeddings

configure(api_key=settings.GOOGLE_API_KEY)

# Global connection pool (server startup'ta başlatılır)
_pool: asyncpg.Pool | None = None


async def init_pool():
    """Server başlarken bir kez çağrılır — pool oluşturur."""
    global _pool
    if _pool is not None:
        return
    try:
        _pool = await asyncpg.create_pool(
            settings.DATABASE_URL,
            min_size=3,
            max_size=15,
            command_timeout=30,
            statement_cache_size=0,  # pgbouncer uyumu için
        )
        log.success("✅ [DB Pool] asyncpg connection pool başlatıldı (min=3, max=15)")
    except Exception as e:
        log.error(f"❌ [DB Pool] Pool başlatılamadı: {e}")
        _pool = None


async def close_pool():
    """Server kapanırken bir kez çağrılır — pool'u temizler."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        log.info("🔌 [DB Pool] Bağlantı havuzu kapatıldı.")


def get_pool() -> asyncpg.Pool:
    """Pool'u döner; başlatılmamışsa hata fırlatır."""
    if _pool is None:
        raise RuntimeError("DB Pool henüz başlatılmadı. init_pool() çağrılmamış.")
    return _pool


# ---------------------------------------------------------------------------
# EMBEDDING
# ---------------------------------------------------------------------------

async def _get_embedding(text: str) -> list[float]:
    """Gemini API kullanarak metin için embedding oluşturur."""
    try:
        response = generate_embeddings(
            model="models/text-embedding-004",
            content=text,
        )
        return response["embedding"]
    except Exception as e:
        log.error(f"Embedding API Hatası: {e}")
        return []


# ---------------------------------------------------------------------------
# POI KAYIT & SPATIAL RAG
# ---------------------------------------------------------------------------

async def save_poi_with_embedding(
    name: str, description: str, lat: float, lon: float, category: str = "Tavsiye"
) -> str:
    """Konumu ve açıklamasının embedding'ini PostGIS/pgvector'e kaydeder."""
    embedding_vector = await _get_embedding(description)
    if not embedding_vector:
        return "Hata: Embedding oluşturulamadı."

    location_str = f"{lat},{lon}"
    vector_str = f"[{','.join(map(str, embedding_vector))}]"

    try:
        async with get_pool().acquire() as conn:
            query = """
            INSERT INTO poi_embeddings (name, description, category, location, geom, embedding)
            VALUES ($1, $2, $3, $4, ST_SetSRID(ST_MakePoint($6, $5), 4326), $7::vector)
            ON CONFLICT DO NOTHING
            """
            await conn.execute(query, name, description, category, location_str, lat, lon, vector_str)
        return f"✅ POI & Embedding Kaydedildi: {name}"
    except Exception as e:
        log.error(f"[DB] POI kayıt hatası: {e}")
        return f"Veritabanı Hatası: {e}"


async def search_spatial_rag(
    query_text: str,
    lat: float,
    lon: float,
    radius_meters: float = 5000,
    limit: int = 5,
) -> list[dict]:
    """
    Hybrid RAG: pgvector cosine similarity (dense) + ST_DWithin spatial filter (sparse).
    """
    query_vector = await _get_embedding(query_text)
    if not query_vector:
        return [{"error": "Kullanıcı sorgusu için embedding oluşturulamadı."}]

    vector_str = f"[{','.join(map(str, query_vector))}]"

    try:
        async with get_pool().acquire() as conn:
            sql = """
            SELECT
                name,
                description,
                category,
                location,
                (embedding <=> $1::vector) AS semantic_distance,
                ST_Distance(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography
                ) AS distance_meters
            FROM poi_embeddings
            WHERE ST_DWithin(
                geom::geography,
                ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography,
                $4
            )
            ORDER BY semantic_distance ASC
            LIMIT $5;
            """
            records = await conn.fetch(sql, vector_str, lat, lon, radius_meters, limit)

        return [
            {
                "name": r["name"],
                "description": r["description"],
                "category": r["category"],
                "location": r["location"],
                "distance_meters": round(r["distance_meters"], 1),
                "semantic_score": round(1 - r["semantic_distance"], 4),  # 1=mükemmel eşleşme
            }
            for r in records
        ]
    except Exception as e:
        log.error(f"[DB] RAG sorgu hatası: {e}")
        return [{"error": f"RAG Sorgu Hatası: {e}"}]


async def save_location_handler(
    name: str, lat: float, lon: float, category: str = "Genel", note: str = ""
) -> str:
    """saved_places tablosuna konum kayıt (geriye dönük uyumluluk)."""
    try:
        async with get_pool().acquire() as conn:
            query = """
            INSERT INTO saved_places (name, category, note, geom)
            VALUES ($1, $2, $3, ST_SetSRID(ST_MakePoint($5, $4), 4326))
            """
            await conn.execute(query, name, category, note, lat, lon)
        return f"✅ Kaydedildi: {name}"
    except Exception as e:
        log.error(f"[DB] Konum kayıt hatası: {e}")
        return f"Veritabanı Hatası: {e}"


# ---------------------------------------------------------------------------
# SAVED LOCATIONS (orchestrator.ProfileManager'dan taşındı → döngüsel import kırıldı)
# ---------------------------------------------------------------------------

async def get_saved_locations(username: str = "test_pilot") -> dict[str, str]:
    """
    Kullanıcının kayıtlı konumlarını (Ev, İş vb.) döner.
    Önceden orchestrator'dan import ediliyordu — şimdi doğrudan DB'den çekiliyor.
    """
    try:
        async with get_pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT sl.name, sl.coordinates
                FROM saved_locations sl
                JOIN users u ON u.id = sl.user_id
                WHERE u.username = $1 AND sl.coordinates IS NOT NULL
                """,
                username,
            )
        return {r["name"].lower().strip(): r["coordinates"] for r in rows}
    except Exception as e:
        log.warning(f"⚠️ [DB] Kayıtlı konumlar alınamadı: {e}")
        return {}