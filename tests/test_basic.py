# tests/test_basic.py
from pydantic import ValidationError
import pytest
from services.mcp_city.server import RouteQuery, WeatherQuery

# Bu test, Rota sorgusu modelimizin veri tiplerini kontrol eder
def test_route_model_works():
    # Doğru veri girince hata vermemeli
    query = RouteQuery(origin="Rize", destination="Trabzon")
    assert query.origin == "Rize"
    assert query.destination == "Trabzon"

# Bu test, eksik veri girince sistemin hata verip vermediğini kontrol eder
def test_route_model_fails_correctly():
    # Destination yazmazsak hata patlamalı (Beklediğimiz davranış bu)
    with pytest.raises(ValidationError):
        RouteQuery(origin="Sadece Rize")