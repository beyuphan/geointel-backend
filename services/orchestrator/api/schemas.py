"""
api/schemas.py — Mobil API Pydantic Modelleri

Tüm request/response şemaları tek dosyada.
ApiResponse[T] generic envelope ile mobil app her zaman aynı yapıyı bekler.
"""
from __future__ import annotations
from typing import TypeVar, Generic, Optional, Any
from pydantic import BaseModel, Field
from datetime import datetime

T = TypeVar("T")


# ═══════════════════════════════════════════════════════════════════════════
# GENERIC ENVELOPE
# ═══════════════════════════════════════════════════════════════════════════

class ApiError(BaseModel):
    code: str = Field(description="Hata kodu: AUTH_REQUIRED, VALIDATION_ERROR, SERVER_ERROR vb.")
    message: str = Field(description="Kullanıcıya gösterilebilir hata mesajı")


class ApiMetadata(BaseModel):
    api_version: str = "v1"
    response_time_ms: Optional[int] = None
    session_id: Optional[str] = None


class ApiResponse(BaseModel, Generic[T]):
    """Tüm API endpoint'lerinin standart response wrapper'ı."""
    success: bool
    data: Optional[T] = None
    error: Optional[ApiError] = None
    metadata: ApiMetadata = Field(default_factory=ApiMetadata)


# ═══════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=30, description="Kullanıcı adı")
    email: Optional[str] = Field(default=None, description="E-posta adresi (opsiyonel)")
    password: str = Field(min_length=6, max_length=128, description="Şifre")


class LoginRequest(BaseModel):
    username: str
    password: str


class ForgotPasswordRequest(BaseModel):
    email: str = Field(description="Kayıtlı e-posta adresi")


class ResetPasswordRequest(BaseModel):
    token: str = Field(description="Şifre sıfırlama token'ı")
    new_password: str = Field(min_length=6, max_length=128, description="Yeni şifre")


class TokenData(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token süresi (saniye)")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(description="Yenilenecek refresh token")


class UserPublic(BaseModel):
    id: str
    username: str
    email: Optional[str] = None
    created_at: Optional[str] = None


class AuthResponse(BaseModel):
    token: TokenData
    user: UserPublic


# ═══════════════════════════════════════════════════════════════════════════
# CHAT
# ═══════════════════════════════════════════════════════════════════════════

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = "default_session"
    current_lat: Optional[float] = None
    current_lon: Optional[float] = None
    fcm_token: Optional[str] = None


class MapMarker(BaseModel):
    lat: float
    lon: float
    title: Optional[str] = None
    type: Optional[str] = None  # origin, destination, poi, fuel_station
    icon: Optional[str] = None  # pin_green, pin_red, fuel, pharmacy


class LatLon(BaseModel):
    lat: float
    lon: float


class MapBounds(BaseModel):
    ne: LatLon
    sw: LatLon


class MapData(BaseModel):
    markers: list[MapMarker] = []
    polyline: Optional[str] = None
    bounds: Optional[MapBounds] = None
    center: Optional[dict] = None  # {"lat": float, "lon": float}
    geojson_layers: list[dict] = []


class ActionCard(BaseModel):
    id: str
    label: str
    action: str    # start_navigation, show_alternatives, show_on_map, compare_fuel
    icon: str      # navigation, swap, map, fuel
    style: str = "secondary"  # primary, secondary, danger


class ChatResponse(BaseModel):
    message: str
    intent: Optional[dict] = None
    map: MapData = Field(default_factory=MapData)
    action_cards: list[ActionCard] = []
    model_used: Optional[str] = None
    tools_used: list[str] = Field(default=[], description="Kullanılan MCP tool adları (context indicator)")
    fuel_data: Optional[dict] = None
    weather_warning: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# PROFILE
# ═══════════════════════════════════════════════════════════════════════════

class VehicleUpdate(BaseModel):
    brand: str = Field(description="Marka (ör: Toyota)")
    model: str = Field(description="Model (ör: Corolla)")
    year: int = Field(ge=1990, le=2030, description="Üretim yılı")
    fuel_type: str = Field(description="gasoline, diesel, lpg, electric")
    city_consumption: float = Field(ge=0, le=50, description="Şehir içi tüketim L/100km")
    highway_consumption: float = Field(ge=0, le=50, description="Uzun yol tüketim L/100km")


class VehicleResponse(BaseModel):
    id: Optional[int] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    fuel_type: str
    city_consumption: Optional[float] = None
    highway_consumption: Optional[float] = None
    avg_consumption: float
    is_primary: bool = True


class LocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50, description="Konum adı (Ev, İş, Spor Salonu)")
    coordinates: str = Field(description="lat,lon formatında koordinat")
    category: Optional[str] = Field(default="other", description="home, work, favorite, other")


class LocationResponse(BaseModel):
    id: int
    name: str
    coordinates: str
    category: Optional[str] = None


class PreferenceUpdate(BaseModel):
    key: str = Field(description="Tercih anahtarı (team, cuisine, music)")
    value: str = Field(description="Tercih değeri")


class PreferenceResponse(BaseModel):
    key: str
    value: str


class ProfileResponse(BaseModel):
    user: UserPublic
    vehicle: Optional[VehicleResponse] = None
    locations: list[LocationResponse] = []
    preferences: list[PreferenceResponse] = []


# ═══════════════════════════════════════════════════════════════════════════
# HISTORY
# ═══════════════════════════════════════════════════════════════════════════

class RouteHistoryItem(BaseModel):
    origin: str
    destination: str
    distance_km: float
    duration_min: float
    date: Optional[str] = None


class ChatHistoryItem(BaseModel):
    role: str   # user, assistant
    content: str


class ChatSessionItem(BaseModel):
    """Chat geçmişi session listesi."""
    session_id: str
    message_count: int
    last_message: Optional[str] = None
    last_activity: Optional[str] = None


class LocationUpdateRequest(BaseModel):
    lat: float
    lon: float
    session_id: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# ACCOUNT MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

class AccountExportResponse(BaseModel):
    """KVKK/GDPR uyumlu veri dışa aktarma."""
    user: UserPublic
    vehicle: Optional[VehicleResponse] = None
    locations: list[LocationResponse] = []
    preferences: list[PreferenceResponse] = []
    route_history: list[RouteHistoryItem] = []
    chat_sessions: list[ChatSessionItem] = []
    exported_at: str = Field(description="ISO 8601 formatında export zamanı")


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-VEHICLE
# ═══════════════════════════════════════════════════════════════════════════

class VehicleCreate(BaseModel):
    """Yeni araç ekleme (garage)."""
    brand: str = Field(description="Marka")
    model: str = Field(description="Model")
    year: int = Field(ge=1990, le=2030, description="Üretim yılı")
    fuel_type: str = Field(description="gasoline, diesel, lpg, electric")
    city_consumption: float = Field(ge=0, le=50, description="Şehir içi tüketim L/100km")
    highway_consumption: float = Field(ge=0, le=50, description="Uzun yol tüketim L/100km")
    is_primary: bool = Field(default=False, description="Ana araç mı?")


# ═══════════════════════════════════════════════════════════════════════════
# HEALTH / STATUS
# ═══════════════════════════════════════════════════════════════════════════

class ServiceStatus(BaseModel):
    name: str
    status: str = "unknown"   # online, offline, degraded
    latency_ms: Optional[int] = None


class HealthResponse(BaseModel):
    status: str = "ok"
    redis: bool = False
    services: list[ServiceStatus] = []
    tool_count: int = 0
