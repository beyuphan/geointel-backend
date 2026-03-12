import pytest
from services.mcp_city.tools.geometry import filter_places_by_polyline

@pytest.mark.asyncio
async def test_pazar_direction_bug():
    # 1. Rize -> Trabzon Polylinesı (Basitleştirilmiş)
    # Rize (41.02, 40.52) -> Trabzon (41.00, 39.71)
    mock_route = "encoded_polyline_rize_trabzon" 
    
    # 2. Mekanlar: Biri Trabzon yönünde (İyidere), biri ters yönde (Pazar)
    places = [
        {"name": "Pazar Petrol", "lat": 41.17, "lon": 40.88}, # TERS YÖN (Doğu)
        {"name": "İyidere Petrol", "lat": 41.01, "lon": 40.35} # DOĞRU YÖN (Batı)
    ]

    # TODO: geometry.py'a "direction/progress" kontrolü eklenince bu test geçmeli
    results = filter_places_by_polyline(places, encoded_polyline=mock_route)
    
    # Pazar elenmiş olmalı, sadece İyidere kalmalı
    names = [p["name"] for p in results]
    assert "Pazar Petrol" not in names
    assert "İyidere Petrol" in names