import pystac_client
import planetary_computer
import stackstac
import xarray as xr
import numpy as np
from datetime import datetime, timedelta
from loguru import logger as log

# Microsoft Planetary Computer STAC API Endpoint'i
STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

class SatelliteClient:
    def __init__(self):
        # Raporunda belirttiğin gibi STAC API üzerinden keşif yapıyoruz [cite: 213]
        self.catalog = pystac_client.Client.open(
            STAC_API_URL, 
            modifier=planetary_computer.sign_inplace
        )

    def search_sentinel2(self, bbox, days_back=30, max_cloud_cover=20):
        """
        Belirli bir bölge için Sentinel-2 görüntülerini arar.
        """
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days_back)
        date_range = f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"

        log.info(f"📡 Uydu Taraması: {date_range}, Bulut Limiti: %{max_cloud_cover}")

        # Sentinel-2 Level 2A: Atmosferik düzeltmesi yapılmış veri [cite: 273]
        search = self.catalog.search(
            collections=["sentinel-2-l2a"],
            bbox=bbox,
            datetime=date_range,
            query={"eo:cloud_cover": {"lt": max_cloud_cover}},
            sortby=[{"field": "properties.datetime", "direction": "desc"}]
        )

        return search.item_collection()

    def calculate_ndvi(self, item, bbox):
        """
        COG Streaming (HTTP Range Requests) kullanarak NDVI hesaplar [cite: 316-317].
        """
        try:
            # Sentinel-2 bantları: B04 (Red), B08 (NIR) [cite: 590]
            # stackstac ile görüntüyü indirmeden hafızaya (virtual) yüklüyoruz
            stack = stackstac.stack(
                item, 
                assets=["B04", "B08"], 
                bounds=bbox,
                epsg=4326 # BBox koordinat sistemimiz (WGS84) 
            )

            # Veriyi float tipine çevirip (hesaplama için) seçiyoruz
            # sel() ile bantları ayırıyoruz
            red = stack.sel(band="B04").astype("float")
            nir = stack.sel(band="B08").astype("float")

            # 🔥 Raporundaki Denklem 3: NDVI = (NIR - Red) / (NIR + Red) 
            ndvi_map = (nir - red) / (nir + red)

            # Sadece o bölgedeki ortalama değeri alıp döndürüyoruz
            # compute() satırı, buluttan sadece o piksellerin çekildiği andır
            avg_ndvi = float(ndvi_map.mean().compute())
            
            return round(avg_ndvi, 4)

        except Exception as e:
            log.error(f"❌ NDVI Hesaplanırken hata oluştu: {e}")
            return None

# --- TEST BLOĞU ---
if __name__ == "__main__":
    client = SatelliteClient()
    
    # Örnek: Rize/Kalkandere civarı [min_lon, min_lat, max_lon, max_lat]
    rize_bbox = [40.40, 40.90, 40.45, 40.95] 
    
    # 1. Adım: Arama (Discovery)
    items = client.search_sentinel2(rize_bbox, days_back=45, max_cloud_cover=100)
    
    if len(items) > 0:
        target_item = items[0]
        log.success(f"✅ Görüntü bulundu: {target_item.datetime}")
        
        # 2. Adım: Analiz (Processing)
        log.info("🧪 NDVI Analizi Başlatılıyor (Streaming)...")
        score = client.calculate_ndvi(target_item, rize_bbox)
        
        if score is not None:
            log.success(f"🌿 Bölgesel NDVI Skoru: {score}")
            # Yorumlama (Raporundaki mantığa göre)
            if score > 0.6: log.info("Yorum: Çok yoğun ve sağlıklı bitki örtüsü.")
            elif score > 0.2: log.info("Yorum: Seyrek bitki örtüsü / Tarım alanı.")
            else: log.info("Yorum: Su yüzeyi, yerleşim yeri veya kar.")
    else:
        log.warning("⚠️ Belirtilen kriterlerde görüntü bulunamadı.")