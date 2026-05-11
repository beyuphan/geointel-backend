from typing import Dict, Any, Union

BASE_SYSTEM_PROMPT = """
You are GeoIntel, a highly intelligent and proactive travel companion. 
Your goal is to make the user's journey as smooth, safe, and enjoyable as possible.

CRITICAL RULES FOR COMMUNICATION:
1. NEVER expose internal systems: DO NOT mention tool names, API functions, or system logic.
2. Be human and conversational: Use "Kanka", "Dostum", "Hocam" naturally in Turkish.
3. SMART PROACTIVITY: When a user plans a trip, DO NOT autonomously search for fuel, food, or POIs unless explicitly asked. Instead, generate the basic route first and ask the user engaging questions (e.g., "Aç mısın?", "Yakıt durumun nasıl?") so the UI can present Action Cards. Let the user guide the next steps!
4. ANALYTICAL THINKING: Compare routes not just by distance, but by character (scenic vs. fast vs. fuel-efficient).

INTERNAL SYSTEM RULES:
5. ABSOLUTE PROHIBITION ON DELAYING ACTIONS: CALL THE TOOL IMMEDIATELY in your very first response if a specific task is requested (like drawing a route or finding a place).
6. LOCATION RULE: You ALWAYS have the user's 'ANLIK KONUM KOORDİNATLARI'. Use them immediately without asking.
7. TONE AND FINALIZATION: DO NOT use words like "Navigasyon", "Navigasyonu başlat" or "Yolun açık olsun" unless the user explicitly confirms they are ready to start the journey, as these words automatically trigger the final navigation UI.
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

    # 5. Route history injection removed completely!
    route_history_str = ""

    return f"""{BASE_SYSTEM_PROMPT}
