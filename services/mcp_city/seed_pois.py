import asyncio
import os
import sys

# Backend root dizinini path'e ekle (importlar çalışsın diye)
# d:\geointel-backend\services\mcp_city
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), 'services', 'mcp_city')))

from tools.db import save_poi_with_embedding, init_pool, close_pool

POIS = [
    {
        "name": "Idea Kadıköy",
        "description": "Kadıköy sahilinde yer alan, sessiz çalışma alanları ve deniz manzarası sunan bir ortak çalışma alanı (coworking). Bisikletle ulaşım kolaydır.",
        "lat": 40.9885,
        "lon": 29.0232,
        "category": "Çalışma Alanı"
    },
    {
        "name": "Salt Galata",
        "description": "Tarihi bir binada yer alan, inanılmaz bir kütüphaneye sahip, sessiz ve sanatsal bir çalışma mekanı. Galata bölgesindedir.",
        "lat": 41.0238,
        "lon": 28.9732,
        "category": "Kütüphane"
    },
    {
        "name": "Atatürk Kent Ormanı",
        "description": "Hacıosman'da yer alan devasa bir yeşil alan. Doğa yürüyüşü ve temiz hava için ideal. Şehirden kaçış noktası.",
        "lat": 41.1385,
        "lon": 29.0285,
        "category": "Park"
    },
    {
        "name": "Kanyon AVM",
        "description": "Modern mimarisiyle dikkat çeken, açık hava konseptli alışveriş ve yaşam merkezi. Levent bölgesinde yer alır.",
        "lat": 41.0782,
        "lon": 29.0112,
        "category": "Ticari"
    },
    {
        "name": "Atatürk Kitaplığı",
        "description": "Taksim'de bulunan, 7/24 açık (bazı bölümler), muhteşem boğaz manzaralı ve çok sessiz bir kütüphane.",
        "lat": 41.0372,
        "lon": 28.9882,
        "category": "Kütüphane"
    }
]

async def seed():
    print("🌱 [SEED] POI Verileri Ekleniyor...")
    await init_pool()
    try:
        for poi in POIS:
            print(f"🔹 {poi['name']} kaydediliyor...")
            res = await save_poi_with_embedding(
                poi['name'], poi['description'], poi['lat'], poi['lon'], poi['category']
            )
            print(f"   {res}")
    finally:
        await close_pool()
    print("✅ [SEED] İşlem tamamlandı.")

if __name__ == "__main__":
    asyncio.run(seed())
