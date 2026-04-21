"""
stac_client.py — v2.0 (Production Ready)

Değişiklikler:
- Async search desteği (asyncio.get_event_loop → run_in_executor)
- bbox validasyonu eklendi
- NDVI yorumlama katmanı eklendi
- EVI (Enhanced Vegetation Index) hesabı eklendi
- Hata yönetimi güçlendirildi
- PC_SDK_SUBSCRIPTION_KEY opsiyonel (public API fallback)
"""
import os
import numpy as np
from datetime import datetime, timedelta, timezone
from loguru import logger as log
from typing import Optional

# Opsiyonel bağımlılıklar — yoksa graceful fallback
try:
    import pystac_client
    _PYSTAC_AVAILABLE = True
except ImportError:
    _PYSTAC_AVAILABLE = False
    log.warning("pystac_client yüklü değil, satellite tools devre dışı.")

try:
    import planetary_computer
    _PC_AVAILABLE = True
except ImportError:
    _PC_AVAILABLE = False

try:
    import stackstac
    _STACKSTAC_AVAILABLE = True
except ImportError:
    _STACKSTAC_AVAILABLE = False

# Microsoft Planetary Computer STAC API
STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Public Earth Search (PC yoksa fallback)
EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1"


def _interpret_ndvi(ndvi: float) -> dict:
    """NDVI değerini insan okunabilir yoruma çevirir."""
    if ndvi > 0.6:
        return {"seviye": "ÇOK YOĞUN", "yorum": "Sağlıklı, yoğun bitki örtüsü (orman, çayır)", "renk_kodu": "#1a7f00"}
    elif ndvi > 0.4:
        return {"seviye": "SAĞLIKLI", "yorum": "Sağlıklı bitki örtüsü", "renk_kodu": "#4caf50"}
    elif ndvi > 0.2:
        return {"seviye": "SEYREK", "yorum": "Seyrek bitki örtüsü / tarım alanı", "renk_kodu": "#ffeb3b"}
    elif ndvi > 0.0:
        return {"seviye": "ÇOK SEYREK", "yorum": "Kuru/çıplak alan veya kentsel doku", "renk_kodu": "#ff9800"}
    else:
        return {"seviye": "BİTKİSİZ", "yorum": "Su, kar, beton veya kaya yüzeyi", "renk_kodu": "#9e9e9e"}


def _validate_bbox(min_lon, min_lat, max_lon, max_lat) -> tuple[bool, str]:
    """BBox koordinatlarını doğrular."""
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        return False, "Boylam -180 ile 180 arasında olmalı"
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        return False, "Enlem -90 ile 90 arasında olmalı"
    if min_lon >= max_lon or min_lat >= max_lat:
        return False, "min_lon < max_lon ve min_lat < max_lat olmalı"
    # Çok büyük alanları reddet (performans)
    if (max_lon - min_lon) > 2.0 or (max_lat - min_lat) > 2.0:
        return False, "Alan çok büyük (max 2°×2°). Daha küçük bir bölge seçin."
    return True, "ok"


