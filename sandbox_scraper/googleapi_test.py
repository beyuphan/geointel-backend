import httpx
import asyncio
import os

# BURAYA .env DOSYASINDAKİ GOOGLE KEY'İNİ YAPIŞTIR
GOOGLE_API_KEY ="xxx" 

async def test_google():
    print(f"🕵️ Google API Test Ediliyor... Key: {GOOGLE_API_KEY[:5]}***")
    
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    
    # Rize Çaykur Didi Stadyumu civarı
    params = {
        "query": "restoran",
        "location": "41.0256,40.5165",
        "radius": "5000",
        "key": GOOGLE_API_KEY,
        "language": "tr"
    }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params)
            data = resp.json()
            
            print(f"📡 HTTP Durumu: {resp.status_code}")
            
            if "error_message" in data:
                print(f"❌ GOOGLE HATASI: {data['error_message']}")
                print(f"⚠️ Durum Kodu: {data.get('status')}")
                return

            if "results" in data:
                count = len(data["results"])
                print(f"✅ BAŞARILI! {count} mekan bulundu.")
                if count > 0:
                    print(f"🏠 İlk Mekan: {data['results'][0]['name']}")
                    print(f"📍 Adres: {data['results'][0]['formatted_address']}")
            else:
                print("⚠️ Yanıt döndü ama 'results' yok. Ham veri:")
                print(data)

        except Exception as e:
            print(f"🔥 Bağlantı Hatası: {e}")

if __name__ == "__main__":
    asyncio.run(test_google())