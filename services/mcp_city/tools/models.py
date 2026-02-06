# services/mcp_city/models.py
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal

# 1. OSM İsteği Validasyonu
class OSMRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Geçerli bir enlem olmalı")
    lon: float = Field(..., ge=-180, le=180, description="Geçerli bir boylam olmalı")
    category: str = Field(..., description="Aranacak yerin İngilizce OSM etiketi (Örn: cafe, park, supermarket, commercial, clothing).")
    radius: int = Field(default=2000, ge=100, le=50000)

# 2. Google Arama Validasyonu
class GoogleSearchRequest(BaseModel):
    query: str = Field(..., min_length=2, description="Arama metni çok kısa olamaz")
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lon: Optional[float] = Field(None, ge=-180, le=180)
    route_polyline: Optional[str] = Field(None, description="HERE Maps'ten gelen encoded polyline string")
# 3. Rota İsteği Validasyonu
class RouteRequest(BaseModel):
    origin: str
    destination: str

    @field_validator('origin', 'destination')
    def validate_coordinates(cls, v):
        # Boşlukları temizle
        v = v.replace(" ", "")
        # Format kontrolü: "lat,lon" (virgül şart)
        if "," not in v:
            raise ValueError(f"Koordinat formatı hatalı: '{v}'. Beklenen: 'lat,lon'")
        
        try:
            parts = v.split(",")
            lat, lon = float(parts[0]), float(parts[1])
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                raise ValueError("Koordinatlar dünya sınırları dışında.")
        except ValueError:
            raise ValueError(f"Sayısal koordinat girilmeli: '{v}'")
            
        return v
    

# --- ÇIKIŞ MODELLERİ (OUTPUT MODELS) ---

class StandardPlace(BaseModel):
    """Google ve OSM sonuçlarını bu formatta eşitleyeceğiz."""
    name: str
    address: Optional[str] = "Adres yok"
    lat: float
    lon: float
    category: str = "general"
    rating: Optional[float] = None
    is_open: Optional[str] = None
    source: str = "unknown" # 'google', 'osm'

class WeatherResponse(BaseModel):
    location: str
    current_temp: str
    feels_like: str
    condition: str
    forecast_hourly: list[dict]
    warning: Optional[str] = None

class RouteResponse(BaseModel):
    distance_km: float
    duration_min: float
    polyline: str
    summary: str
    checkpoints: dict