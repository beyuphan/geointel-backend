import pytest
from unittest.mock import AsyncMock, patch

# ARTIK DOĞRUDAN HANDLER'LARI ÇAĞIRIYORUZ (DAHA TEMİZ)
from services.mcp_city.tools.osm import search_infrastructure_osm_handler
from services.mcp_city.tools.google import search_places_google_handler
from services.mcp_city.tools.here import get_route_data_handler
from services.mcp_city.tools.weather import get_weather_handler

# --- 1. OSM TESTİ ---
@pytest.mark.asyncio
async def test_osm_airport_search():
    """OSM Handler Testi"""
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

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = AsyncMock(status_code=200, json=lambda: mock_osm_response)

        # .fn falan yok, direkt fonksiyonu çağırıyoruz
        results = await search_infrastructure_osm_handler(lat=41.0, lon=40.0, category="airport")

        assert len(results) == 1
        assert results[0]["isim"] == "Rize-Artvin Havalimanı"
        assert "aeroway" in mock_post.call_args[1]['data']

# --- 2. GOOGLE MAPS TESTİ ---
@pytest.mark.asyncio
async def test_google_places_parsing():
    """Google Handler Testi"""
    mock_google_response = {
        "results": [
            {
                "name": "Nalia",
                "formatted_address": "Rize",
                "rating": 4.5,
                "geometry": {"location": {"lat": 41.02, "lng": 40.52}}
            }
        ]
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.return_value = AsyncMock(status_code=200, json=lambda: mock_google_response)

        results = await search_places_google_handler(query="Rize Restoran")

        assert len(results) == 1
        assert results[0]["isim"] == "Nalia"

# --- 3. ROTA HESAPLAMA TESTİ ---
@pytest.mark.asyncio
async def test_route_calculation():
    """Here Maps Handler Testi"""
    mock_here_response = {
        "routes": [{
            "sections": [{
                "summary": {
                    "length": 35800, 
                    "duration": 1980 
                }
            }]
        }]
    }

    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.return_value = AsyncMock(status_code=200, json=lambda: mock_here_response)

        data = await get_route_data_handler(origin="41.0,40.0", destination="41.1,40.1")

        assert data["mesafe_km"] == 35.8
        assert data["sure_dk"] == 33.0

# --- 4. VALIDASYON HATASI TESTİ (YENİ) ---
@pytest.mark.asyncio
async def test_validation_error():
    """Pydantic validasyonunun çalışıp çalışmadığını test eder"""
    # Hatalı kategori gönderiyoruz ("disko" diye bir kategori yok)
    results = await search_infrastructure_osm_handler(lat=41.0, lon=40.0, category="disko")
    
    # Listenin ilk elemanında "error" olmalı
    assert "error" in results[0]
    assert "Hatalı Parametre" in results[0]["error"]