import operator
from typing import Literal, List, Dict, Any, Union, Annotated, TypedDict, Optional
from pydantic import BaseModel, Field


class IntentAnalysis(BaseModel):
    category: Literal[
        "routing",      # Rota, navigasyon, yol tarifi
        "fuel",         # Yakıt fiyatı, benzin istasyonu
        "pharmacy",     # Nöbetçi eczane, ilaç
        "event",        # Konser, maç, etkinlik
        "city_data",    # İBB WFS, ISPARK, afet
        "places",       # Kafe, restoran, mekan arama
        "day_plan",     # Gün planlama ("günümü planla", "bugün ne yapayım")
        "general",      # Serbest sohbet
    ] = Field(description="Kullanıcı isteğinin kategorisi")
    urgency: bool = Field(default=False, description="Acil mi?")
    focus_points: List[str] = Field(default=[], description="Anahtar kelimeler")
    complexity: Literal["low", "high"] = Field(
        default="low",
        description="Tek adımlı: 'low'. Çok adımlı analiz/sentez: 'high'."
    )


class AgentState(TypedDict):
    messages: Annotated[List[Any], operator.add]
    intent: Dict[str, Any]           # classify_intent'ten gelen sonuç
    retry_count: int
    session_id: str
    visual_data: Dict[str, Any]      # markers, polyline, geojson
    route_polyline: Optional[str]    # Redis'teki polyline kopyası
