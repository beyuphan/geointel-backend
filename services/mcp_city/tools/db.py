import asyncpg
import json
from .config import settings
from google.generativeai import configure, embed_content as generate_embeddings

configure(api_key=settings.GOOGLE_API_KEY)

async def _get_embedding(text: str) -> list[float]:
    """Gemini API kullanarak metin için embedding (vektör) oluşturur."""
    try:
        response = generate_embeddings(
            model="models/text-embedding-004", # Gemini'nin güncel embedding modeli
            text=text
        )
        return response['embedding']
    except Exception as e:
        print(f"Embedding API Hatası: {e}")
        return []

async def save_poi_with_embedding(name: str, description: str, lat: float, lon: float, category: str = "Tavsiye") -> str:
    """Konumu ve açıklamasının embedding'ini (vektörünü) PostGIS/pgvector'e kaydeder."""
    conn = await asyncpg.connect(settings.DATABASE_URL)
    
    # Metni vektörleştir
    embedding_vector = await _get_embedding(description)
    if not embedding_vector:
        return "Hata: Embedding oluşturulamadı."

    location_str = f"{lat},{lon}"
    # pgvector formatına çevir: '[0.1, 0.2, ...]'
    vector_str = f"[{','.join(map(str, embedding_vector))}]"
    
    try:
        query = """
        INSERT INTO poi_embeddings (name, description, category, location, geom, embedding) 
        VALUES ($1, $2, $3, $4, ST_SetSRID(ST_MakePoint($6, $5), 4326), $7::vector)
        """
        await conn.execute(query, name, description, category, location_str, lat, lon, vector_str)
        return f"💾 POI & Embedding Kaydedildi: {name}"
    except Exception as e:
        return f"Veritabanı Hatası: {e}"
    finally:
        await conn.close()

async def search_spatial_rag(query_text: str, lat: float, lon: float, radius_meters: float = 5000, limit: int = 5) -> list[dict]:
    """
    HİBRİT RAG ARAMA: Kullanıcının metin sorgusuna anlamsal (semantic) olarak en çok 
    benzeyen yerleri bulur ve bunları belirtilen koordinata olan uzaklıklarına göre (spatial) daraltır.
    """
    conn = await asyncpg.connect(settings.DATABASE_URL)
    
    # Kullanıcı sorgusunu vektörleştir
    query_vector = await _get_embedding(query_text)
    if not query_vector:
        return [{"error": "Kullanıcı sorgusu için embedding oluşturulamadı."}]
        
    vector_str = f"[{','.join(map(str, query_vector))}]"

    try:
        # Hibrit Sorgu
        # 1. ST_DWithin: Noktalar belirtilen yarıçap (metre) içinde mi? (SPARSE - Konumsal Filtre)
        # 2. <=>: pgvector HNSW kosinüs uzaklığı operatörü. En benzer olanlar en düşük değere sahiptir. (DENSE - Anlamsal Sıralama)
        sql = """
        SELECT 
            name, 
            description, 
            category, 
            location,
            (embedding <=> $1::vector) AS semantic_distance,
            ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography) AS distance_meters
        FROM poi_embeddings
        WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography, $4)
        ORDER BY semantic_distance ASC
        LIMIT $5;
        """
        
        records = await conn.fetch(sql, vector_str, lat, lon, radius_meters, limit)
        
        results = []
        for r in records:
            results.append({
                "name": r["name"],
                "description": r["description"],
                "category": r["category"],
                "location": r["location"],
                "distance_meters": round(r["distance_meters"], 1),
                "semantic_distance": round(r["semantic_distance"], 4) # 0'a ne kadar yakınsa o kadar iyi
            })
            
        return results
    except Exception as e:
        return [{"error": f"RAG Sorgu Hatası: {e}"}]
    finally:
        await conn.close()
        
async def save_location_handler(name: str, lat: float, lon: float, category: str = "Genel", note: str = "") -> str:
    """Eski kayıt metodu (Geriye dönük uyumluluk)."""
    conn = await asyncpg.connect(settings.DATABASE_URL)
    try:
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