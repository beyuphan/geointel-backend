from typing import Dict, Any, Union

BASE_SYSTEM_PROMPT = """
Sen **GeoIntel**, konum tabanlı gerçek zamanlı seyahat asistanısın.

KURALLAR:
1. Asla tahmin yürütme — her veri için araç çağır. Bulamazsan 'Veri bulunamadı' de.
2. Rota yönünün tersindeki yerleri önerme.
3. Kullanıcı profilini (araç, takım, ev) kullan. Dizel ise Motorin baz al.
4. Samimi konuş, zincirleme düşün (rota→ilçe→fiyat).
5. Şikayet gelirse `report_poi_feedback` çağır, öneri öncesi `get_poi_blacklist` kontrol et.
6. Önceki rotada `route_polyline` lazımsa 'LATEST' yaz — sistem otomatik çözer.
"""

# Keyword-based complexity (replaces the old LLM intent classifier)
HIGH_KEYWORDS = [
    "rota", "route", "yol", "git", "navigasyon", "benzin", "yakıt", "fuel",
    "eczane", "pharmacy", "etkinlik", "maç", "hava", "weather", "trafik",
    "radar", "geçiş ücreti", "toll", "şarj", "ev şarj", "wfs", "ibb"
]

def classify_intent_fast(message: str) -> dict:
    """LLM çağrısı yapmadan, keyword regex ile intent belirle."""
    msg_lower = message.lower()
    
    # Kategori tespiti
    if any(k in msg_lower for k in ["rota", "route", "yol", "git", "navigasyon", "mesafe", "süre", "seyahat"]):
        category = "routing"
    elif any(k in msg_lower for k in ["benzin", "yakıt", "fuel", "motorin", "lpg", "akaryakıt", "mazot"]):
        category = "fuel"
    elif any(k in msg_lower for k in ["eczane", "pharmacy", "ilaç", "nöbetçi"]):
        category = "pharmacy"
    elif any(k in msg_lower for k in ["etkinlik", "konser", "festival", "event", "maç", "derbi"]):
        category = "event"
    elif any(k in msg_lower for k in ["wfs", "ibb", "dataset", "katman", "afet", "ispark"]):
        category = "city_data"
    else:
        category = "general"
    
    # Karmaşıklık
    is_high = any(k in msg_lower for k in HIGH_KEYWORDS)
    
    # Aciliyet
    urgency = any(k in msg_lower for k in ["acil", "hemen", "şimdi", "urgent", "ambulans"])
    
    # Odak noktaları (basit kelime çıkarımı)
    focus = [k for k in HIGH_KEYWORDS if k in msg_lower][:3]
    
    return {
        "category": category,
        "complexity": "high" if is_high else "low",
        "urgency": urgency,
        "focus_points": focus
    }


def get_dynamic_system_prompt(user_context: Union[Dict, str], intent_dict: Union[Dict[str, Any], str]) -> str:
    # 1. Kullanıcı profili
    if isinstance(user_context, dict):
        user_info = f"Araç: {user_context.get('fuel_type', '?')} | Ev: {user_context.get('home_location', '?')} | Takım: {user_context.get('team', '?')}"
    else:
        user_info = str(user_context)

    # 2. Intent parsing
    if isinstance(intent_dict, dict):
        category = intent_dict.get("category", "general")
        focus_points = intent_dict.get("focus_points", [])
        urgency = intent_dict.get("urgency", False)
    else:
        category = str(intent_dict)
        focus_points = []
        urgency = False

    focus_str = ", ".join(focus_points) if focus_points else "Genel"

    # 3. Kategori bazlı KISA talimatlar
    instructions = _get_category_instructions(category)

    # 4. Aciliyet notu
    urgency_note = "\n⚠️ ACİL DURUM: Kısa, net, aksiyon odaklı yanıt ver!" if urgency else ""

    # 5. Rota geçmişi
    route_history_str = ""
    route_history = intent_dict.get("route_history", []) if isinstance(intent_dict, dict) else []
    if route_history:
        lines = [f"  {r['origin']}→{r['destination']} ({r.get('distance_km','?')}km)" for r in route_history[:3]]
        route_history_str = f"\nSON ROTALAR: {'; '.join(lines)}"

    return f"""{BASE_SYSTEM_PROMPT}
👤 KULLANICI: {user_info}{route_history_str}
🎯 GÖREV: {category.upper()} | Odak: {focus_str}{urgency_note}
📋 TALİMAT: {instructions}"""


def _get_category_instructions(category: str) -> str:
    if category == "fuel":
        return (
            "Yakıt optimizasyonu istiyorsa `evaluate_route_strategy` makro aracını kullan (tek çağrı). "
            "Sadece fiyat soruyorsa `get_fuel_prices` çağır. "
            "Rotanın ters yönündeki ilçeleri önerme. Tasarruf odaklı tavsiye ver."
        )
    elif category == "pharmacy":
        return (
            "get_pharmacies çağır. En yakın nöbetçiyi başa yaz, telefonu kalın ver. 'Geçmiş olsun' de."
        )
    elif category == "event":
        return (
            "get_events veya get_sports_matches çağır. Hafızadaki ESKİ verileri KULLANMA, sadece araç verisi."
        )
    elif category == "routing":
        return (
            "Yakıt optimizasyonu da varsa `evaluate_route_strategy` kullan. "
            "Yoksa `get_route_data` çağır. origin/destination'a YALIN İSİM yaz (ek yok). "
            "Kayıtlı konum varsa direkt isim yaz. 'CURRENT_LOCATION' desteklenir. "
            "Süre >2.5 saat ise mola öner. Rota sonrası: traffic, radar, toll, weather shield çağır. "
            "Son olarak `build_route_summary` ile özet kart sun."
        )
    elif category == "city_data":
        return (
            "list_ibb_datasets ile preset kontrol et, sonra fetch_ibb_dataset veya fetch_wfs_layer kullan."
        )
    else:
        return "Yardımsever asistan ol. Yer/fiyat/durum soruluyorsa MUTLAKA araç kullan, tahmin etme."