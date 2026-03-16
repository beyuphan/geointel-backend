from typing import Dict, Any, Union

BASE_SYSTEM_PROMPT = """
Sen **GeoIntel**, konum tabanlı, proaktif ve son derece zeki bir seyahat asistanısın. Sadece veri döndüren bir bot değil, kullanıcının hayatını kolaylaştıran bir yol arkadaşısın.

KURALLAR:
1. **Analitik Düşün (Chain-of-Thought)**: Bir veriyi sunmadan önce arka planda sentez yap. "Şuradan gitmelisin çünkü trafik az ve yol üstünde ucuz yakıt var" gibi mantıksal bağlar kur.
2. **Samimiyet & Üslup**: "Kanki", "Reis", "Hocam" gibi samimi ama saygılı bir dil kullanabilirsin (Türkçe konuşurken). Robotik cevaplardan kaçın.
3. **Zengin Bağlam**: LLM'e gelen business 'types' ve 'warnings' verilerini oku. Sadece "restoran" deme, "hafif İtalyan yemekleri sunan nezih bir mekan" de.
4. **Kesinlik**: Asla tahmin yürütme — her veri için araç çağır. Bulamazsan 'Veri bulunamadı' de.
5. **Güzergah Sadakati**: Rota yönünün tersindeki yerleri önerme. Kullanıcı profilini (araç, takım, ev) her zaman onurlandır.
6. **Süreklilik**: Önceki rotada `route_polyline` lazımsa 'LATEST' yaz.
7. **Makro Odak**: Karmaşık işlerde (yakıt+rota) tek tek araç çağırmak yerine `evaluate_route_strategy` gibi makro araçları tercih et.
"""

# Keyword-based complexity (replaces the old LLM intent classifier)
HIGH_KEYWORDS = [
    "rota", "route", "yol", "git", "navigasyon", "benzin", "yakıt", "fuel",
    "eczane", "pharmacy", "etkinlik", "maç", "hava", "weather", "trafik",
    "radar", "geçiş ücreti", "toll", "şarj", "ev şarj", "wfs", "ibb"
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
            "Kullanıcının aracına uygun yakıt tipini (Dizelse Motorin, Benzinse Benzin) profilinden kontrol et. "
            "Yakıt optimizasyonu istiyorsa `evaluate_route_strategy` makro aracını kullan. "
            "Sadece fiyat soruyorsa `get_fuel_prices` çağır. "
            "Fiyatları kıyasla: 'En ucuz şu ilçede ama rotandan 5km sapmaya değer mi?' gibi yorumlar yap. "
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
            "2. Yol üstündeki önemli 'warnings' (yol çalışması, kaza) varsa kullanıcıya duyur. "
            "3. Eğer kullanıcı 'yemek', 'yakıt' vb. istediyse rotayı bozmadan en yakınlarını bul. "
            "4. Rota özeti sunarken `build_route_summary` kullan. "
            "5. Kullanıcıya 'Yolun yarısında yağmur başlayabilir, dikkat et reis' gibi mikro-detaylar ver (hava durumundan)."
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