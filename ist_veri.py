import requests
import os
import time

# HEDEF: Altın Boynuz + Boğaz Hattı + İki Köprü + Kadıköy/Üsküdar
# (MinLat, MinLon, MaxLat, MaxLon)
# 40.98 (Kadıköy) - 41.11 (Maslak/FSM)
# 28.92 (Zeytinburnu) - 29.07 (Altunizade)
BBOX = "40.98,28.92,41.11,29.07"
OUTPUT_FILE = "data/istanbul_pilot.osm"

# Yedekli Sunucu Listesi (Failover)
SERVERS = [
    "https://overpass.kumi.systems/api/interpreter", 
    "https://overpass-api.de/api/interpreter",       
    "https://lz4.overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
]

def download_expanded():
    print("🌍 İSTANBUL GENİŞLETİLMİŞ BÖLGE İNDİRİLİYOR...")
    print("👉 Kapsam: Fatih - Beşiktaş - Şişli - Üsküdar - Kadıköy - Köprüler")
    print("⏳ Veri büyük (50MB+), işlem 2-3 dakika sürebilir. Bekle...")
    
    # Timeout: 500 saniye (8 dakika), MaxSize: 1GB
    query = f"""
    [out:xml][timeout:500][maxsize:1073741824];
    (
      way["highway"]({BBOX});
    );
    (._;>;);
    out meta;
    """
    
    os.makedirs("data", exist_ok=True)
    
    for url in SERVERS:
        print(f"\n🔄 Sunucu Deneniyor: {url} ...")
        try:
            response = requests.post(url, data=query, timeout=500)
            
            if response.status_code == 200:
                content_len = len(response.content)
                
                # Eğer 10KB'dan küçükse hata vardır
                if content_len < 10000:
                    print("   ⚠️ Veri çok küçük/hatalı geldi.")
                    print(f"   Cevap: {response.text[:100]}...")
                    continue 

                with open(OUTPUT_FILE, "wb") as f:
                    f.write(response.content)
                
                size_mb = os.path.getsize(OUTPUT_FILE) / (1024 * 1024)
                print(f"   ✅ BAŞARILI! İndirme Tamamlandı.")
                print(f"   📦 Dosya: {OUTPUT_FILE} ({size_mb:.2f} MB)")
                
                if size_mb > 15:
                    print("   🚀 EFSANE! Dolu dolu bir harita indi.")
                    return
                else:
                    print("   ⚠️ Dosya boyutu beklenen az ama devam edelim.")
                    return
                
            elif response.status_code == 429:
                print("   ⏳ Rate Limit (Çok istek). Bekleyip diğerine geçiliyor...")
            elif response.status_code == 504:
                print("   🐢 Timeout. Sunucu yetemedi, diğerine geçiliyor...")
            else:
                print(f"   ❌ Hata Kodu: {response.status_code}")
                
        except Exception as e:
            print(f"   🔥 Bağlantı Hatası: {e}")
            
        time.sleep(2) 

    print("\n💀 BAŞARISIZ: Hiçbir sunucu bu kadar büyük veriyi veremedi.")

if __name__ == "__main__":
    download_expanded()