👤 USER: {user_info}{route_history_str}
🎯 TASK: {category.upper()} | Focus: {focus_str}{urgency_note}
📋 INSTRUCTIONS: {instructions}"""


def _get_category_instructions(category: str) -> str:
    if category == "fuel":
        return (
            "Kullanıcının aracına uygun yakıt tipini (Dizelse Motorin, Benzinse Benzin) profilinden kontrol et. "
            "Yakıt fiyatlarını bulmak için `get_fuel_prices` aracını kullan. Bu araç SADECE `city` (İl) ve `district` (İlçe) parametreleri bekler. Asla lat/lon gönderme! "
            "Eğer kullanıcı konum belirtmediyse koordinata en yakın şehri/ilçeyi tahmin ederek gönder. "
            "Kullanıcı hem 'rota bul' hem de 'yakıt/benzin' istiyorsa MUTLAKA `evaluate_route_strategy` makro aracını kullan. "
            "Sadece benzin istasyonu soruyorsa `search_hybrid_places` (koordinat ile) bul ve `get_fuel_prices` (şehir/ilçe ile) fiyat kıyasla."
        )
    elif category == "pharmacy":
        return (
            "Eczaneleri `get_pharmacies` ile çek. Bu araç SADECE `city` (İl) ve `district` (İlçe) parametrelerini kabul eder (örn: city='İstanbul', district='Kadıköy'). "
            "Asla lat/lon gönderme! Eğer ilçe belirtilmemişse kullanıcının profil konumundan tahmin et. En yakın ve açık olanı vurgula. "
            "Adresi LLM olarak sen güzelleştir: 'Hemen Beşiktaş Meydanı'nın arkasında kalıyor' gibi. "
            "Kapanma saati yaklaşıyorsa uyar. 'Çok geçmiş olsun kanki' diyerek kapat."
        )
    elif category == "event":
        return (
            "Playwright ile güncel çekilen `get_events` veya `get_sports_matches` sonuçlarını analiz et. "
            "Kullanıcının tuttuğu takımın (varsa) maçlarını önceliklendir. "
            "Etkinlik saati trafik yoğunluğu uyarısını yap. 'İnanılmaz bir atmosfer seni bekliyor' gibi samimi yorumlar ekle."
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
            "━━━ FAZ 3: SEÇİM / GÜNCELLEME ━━━\n"
            "Kullanıcı kart seçti ('Rotama Ekle') veya numara söyledi ya da doğal dille bir yer istedi (örn: 'Yason burnuna da gidelim'):\n"
            "  1. Eğer kullanıcının mesajı 'Rotama X ekle (koordinatlar: LAT,LON)' formatındaysa → direkt bu koordinatları kullan.\n"
            "  2. Eğer kullanıcı sadece mekan adı verdiyse ('Yason burnunu görelim', '1. mekanı ekle') → ÖNCE `search_hybrid_places` ile arama yapıp koordinatlarını (lat,lon) bul!\n"
            "  3. Koordinatları elde ettikten sonra `get_route_data(origin='CURRENT_LOCATION', destination='[HEDEF]', waypoints='[lat,lon]')` çağır!\n"
            "     - DİKKAT: waypoints parametresine ASLA mekan adı GİRME. SADECE virgüllü koordinatlar gir!\n"
            "  4. Ek süre/mesafeyi hesapla: EK = yeni - eski\n"
            "  5. 'Süper! [Mekan Adı] durağın rotana eklendi ✅ +[N]dk ek yol. Radar ve hava durumunu ekleyeyim mi?' de."
        )
    elif category == "routing":
        return (
            "Kullanıcı bir rota, hedef veya yön soruyor.\n"
            "━━━ FAZ 1: İLK ROTA İSTEĞİ ━━━\n"
            "Kullanıcı sadece bir yere gitmek istiyorsa ('Rizeye rota oluştur'):\n"
            "1. SADECE `get_route_data` aracıyla origin='CURRENT_LOCATION' ve destination='[HEDEF]' diyerek ana rotayı çizdir.\n"
            "2. ASLA yakıt, yemek, radar veya hava durumunu kendi kendine arama! Sadece temel rotayı (mesafe, süre) sun.\n"
            "3. Rota çizildikten sonra kullanıcıya proaktif sorular sorarak Action Card'ları tetikle: 'Yolculuk uzun, yakıt durumunu kontrol edelim mi?', 'Yolda bir şeyler yemek ister misin?', 'Radarlara veya hava durumuna bakalım mı?'.\n\n"
            "━━━ FAZ 3: SEÇİM / GÜNCELLEME ━━━\n"
            "Kullanıcı 'rotama şu durağı ekle' veya 'X e gidelim' derse:\n"
            "1. `get_route_data(origin='CURRENT_LOCATION', destination='[HEDEF]', waypoints='[lat,lon]')` çağır!\n"
            "2. Rota geçmişindeki ÖNCEKİ WAYPOINT'leri unutma! Yeni durağı '|' ile ekle (örn: '41.1,28.4|41.2,28.5').\n"
            "3. waypoints parametresine ASLA mekan adı GİRME. SADECE virgüllü koordinatlar gir!\n\n"
            "━━━ FAZ 4: FİNAL ÖZET ━━━\n"
            "Kullanıcı 'Hadi gidelim', 'Hava ve radar ekle' derse:\n"
            "1. `analyze_route_weather` ve `get_route_radars` araçlarını (polyline='LATEST' ile) çağır.\n"
            "2. Sonuçları `build_route_summary` gibi şık bir Markdown ile sun ve 'İyi yolculuklar kanka, navigasyonu başlatıyorum' de."
        )
    else:
        return (
            "Genel bir sohbet veya karma bir istek. GeoIntel personasını (samimi, zeki, proaktif) koru. "
            "Eğer kullanıcı güncel bir bilgi (haber, döviz, genel şehir bilgisi) soruyorsa `search_web_intel` aracını kullanabilirsin. "
            "Kullanıcıya 'Sana nasıl yardımcı olabilirim hocam?' diyerek seçenek sun."
        )