"""
api/schemas.py — Mobil API Pydantic Modelleri

Tüm request/response şemaları tek dosyada.
ApiResponse[T] generic envelope ile mobil app her zaman aynı yapıyı bekler.

v4.0 — POI Overlay (Tam Ekran Swipe Kartları) + Routing Phase eklendi
"""
from __future__ import annotations
from typing import TypeVar, Generic, Optional, Any, List
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
# CHAT — POI OVERLAY & ROUTING PHASE CARDS
# ═══════════════════════════════════════════════════════════════════════════

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = "default_session"
    current_lat: Optional[float] = None
    current_lon: Optional[float] = None
    fcm_token: Optional[str] = None


class TripPlanRequest(BaseModel):
    """
    Structured trip planning request — TripSetupWizard'dan gelir.
    LLM'e doğal dil mesaj olarak değil, deterministic parametreler olarak işlenir.
    """
    origin: str = Field(default="CURRENT_LOCATION", description="Başlangıç noktası ('CURRENT_LOCATION' veya 'lat,lon' veya şehir adı)")
    destination: str = Field(description="Hedef şehir veya adres")
    waypoints: List[str] = Field(default=[], description="Ara duraklar: ['Bolu', 'Düzce']")
    break_interval_hours: float = Field(default=2.0, ge=0.5, le=8.0, description="Kaç saatte bir mola (0=molasız)")
    food_preference: str = Field(default="Fark etmez", description="Yöresel, Fast Food, Ev Yemeği, Kahve & Tatlı, Fark etmez")
    food_location: str = Field(default="Ortaları", description="Rotanın neresinde yemek: Başları, Ortaları, Sonları, belirli şehir adı")
    fuel_remaining_km: float = Field(default=0, ge=0, le=1200, description="Anlık yakıt ile gidilebilecek km (0 = bilinmiyor)")
    custom_note: str = Field(default="", max_length=500, description="Serbest not: 'sahil yolundan git'")
    session_id: str = Field(default="default_session")
    current_lat: Optional[float] = None
    current_lon: Optional[float] = None
    fcm_token: Optional[str] = None


class TripAddStopsRequest(BaseModel):
    """
    Kullanıcının seçtiği durakları doğrudan rotaya ekler.
    LLM kullanılmaz — tamamen deterministik.
    """
    session_id: str = Field(default="default_session")
    # Seçilen koordinatlar: [{"lat": 41.1, "lon": 29.2, "name": "..."}]
    selected_stops: List[dict] = Field(description="Seçilen durак koordinatları")
    # Mevcut rota bilgisi
    origin: Optional[str] = Field(default=None, description="Başlangıç koordinatı")
    destination: Optional[str] = Field(default=None, description="Hedef şehir/koordinat")
    current_lat: Optional[float] = None
    current_lon: Optional[float] = None


class MapMarker(BaseModel):
    lat: float
    lon: float
    title: Optional[str] = None
    type: Optional[str] = "poi"   # poi, pharmacy, fuel_station, waypoint
    icon: Optional[str] = None
    snippet: Optional[str] = None
    # ★ Zengin POI kart metadata'sı
    poi_card: Optional[dict] = None  # Kaydırılabilir kart için detaylı bilgi


class LatLon(BaseModel):
    lat: float
    lon: float


class MapBounds(BaseModel):
    ne: LatLon
    sw: LatLon


class MapData(BaseModel):
    markers: list[MapMarker] = []
    polyline: Optional[str] = None
    alternatives: list[str] = []  # 🗺️ Alternatif rotalar için
    bounds: Optional[MapBounds] = None
    center: Optional[dict] = None  # {"lat": float, "lon": float}
    geojson_layers: list[dict] = []


class ActionCard(BaseModel):
    id: str
    label: str
    action: str    # "ui:<type>" = mobil handle eder | "<metin>" = AI'a gönderilir
    icon: str
    style: str = "secondary"  # primary, secondary, danger
    # Mobil UI'ın kullanıcıdan input alıp dolduracağı şablon
    # Örn: "Yakıt analizimi yap, {range_km} km menzilim var, güzergahtaki istasyonları bul"
    action_template: Optional[str] = None
    # True ise AI'a gönderilmez, mobil direkt handle eder
    is_ui_only: bool = False
    ui_component: Optional[str] = None # 'button', 'slider', 'radio'
    options: Optional[List[str]] = None # radio seçenekleri
    min_val: Optional[int] = None # slider min
    max_val: Optional[int] = None # slider max


# ─────────────────────────────────────────────────────────────────────────
# ★ POI OVERLAY — Tam Ekran Swipe Kartları (v4.0 YENİ)
# ─────────────────────────────────────────────────────────────────────────

