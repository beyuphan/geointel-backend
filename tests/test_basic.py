import pytest
from unittest.mock import AsyncMock, patch
from services.mcp_city.server import (
    search_infrastructure_osm, 
    search_places_google, 
    get_route_data, 
    get_weather
)

# --- 1. OSM TESTİ (Tag Mapper Kontrolü) ---
@pytest.mark.asyncio
async def test_osm_airport_search():
    """
    OSM fonksiyonunun 'airport' kategorisini doğru anlayıp,
    fake veriyi doğru işlediğini test eder.
    """
    # OSM'den dönecek sahte veri (Mock)
    mock_osm_response = {
        "elements": [
            {
                "type": "node",
                "id": 123,
                "lat": 41.17,
                "lon": 40.84,
                "tags": {"name": "Rize-Artvin Havalimanı", "aeroway": "aerodrome"}
            }
        ]
    }

    # httpx.AsyncClient.post metodunu ele geçiriyoruz (Intercept)
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = AsyncMock(status_code=200, json=lambda: mock_osm_response)

        # Fonksiyonu çağır
        results = await search_infrastructure_osm(lat=41.0, lon=40.0, category="airport")

        # KONTROLLER
        assert len(results) == 1
        assert results[0]["isim"] == "Rize-Artvin Havalimanı"
        assert results[0]["kategori"] == "airport"
        # Bakalım 'airport' deyince arkada doğru tag'i kullanmış mı?
        # mock_post.call_args[1]['data'] içinde query string olmalı
        # Query içinde 'aeroway' geçiyor mu?
        assert "aeroway" in mock_post.call_args[1]['data']

# --- 2. GOOGLE MAPS TESTİ (Ticari Arama) ---
@pytest.mark.asyncio
async def test_google_places_parsing():
    """
    Google'dan gelen karmaşık JSON'ı bizim sade formatımıza 
    çevirip çevirmediğini test eder.
    """
    mock_google_response = {
        "results": [
            {
                "name": "Nalia Karadeniz Mutfağı",
                "formatted_address": "Rize Merkez",
                "rating": 4.5,
                "geometry": {"location": {"lat": 41.02, "lng": 40.52}}
            }
        ]
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.return_value = AsyncMock(status_code=200, json=lambda: mock_google_response)

        results = await search_places_google(query="Rize Restoran")

        assert len(results) == 1
        assert results[0]["isim"] == "Nalia Karadeniz Mutfağı"
        assert results[0]["puan"] == 4.5
        assert "lat" in results[0]

# --- 3. ROTA HESAPLAMA TESTİ (Matematik Kontrolü) ---
@pytest.mark.asyncio
async def test_route_calculation():
    """
    Metre -> KM ve Saniye -> Dakika dönüşümü doğru mu?
    """
    mock_here_response = {
        "routes": [{
            "sections": [{
                "summary": {
                    "length": 35800, # 35.8 km
                    "duration": 1980 # 33 dakika
                }
            }]
        }]
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.return_value = AsyncMock(status_code=200, json=lambda: mock_here_response)

        data = await get_route_data(origin="41.0,40.0", destination="41.1,40.1")

        assert data["mesafe_km"] == 35.8
        assert data["sure_dk"] == 33.0

# --- 4. HAVA DURUMU TESTİ (Basit Kontrol) ---
@pytest.mark.asyncio
async def test_weather_fetching():
    mock_weather_response = {
        "current": {
            "temp": 15.5,
            "weather": [{"description": "parçalı bulutlu"}]
        }
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.return_value = AsyncMock(status_code=200, json=lambda: mock_weather_response)

        data = await get_weather(lat=41.0, lon=40.0)

        assert data["sicaklik"] == 15.5
        assert data["durum"] == "parçalı bulutlu"

# --- 5. HATA SENARYOSU TESTİ ---
@pytest.mark.asyncio
async def test_api_failure():
    """API 500 verirse kod patlıyor mu?"""
    with patch("httpx.AsyncClient.post") as mock_post:
        # OSM sunucusu hata verdi diyelim
        mock_post.return_value = AsyncMock(status_code=500, text="Internal Server Error")

        results = await search_infrastructure_osm(lat=41.0, lon=40.0, category="park")
        
        # Kodun patlamayıp, içinde "error" olan bir liste dönmesi lazım
        assert isinstance(results, list)
        assert "error" in results[0]