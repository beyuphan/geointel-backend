from typing import Dict, Any, Union

BASE_SYSTEM_PROMPT = """
You are GeoIntel, an intelligent location-aware travel assistant.

Rules:
1. Think analytically: connect data before presenting (e.g., "Take this route — less traffic and fuel station on the way").
2. Be precise: always call a tool for data. If not found, say so clearly.
3. For complex route + fuel requests, use evaluate_route_strategy macro tool — it solves route, stations and prices in one call.
4. For repeat route context, use 'LATEST' as polyline.
5. Never hallucinate coordinates — always resolve via tools.
"""

# Keyword-based complexity (replaces the old LLM intent classifier)
# Only genuinely complex/geospatial tasks go to Claude
HIGH_KEYWORDS = [
    "rota", "route", "yol", "git", "navigasyon",
    "trafik", "radar", "geçiş ücreti", "toll",
    "wfs", "ibb", "katman"
]

def classify_intent_fast(message: str) -> dict:
    """LLM çağrısı yapmadan, keyword regex ile intent belirle. 
    Eğer cümle karmaşıksa 'high' döner ve LLM Router'ı tetikler."""
    msg_lower = message.lower()
    words = message.split()
    
    # 1. Kategori tespiti (Regex/Keyword)
    category = "general"
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
    
    # 2. Karmaşıklık (Hibrit Karar)
    # Kural A: Özel anahtar kelimeler
    is_high = any(k in msg_lower for k in HIGH_KEYWORDS)
    # Kural B: Cümle uzunluğu (Kullanıcının önerisi: >10 kelime zeka gerektirir)
    if len(words) > 10:
        is_high = True
    
    # 3. Aciliyet
    urgency = any(k in msg_lower for k in ["acil", "hemen", "şimdi", "urgent", "ambulans"])
    
    # 4. Odak noktaları
    focus = [k for k in HIGH_KEYWORDS if k in msg_lower][:3]
    
    return {
        "category": category,
        "complexity": "high" if is_high else "low",
        "urgency": urgency,
        "focus_points": focus,
        "needs_deep_analysis": is_high and len(words) > 8 # Ekstra zeka katmanı için ipucu
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

    focus_str = ", ".join(focus_points) if focus_points else "General"

    # 3. Category-specific instructions
    instructions = _get_category_instructions(category)

    # 4. Urgency note
    urgency_note = "\n⚠️ URGENT: Short, direct, action-focused response!" if urgency else ""

    # 5. Route history — only inject for routing category (saves tokens for other categories)
    route_history_str = ""
    if category == "routing" and isinstance(intent_dict, dict):
        route_history = intent_dict.get("route_history", [])
        if route_history:
            lines = [f"  {r['origin']}→{r['destination']} ({r.get('distance_km','?')}km)" for r in route_history[:3]]
            route_history_str = f"\nRECENT ROUTES: {'; '.join(lines)}"

    return f"""{BASE_SYSTEM_PROMPT}
👤 USER: {user_info}{route_history_str}
🎯 TASK: {category.upper()} | Focus: {focus_str}{urgency_note}
📋 INSTRUCTIONS: {instructions}"""


def _get_category_instructions(category: str) -> str:
    if category == "fuel":
        return (
            "Kullanıcının aracına uygun yakıt tipini (Dizelse Motorin, Benzinse Benzin) profilinden kontrol et. "
            "Kullanıcı hem 'rota bul' hem de 'yakıt/benzin' istiyorsa MUTLAKA `evaluate_route_strategy` makro aracını kullan — "
            "bu araç rotayı çizer, istasyonları bulur ve yakıt fiyatlarını TEK seferde analiz eder. "
            "Kullanıcı 'yap', 'tamam', 'devam et' gibi bir onay verirse veya önceki mesajda benzin+rota birlikte istenmişse, "
            "hemen `evaluate_route_strategy` aracını çağır, izin isteme. "
            "Sadece benzin istasyonu soru yorsa `search_hybrid_places` ile bul ve HEMEN fiyat kıyasla — "
            "'Fiyat kıyaslaması yapmamı ister misin?' diye SORMA. Direkt yap! "
            "En ucuzu: 'Şu istasyon rota üzerinde ve km başına X TL tutuyor' biçiminde sun. "
            "Tasarruf ve verimlilik odaklı konuş."
        )
    elif category == "pharmacy":
        return (
            "Eczaneleri `get_pharmacies` ile çek. En yakın ve açık olanı vurgula. "
            "Adresi LLM olarak sen güzelleştir: 'Hemen Beşiktaş Meydanı'nın arkasında kalıyor' gibi. "
            "Kapanma saati yaklaşıyorsa uyar. 'Çok geçmiş olsun kanki' diyerek kapat."
        )
    elif category == "event":
        return (
            "Playwright ile güncel çekilen `get_events` veya `get_sports_matches` sonuçlarını analiz et. "
            "Kullanıcının tuttuğu takımın (varsa) maçlarını önceliklendir. "
            "Etkinlik saati trafik yoğunluğu uyarısını yap. 'İnanılmaz bir atmosfer seni bekliyor' gibi samimi yorumlar ekle."
        )
    elif category == "routing":
        return (
            "Bu en karmaşık görev. Adım adım düşün (Chain-of-Thought): "
            "1. `get_route_data` ile ana rotayı ve trafik durumunu anla. "
            "2. Eğer yol üstü istek de varsa (yemek, yakıt, kafe vb.), `search_hybrid_places` veya `search_places_google` kullan. "
            "3. Yol üstündeki önemli 'warnings' (yol çalışması, kaza) varsa kullanıcıya duyur. "
            "4. Eğer kullanıcı yakıt + rota birlikte istiyorsa `evaluate_route_strategy` kullan. "
            "5. Rota özeti sunarken `build_route_summary` kullan. "
            "6. Radar/kamera sorularında `get_route_radars` çağır. "
            "7. 'Yolun yarısında yağmur başlayabilir, dikkat et reis' gibi mikro-detaylar ver."
        )
    elif category == "city_data":
        return (
            "İBB verilerini sentezle. 'İspark doluluk oranı %80, bence başka yere park et' gibi aksiyonel tavsiyeler ver. "
            "Sadece rakam verme, hayat kurtaran yorum yap."
        )
    else:
        return (
            "Genel bir sohbet veya karma bir istek. GeoIntel personasını (samimi, zeki, proaktif) koru. "
            "Kullanıcıya 'Sana nasıl yardımcı olabilirim hocam?' diyerek seçenek sun."
        )