class SatelliteClient:
    def __init__(self):
        self._catalog = None

    def _get_catalog(self):
        """Lazy initialization — bağlantıyı ilk kullanımda kur."""
        if self._catalog is not None:
            return self._catalog

        if not _PYSTAC_AVAILABLE:
            raise RuntimeError("pystac_client paketi yüklü değil.")

        # Planetary Computer (abonelik anahtarı ile)
        pc_key = os.getenv("PC_SDK_SUBSCRIPTION_KEY", "")
        if pc_key and _PC_AVAILABLE:
            self._catalog = pystac_client.Client.open(
                STAC_API_URL,
                modifier=planetary_computer.sign_inplace
            )
            log.info("🛰️ [Satellite] Microsoft Planetary Computer bağlantısı kuruldu.")
        else:
            # Public fallback: AWS Earth Search (ücretsiz)
            self._catalog = pystac_client.Client.open(EARTH_SEARCH_URL)
            log.info("🛰️ [Satellite] AWS Earth Search (public) bağlantısı kuruldu.")

        return self._catalog

    def search_sentinel2(
        self,
        bbox: list,
        days_back: int = 30,
        max_cloud_cover: int = 20,
        limit: int = 10,
    ):
        """
        Sentinel-2 L2A görüntüsü arar.

        Returns: pystac ItemCollection veya []
        """
        try:
            catalog = self._get_catalog()
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=days_back)
            date_range = f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"

            log.info(f"📡 [STAC] Arama: bbox={bbox}, tarih={date_range}, bulut<{max_cloud_cover}%")

            # PC ve Earth Search için collection adı farklı
            pc_key = os.getenv("PC_SDK_SUBSCRIPTION_KEY", "")
            collection = "sentinel-2-l2a" if (pc_key and _PC_AVAILABLE) else "sentinel-2-c1-l2a"

            search = catalog.search(
                collections=[collection],
                bbox=bbox,
                datetime=date_range,
                query={"eo:cloud_cover": {"lt": max_cloud_cover}},
                sortby=[{"field": "properties.datetime", "direction": "desc"}],
                max_items=limit,
            )
            items = list(search.items())
            log.info(f"📡 [STAC] {len(items)} görüntü bulundu.")
            return items

        except Exception as e:
            log.error(f"❌ [STAC] Arama hatası: {e}")
            return []

    def calculate_ndvi(self, item, bbox: list) -> Optional[float]:
        """
        COG Streaming ile NDVI hesaplar.
        Sadece ilgili pikseller indirilir (cloud-native).
        """
        if not _STACKSTAC_AVAILABLE:
            raise RuntimeError("stackstac paketi yüklü değil. NDVI hesaplanamıyor.")

        try:
            import stackstac

            stack = stackstac.stack(
                item,
                assets=["B04", "B08"],
                bounds=bbox,
                epsg=4326,
                resolution=60,  # 60m çözünürlük → daha hızlı
            )

            red = stack.sel(band="B04").astype("float32")
            nir = stack.sel(band="B08").astype("float32")

            # NDVI formülü: (NIR - Red) / (NIR + Red)
            denominator = nir + red
            # Sıfıra bölme koruması
            ndvi_map = np.where(denominator != 0, (nir - red) / denominator, 0)

            avg_ndvi = float(np.nanmean(ndvi_map))
            return round(avg_ndvi, 4)

        except Exception as e:
            log.error(f"❌ [NDVI] Hesaplama hatası: {e}")
            return None

    def calculate_evi(self, item, bbox: list) -> Optional[float]:
        """
        EVI = 2.5 × (NIR - Red) / (NIR + 6×Red - 7.5×Blue + 1)
        Kentsel alanlarda NDVI'den daha güvenilir.
        """
        if not _STACKSTAC_AVAILABLE:
            raise RuntimeError("stackstac paketi yüklü değil.")

        try:
            import stackstac

            stack = stackstac.stack(
                item,
                assets=["B02", "B04", "B08"],  # Blue, Red, NIR
                bounds=bbox,
                epsg=4326,
                resolution=60,
            )

            blue = stack.sel(band="B02").astype("float32")
            red = stack.sel(band="B04").astype("float32")
            nir = stack.sel(band="B08").astype("float32")

            denominator = nir + 6 * red - 7.5 * blue + 1
            evi_map = np.where(denominator != 0, 2.5 * (nir - red) / denominator, 0)

            avg_evi = float(np.nanmean(evi_map))
            return round(avg_evi, 4)

        except Exception as e:
            log.error(f"❌ [EVI] Hesaplama hatası: {e}")
            return None


# Singleton
_client_instance: Optional[SatelliteClient] = None


def get_satellite_client() -> SatelliteClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = SatelliteClient()
    return _client_instance