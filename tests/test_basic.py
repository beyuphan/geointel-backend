import pytest
from unittest.mock import patch, AsyncMock
from services.mcp_city.tools.osm import search_infrastructure_osm_handler
from services.mcp_city.tools.google import search_places_google_handler
from services.mcp_city.tools.here import get_route_data_handler

@pytest.mark.asyncio
async def test_osm_search():
    """OSM Handler Testi"""
    mock_response = {
        "elements": [
            {"tags": {"name": "Rize Meydan"}, "lat": 41.0, "lon": 40.5}
        ]
    }

    # httpx.AsyncClient.post metodunu taklit ediyoruz (Mock)
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = AsyncMock(status_code=200, json=lambda: mock_response)
        
        results = await search_infrastructure_osm_handler(lat=41.0, lon=40.5, category="square")
        
        assert len(results) > 0
        assert results[0]["isim"] == "Rize Meydan"

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

    # 1. Google API çağrısını Mockla
    # 2. Redis Store'u Mockla (Test ortamında Redis yok diye patlamasın)
    with patch("httpx.AsyncClient.get") as mock_get, \
         patch("services.mcp_city.tools.google.redis_store") as mock_redis:
        
        # Redis'ten 'LATEST' rota sorgusu gelirse None dönsün (Hafıza boş gibi davransın)
        mock_redis.get_route.return_value = None
        
        # Google API başarılı dönsün
        mock_get.return_value = AsyncMock(status_code=200, json=lambda: mock_google_response)

        results = await search_places_google_handler(query="Rize Restoran")

        # Hata kontrolü: Eğer sonuçta 'error' varsa testi fail et
        if results and "error" in results[0]:
            pytest.fail(f"Google Handler Hata Döndü: {results[0]['error']}")

        assert len(results) == 1
        assert results[0]["isim"] == "Nalia"

@pytest.mark.asyncio
async def test_route_calculation():
    """Here Maps Handler Testi"""
    mock_here_response = {
        "routes": [{
            "sections": [{
                "summary": {
                    "length": 35800,
                    "duration": 1980
                },
                # EKSİK OLAN KISIM BURASIYDI:
                "polyline": "mock_polyline_string_data" 
            }]
        }]
    }

    # Redis'e yazmaya çalışacağı için Redis'i de mockluyoruz
    with patch("httpx.AsyncClient.get") as mock_get, \
         patch("services.mcp_city.tools.here.redis_store") as mock_redis:
        
        mock_get.return_value = AsyncMock(status_code=200, json=lambda: mock_here_response)
        
        # flexpolyline decode kısmını da mocklayabiliriz ama basit string versek de çalışır
        # Burada flexpolyline.decode'un patlamaması için basit bir trick veya 
        # mock polyline'ın decode edilebilir olması lazım. 
        # En temizi flexpolyline'i de mocklamak.
        with patch("services.mcp_city.tools.here.flexpolyline.decode") as mock_decode:
            # Fake koordinat listesi dön
            mock_decode.return_value = [(41.0, 40.0), (41.05, 40.05), (41.1, 40.1)]
            
            # get_location_name fonksiyonunu da mockla (Google'a gitmesin)
            with patch("services.mcp_city.tools.here.get_location_name", return_value="Rize Merkez"):
                
                data = await get_route_data_handler(origin="41.0,40.0", destination="41.1,40.1")

                if "error" in data:
                    pytest.fail(f"Rota Hatası: {data['error']}")

                assert data["mesafe_km"] == 35.8
                assert data["analiz_noktalari"]["orta_nokta"]["ad"] == "Rize Merkez"

@pytest.mark.asyncio
async def test_validation_error():
    """Pydantic validasyonunun çalışıp çalışmadığını test eder"""
    
    # Hatalı kategori gönderiyoruz
    results = await search_infrastructure_osm_handler(lat=41.0, lon=40.0, category="disko")

    assert "error" in results[0]
    # Hata mesajı Pydantic sürümüne göre değişebilir, genel bir kontrol yapalım
    error_msg = results[0]["error"].lower()
    
    # "validation error" veya "match pattern" gibi ifadeler olmalı
    assert "validation error" in error_msg or "input should match" in error_msg or "string should match" in error_msg