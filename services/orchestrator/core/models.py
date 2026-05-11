import operator
from typing import Literal, List, Dict, Any, Union, Annotated, TypedDict, Optional
from pydantic import BaseModel, create_model, Field

class IntentAnalysis(BaseModel):
    category: Literal["fuel", "pharmacy", "event", "routing", "city_data", "places", "general"] = Field(
        description="Kullanıcının isteğinin ana kategorisi"
    )
    urgency: bool = Field(description="İşlem acil mi?")
    focus_points: List[str] = Field(description="Anahtar kelimeler")
    complexity: Literal["low", "high"] = Field(
        description="Basit bilgi çekme ve arama (Örn: Eczane nerede, fiyatlar nedir) için 'low'. "
                    "Çoklu adım, analiz, rota kıyaslaması ve çevresel faktör sentezi için 'high'."
    )

class AgentState(TypedDict):
    messages: Annotated[List[Any], operator.add]
    intent: Dict[str, Any]
    retry_count: int
    session_id: str 
    visual_data: Dict[str, Any]
    route_polyline: Optional[str]
    # Phase 1=İlk rota | 2=POI önerisi (yemek/mola/yakıt) | 3=Seçim yapıldı | 4=Final onay
    routing_phase: Optional[int]
    # Son POI aramasından gelen mekanlar (overlay kart sistemi için)
    poi_suggestions: Optional[List[Dict[str, Any]]]
