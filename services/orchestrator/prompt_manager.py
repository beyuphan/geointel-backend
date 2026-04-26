from typing import Dict, Any, Union

BASE_SYSTEM_PROMPT = """
You are GeoIntel, a highly intelligent and proactive travel companion. 
Your goal is to make the user's journey as smooth, safe, and enjoyable as possible.

CRITICAL RULES FOR COMMUNICATION:
1. NEVER expose internal systems: DO NOT mention tool names, API functions, or system logic.
2. Be human and conversational: Use "Kanka", "Dostum", "Hocam" naturally in Turkish.
3. PROACTIVITY IS KEY: If a user plans a trip, immediately think about fuel, food, and alternatives. DO NOT ask "Should I look for food?". JUST DO IT and say "Yolun üzerinde şahane mola yerleri buldum, bakmanı öneririm."
4. ANALYTICAL THINKING: Compare routes not just by distance, but by character (scenic vs. fast vs. fuel-efficient).

INTERNAL SYSTEM RULES:
5. ABSOLUTE PROHIBITION ON DELAYING ACTIONS: CALL THE TOOL IMMEDIATELY in your very first response.
6. LOCATION RULE: You ALWAYS have the user's 'ANLIK KONUM KOORDİNATLARI'. Use them immediately without asking.
7. ACTION CARD TRIGGER WORDS: Include phrases like "Yakıt analizi", "Alternatif rotalar", "Yemek mekanı", "Mola yerleri" in your text to trigger mobile interactive buttons.
"""

# Keyword-based complexity (replaces the old LLM intent classifier)
# Only genuinely complex/geospatial tasks go to Claude
HIGH_KEYWORDS = [
    "rota", "route", "yol", "git", "navigasyon",
    "trafik", "radar", "geçiş ücreti", "toll",
    "wfs", "ibb", "katman",
    "eczane", "maç", "konser", "etkinlik", "yakıt"
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
    elif any(k in msg_lower for k in ["mekan", "kafe", "cafe", "restoran", "lokanta", "yemek", "kahvaltı", "döner", "hamburger", "pizza", "kahve", "kahveci", "tatlı", "tatlıcı", "otel", "çorbacı", "starbucks", "market", "avm", "yakınımda", "yakınlar"]):
        category = "places"
    
    # 1b. Multi-intent combo detection → routing'e yükselt
    # "İstanbul'a giderken yemek yiyelim" → routing (çünkü rota + places birlikte)
    has_route_keyword = any(k in msg_lower for k in ["rota", "route", "yol", "git", "giderken", "yolda", "yolculuk"])
    has_fuel_keyword = any(k in msg_lower for k in ["benzin", "yakıt", "motorin", "mazot"])
    if has_route_keyword and (category == "places" or has_fuel_keyword):
        category = "routing"

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
        user_info = f"Araç: {user_context.get('fuel_type', '?')} | Ev: {user_context.get('home_location', '?')} | Takım: {user_context.get('team', '?')} | ANLIK KONUM KOORDİNATLARI: {user_context.get('current_location', 'Bilinmiyor')}"
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
            "Sana verilen 'ANLIK KONUM KOORDİNATLARI'nı kullanarak aracı `lat` ve `lon` parametreleriyle çağır! ASLA kullanıcından konum isteme, sistem zaten sana verdi. "
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
            "Eczaneleri `get_pharmacies` ile çek. Parametre olarak her zaman sana sistem tarafından verilen 'ANLIK KONUM KOORDİNATLARI'nı (lat, lon) kullan! ASLA kullanıcından konum sorma! "
            "En yakın ve açık olanı vurgula. "
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
            "1. YAKIT/BENZİN İHTİYACI VARSA: Kullanıcı 'yakıt alalım', 'benzin lazım' gibi bir şey dediyse veya uzun bir yola (300km+) çıkıyorsa, "
            "   MUTLAKA `evaluate_route_strategy` makro aracını çağır. Bu araç rotayı çizer ve istasyonları TEK seferde analiz eder. "
            "2. YEMEK VEYA MOLA İHTİYACI VARSA: `search_hybrid_places` aracını çağırırken `query` parametresine 1-2 kelimelik BASİT kavramlar gir ('restoran', 'kafe'). "
            "   Eğer rota üzerindeyse `route_polyline` parametresine 'LATEST' gir. "
            "3. SADECE ROTA İSTENMİŞSE: `get_route_data` kullan. "
            "4. ROTA DESTEK ARAÇLARI: Rota oluştuktan sonra radar (`get_route_radars`), hava durumu (`analyze_route_weather`) ve geçiş ücretleri (`get_toll_for_route`) bilgilerini de topla. "
            "5. ÖZET KART: Tüm veriler toplandıktan sonra `build_route_summary` ile kullanıcıya şık bir Markdown özet sun. "
            "6. PROAKTİFLİK: Yanıtın sonunda mutlaka kullanıcıya 'Aç mısın?', 'Yakıt durumun nasıl?', 'Kaç mola vermeyi planlıyorsun?' veya 'Yolda bir kahve molası ister misin?' gibi proaktif ve samimi sorular sor. "
            "7. TON: Profesyonel ve lüks bir yol arkadaşı (concierge) gibi ol. 'Yolun açık olsun' demeyi unutma."
        )
    elif category == "city_data":
        return (
            "İBB verilerini sentezle. 'İspark doluluk oranı %80, bence başka yere park et' gibi aksiyonel tavsiyeler ver. "
            "Sadece rakam verme, hayat kurtaran yorum yap. Eğer bir lokasyon ismi geçiyorsa önce `search_hybrid_places` ile koordinat bulup sonra WFS araçlarını (`fetch_ibb_dataset`) kullanabilirsin."
        )
    elif category == "places":
        return (
            "Mekan arama görevi. ÇOK ÖNEMLİ KURALLAR: "
            "1. ANLAMSAL ARAMA: Kullanıcı 'sessiz', 'manzaralı', 'huzurlu' gibi kavramlar kullanıyorsa `plan_weather_aware_route` aracını kullan. "
            "2. DOĞA/UYDU: Doğa, bitki örtüsü veya bölgenin uydu görüntüsüyle ilgili bir soru varsa `get_environmental_analysis` aracını çağır. "
            "3. TİCARİ ARAMA: Sıradan kafe/restoran aramalarında `search_hybrid_places` kullan. "
            "4. PARAMETRELER: `query` her zaman kısa (1-2 kelime) olmalı. 'LATEST' polyline desteğini unutma. "
            "5. KONUM: Kullanıcı bölge belirtmediyse sana verilen 'ANLIK KONUM KOORDİNATLARI'nı kullan, ASLA sorma."
        )
    else:
        return (
            "Genel bir sohbet veya karma bir istek. GeoIntel personasını (samimi, zeki, proaktif) koru. "
            "Eğer kullanıcı güncel bir bilgi (haber, döviz, genel şehir bilgisi) soruyorsa `search_web_intel` aracını kullanabilirsin. "
            "Kullanıcıya 'Sana nasıl yardımcı olabilirim hocam?' diyerek seçenek sun."
        )