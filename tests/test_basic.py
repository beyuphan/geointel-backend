import pytest
from unittest.mock import AsyncMock, patch
from services.mcp_city.server import (
    search_infrastructure_osm, 
    search_places_google, 
    get_route_data, 
    get_weather
)

# --- 1. OSM TESTİ ---
@pytest.mark.asyncio
async def test_osm_airport_search():
    """OSM testi"""
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

        # DÜZELTME BURADA: .fn EKLENDİ 👇
        results = await search_infrastructure_osm.fn(lat=41.0, lon=40.0, category="airport")

        assert len(results) == 1
        assert results[0]["isim"] == "Rize-Artvin Havalimanı"
        assert results[0]["kategori"] == "airport"
        assert "aeroway" in mock_post.call_args[1]['data']

# --- 2. GOOGLE MAPS TESTİ ---
@pytest.mark.asyncio
async def test_google_places_parsing():
    """Google testi"""
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

        # DÜZELTME BURADA: .fn EKLENDİ 👇
        results = await search_places_google.fn(query="Rize Restoran")

        assert len(results) == 1
        assert results[0]["isim"] == "Nalia Karadeniz Mutfağı"
        assert results[0]["puan"] == 4.5

# --- 3. ROTA HESAPLAMA TESTİ ---
@pytest.mark.asyncio
async def test_route_calculation():
    """Rota testi"""
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

        # DÜZELTME BURADA: .fn EKLENDİ 👇
        data = await get_route_data.fn(origin="41.0,40.0", destination="41.1,40.1")

        assert data["mesafe_km"] == 35.8
        assert data["sure_dk"] == 33.0

# --- 4. HAVA DURUMU TESTİ ---
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

        # DÜZELTME BURADA: .fn EKLENDİ 👇
        data = await get_weather.fn(lat=41.0, lon=40.0)

        assert data["sicaklik"] == 15.5
        assert data["durum"] == "parçalı bulutlu"

# --- 5. HATA SENARYOSU TESTİ ---
@pytest.mark.asyncio
async def test_api_failure():
    """Hata testi"""
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = AsyncMock(status_code=500, text="Internal Server Error")

        # DÜZELTME BURADA: .fn EKLENDİ 👇
        results = await search_infrastructure_osm.fn(lat=41.0, lon=40.0, category="park")
        
        assert isinstance(results, list)
        assert "error" in results[0]