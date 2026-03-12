from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import Optional, List, Dict, Any, Union, Literal

# --- TEMEL MODELLER ---

class Coordinate(BaseModel):
    """Enlem ve Boylam için katı validasyon."""
    lat: float = Field(..., ge=-90, le=90, description="Enlem -90 ile 90 arasında olmalı")
    lon: float = Field(..., ge=-180, le=180, description="Boylam -180 ile 180 arasında olmalı")

# --- GİRİŞ MODELLERİ (INPUT) ---

class OSMRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    category: str = Field(..., min_length=2, description="OSM etiketi (Örn: hospital, park)")
    radius: int = Field(default=2000, ge=100, le=50000)

class GoogleSearchRequest(BaseModel):
    query: str = Field(..., min_length=2)
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lon: Optional[float] = Field(None, ge=-180, le=180)
    route_polyline: Optional[str] = Field(None, description="Rota üzerinde arama için polyline")

class RouteRequest(BaseModel):
    origin: str = Field(..., min_length=1, description="Başlangıç noktası veya koordinatı")
    destination: str = Field(..., min_length=1, description="Varış noktası veya koordinatı")

# --- ÇIKIŞ MODELLERİ (OUTPUT) ---

class StandardPlace(BaseModel):
    """Google ve OSM sonuçlarını eşitleyen ana model."""
    model_config = ConfigDict(extra='allow') # Ekstra metadata gelirse reddetme
    
    name: str = Field(..., min_length=1)
    address: Optional[str] = "Adres bilgisi yok"
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    category: str = "general"
    rating: Optional[float] = 0.0
    review_count: int = 0
    is_open: str = "Bilinmiyor"
    source: Literal["google", "osm"] = "google"
    metadata: Dict[str, Any] = Field(default_factory=dict)

class WeatherResponse(BaseModel):
    location: str
    current_temp: str
    feels_like: str
    condition: str
    forecast_hourly: List[Dict[str, Any]]
    warning: Optional[str] = None

class RouteResponse(BaseModel):
    distance_km: float
    duration_min: float
    polyline: str
    summary: str
    checkpoints: Dict[str, Any]
    source_system: str
    alternatives: List[Dict[str, Any]] = Field(default_factory=list, description="Alternatif rota seçenekleri (isim, mesafe_km, sure_dk, polyline_encoded)")

class ErrorResponse(BaseModel):
    """Standart hata formatı."""
    status: str = "error"
    message: str
    code: int = 500