class PoiOverlayCard(BaseModel):
    """
    Tekil mekan kartı — tam ekran overlay'de swipe ile gösterilir.
    Mobil: PageView/SwipeableCard bileşeni bu modeli kullanır.
    """
    id: str = Field(description="Benzersiz kart ID'si (Google Place ID veya UUID)")
    name: str = Field(description="Mekan adı")
    address: Optional[str] = Field(default=None, description="Tam adres")
    category: Optional[str] = Field(default=None, description="restoran, kafe, benzin_istasyonu, market vb.")
    lat: float
    lon: float

    # Navigasyon bilgileri
    deviation_meters: Optional[int] = Field(default=None, description="Rota'dan sapma (metre)")
    distance_along_route_km: Optional[float] = Field(default=None, description="Rota üzerindeki km")
    extra_time_min: Optional[int] = Field(default=None, description="Bu durağın eklediği süre (dk)")
    eta: Optional[str] = Field(default=None, description="Tahmini varış: 'HH:MM' formatında")
    on_route_side: Optional[str] = Field(default=None, description="right, left, unknown")

    # Kalite bilgileri
    rating: Optional[float] = Field(default=None, ge=0, le=5)
    review_count: Optional[int] = Field(default=None)
    price_level: Optional[int] = Field(default=None, ge=0, le=4, description="0=ücretsiz, 1=ucuz, 2=orta, 3=pahalı, 4=çok pahalı")
    is_open: Optional[bool] = Field(default=None)
    open_now: Optional[bool] = Field(default=None)
    opening_hours: Optional[list] = Field(default=None)
    phone: Optional[str] = Field(default=None)

    # Yakıt fiyat bilgisi (fuel_station tipi için)
    fuel_price_info: Optional[dict] = Field(
        default=None,
        description="{'price_per_liter': 45.2, 'company': 'Opet', 'fuel_type': 'benzin'}"
    )

    # UI için hesaplanmış alanlar
    deviation_label: Optional[str] = Field(default=None, description="'Yol üstü ✅', '850m sapma ⚠️', '3.2km uzatır ❌'")
    route_impact_label: Optional[str] = Field(default=None, description="'+5 dk', 'Sıfır etki', '+12 dk'")
    is_recommended: bool = Field(default=False, description="AI'ın öne çıkardığı mekan mı?")
    recommendation_reason: Optional[str] = Field(default=None, description="'En yüksek puan', 'Yol üstü', 'En ucuz'")
    # AI tarafından üretilen tek cümlelik öneri metni (kart açıklaması)
    ai_recommendation: Optional[str] = Field(
        default=None,
        description="Neden bu mekanı öneriyoruz — kısa, özgün açıklama. Örn: 'Düzce geçişinde yol üstünde, yüksek puanlı ve açık.'"
    )


class PoiOverlay(BaseModel):
    """
    Tam ekran POI overlay container.
    Mobil bu nesneyi alınca harita yerine tam ekran swipe arayüzü açar.
    """
    mode: str = Field(
        default="poi_selection",
        description="poi_selection | route_confirmation | final_summary | trip_plan"
    )
    title: str = Field(description="Overlay başlığı: 'Yol Üzerindeki Restoranlar'")
    subtitle: Optional[str] = Field(default=None, description="'3 mekan bulundu, kaydırarak incele'")
    cards: List[PoiOverlayCard] = Field(default=[], description="Swipe kartları")
    primary_action: Optional[str] = Field(
        default=None,
        description="Birincil buton metni: 'Rotama Ekle', 'Navigasyona Başla'"
    )
    secondary_action: Optional[str] = Field(
        default=None,
        description="İkincil buton metni: 'Atla', 'Farklı Mekan Öner'"
    )
    # Rota özeti için (mode=final_summary / trip_plan)
    route_summary: Optional[dict] = Field(
        default=None,
        description="Tamamlanan rota özeti: {total_km, total_min, stops, warnings}"
    )
    # Hava durumu uyarıları (rota boyunca)
    weather_warnings: Optional[List[dict]] = Field(
        default=None,
        description="[{'location': 'Bolu', 'condition': 'Kar', 'severity': 'warning'}]"
    )
    # Trip plan modu için kategorize duraklar
    sections: Optional[List[dict]] = Field(
        default=None,
        description="[{'type': 'food', 'title': 'Yemek Durağı', 'cards': [...]}]"
    )


# ─────────────────────────────────────────────────────────────────────────
# ★ ROUTING PHASE — Akıllı Faz Yönetimi
# ─────────────────────────────────────────────────────────────────────────

class RoutingPhaseInfo(BaseModel):
    """
    Mobil uygulamanın routing UI state machine'i için faz bilgisi.
    Mobil bu nesneye göre hangi UI'ı göstereceğine karar verir.
    """
    phase: int = Field(
        description=(
            "1=İlk rota (harita + action cards), "
            "2=POI sorgu (overlay açıldı), "
            "3=Seçim yapıldı (güncellenen rota), "
            "4=Onay (final summary)"
        )
    )
    phase_label: str = Field(description="'Rota Hazır', 'Mekan Seçimi', 'Rota Güncellendi', 'Yolculuk Özeti'")
    has_active_route: bool = Field(default=False)
    active_destination: Optional[str] = Field(default=None)
    waypoints_count: int = Field(default=0)


class ChatResponse(BaseModel):
    status: str = Field(default="completed", description="completed | action_required")
    message: str
    intent: Optional[dict] = None
    map: MapData = Field(default_factory=MapData)
    action_cards: list[ActionCard] = []
    tools_used: list[str] = Field(default=[], description="Kullanılan MCP tool adları")
    distance_km: Optional[float] = None
    duration_min: Optional[int] = None
    # Tam ekran POI overlay — doluysa mobil swipe kartları gösterir
    poi_overlay: Optional[PoiOverlay] = Field(
        default=None,
        description="Doluysa harita yerine tam ekran POI swipe overlay açılır."
    )
    # Trip plan modu — yapılandırılmış yolculuk özeti
    trip_plan: Optional[dict] = Field(
        default=None,
        description="Structured trip plan: {origin, destination, total_km, breaks, fuel_stops, food_stops}"
    )


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
