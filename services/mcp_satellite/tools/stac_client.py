from pystac_client import Client
import planetary_computer
from datetime import datetime, timedelta

# Microsoft Planetary Computer STAC API Endpoint'i
STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

class SatelliteClient:
    def __init__(self):
        self.catalog = Client.open(STAC_API_URL, modifier=planetary_computer.sign_inplace)

    def search_sentinel2(self, bbox, days_back=5, max_cloud_cover=20):
        """
        Belirli bir bölge (bbox) için Sentinel-2 görüntülerini arar.
        
        Args:
            bbox (list): [min_lon, min_lat, max_lon, max_lat]
            days_back (int): Kaç gün geriye bakılacağı
            max_cloud_cover (int): Maksimum bulutluluk oranı (%)
            
        Returns:
            list: Bulunan sahnelerin (items) listesi
        """
        # Tarih aralığını belirle
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days_back)
        date_range = f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"

        print(f"📡 Uydu Taraması Başlatılıyor: {date_range}, Bulut Limiti: %{max_cloud_cover}")

        search = self.catalog.search(
            collections=["sentinel-2-l2a"], # Sentinel-2 Level 2A (Atmosferik düzeltme yapılmış)
            bbox=bbox,
            datetime=date_range,
            query={"eo:cloud_cover": {"lt": max_cloud_cover}},
            sort_by=[{"field": "properties.datetime", "direction": "desc"}] # En yeniden eskiye
        )

        items = search.item_collection()
        return items

# Test için (sadece bu dosya çalıştırıldığında)
if __name__ == "__main__":
    # Örnek: Rize/Kalkandere civarı
    client = SatelliteClient()
    rize_bbox = [40.40, 40.90, 40.45, 40.95] 
    results = client.search_sentinel2(rize_bbox)
    print(f"Bulunan görüntü sayısı: {len(results)}")
    if len(results) > 0:
        print(f"En son görüntü tarihi: {results[0].datetime}")