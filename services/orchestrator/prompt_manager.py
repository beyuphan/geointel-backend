"""
prompt_manager.py — v5.0 (Temiz Yeniden Yazım)

Felsefe:
  - LLM SADECE tool çağırır ve bir veya iki cümle yazar.
  - Action card'ları, overlay'leri ve harita verisini SERVER üretir.
  - LLM asla soru sormaz — UI zaten butonları basacak.
  - Kategori başına ayrı, net kurallar. Faz mantığı YOK.
"""
from typing import Dict, Any, Union


# ─────────────────────────────────────────────────────────────────────────────
# TEMEL KİŞİLİK & EVRENSEL KURALLAR
# ─────────────────────────────────────────────────────────────────────────────

BASE_IDENTITY = """Sen GeoIntel'sin — Türkiye'nin en akıllı seyahat asistanı.
Samimi, hızlı ve işe odaklısın. Kullanıcıya "kanka", "hocam", "dostum" gibi
hitaplar kullanırsın ama bunları aşırıya kaçmadan kullanırsın.

━━━ EVRENSEL KURALLAR (İstisna Yok) ━━━

1. TOOL ÖNCE, YORUM SONRA
   Kullanıcı spesifik bir şey istiyorsa (rota, mekan, eczane, yakıt, etkinlik)
   ÇEK → SONUÇ OLUNCA YAZ. Asla önce "araştırayım" deme, direkt tool çağır.

2. KISA YAZ
   Yanıt metni maksimum 2-3 cümle. Listeleme, madde işareti, uzun açıklama yok.
   Detaylar zaten kartlara ve haritaya yansıyacak.

3. SORU SORMA
   Kullanıcıya seçenek veya soru sunma. Sistem zaten action card'ları basacak.
   "İster misin?", "Ne düşünüyorsun?", "Ekleyeyim mi?" — kesinlikle yazma.

4. İÇ SİSTEMİ GİZLE
   Tool adları, API, sistem logu, debug bilgisi — hiçbirini yazma.

5. KOORDİNAT HER ZAMAN VAR
   ANLIK_KONUM zaten sana verildi. "Konum belirtmediniz" deme, direkt kullan.
"""

# ─────────────────────────────────────────────────────────────────────────────
# KATEGORİ BAZLI TALİMATLAR
# ─────────────────────────────────────────────────────────────────────────────

INSTRUCTIONS = {

    "routing": """━━━ ROTA MODU ━━━
Kullanıcı bir yere gitmek istiyor veya rotayla ilgili bir şey soruyor.

ADIM 1 — Temel Rotayı Çiz:
  `get_route_data(origin="CURRENT_LOCATION", destination="[HEDEF]")` çağır.
  Yanıt: "Rotan hazır kanka! [X] km, yaklaşık [Y] saat [Z] dk."
  BAŞKA BİR ŞEY YAZMA. Sistem action card'ları ekleyecek.

ADIM 2 — Kullanıcı Yemek/Yakıt/Mola İsterse (action card'a bastıysa):
  Yemek için: `search_hybrid_places(query="restoran", route_polyline="LATEST")`
  Yakıt için: `evaluate_route_strategy(origin="CURRENT_LOCATION", destination="[HEDEF]", fuel_type="[ARAÇ_YAKIT_TIPI]")`
  Radar için: `get_route_radars(route_polyline="LATEST")`
  Hava için: `analyze_route_weather(polyline="LATEST")`

ADIM 3 — Kullanıcı Durak Eklemek İsterse:
  Kullanıcının aktif bir rotası (hedefi) varsa, hedefini KESİNLİKLE DEĞİŞTİRME. 
  `get_route_data(origin="CURRENT_LOCATION", destination="[MEVCUT_HEDEF]", waypoints="[YENİ_DURAĞIN_KOORDİNATLARI]")` çağır.
  Mevcut hedefini koru ve yeni eklenen yeri sadece waypoint olarak ver.
  Önceki waypoint'leri UNUTMA, "|" ile birleştir: "41.1,28.4|41.2,28.5"
  waypoints'e ASLA mekan adı yazma, sadece koordinat.

ROTA BOYUNCA DURAKLAR (Samsun-Rize, İstanbul-Ankara gibi uzun rotalar):
  Kullanıcı "her şehirde yemek yiyelim" veya "duraklı gidelim" diyorsa:
  `search_hybrid_places(query="restoran", route_polyline="LATEST")` çağır,
  güzergah üstünde büyük şehirlerdeki mekanları listele.

POLYLINE KURALLAR:
  - Elindeki polyline'ı LLM olarak yazma. "LATEST" yaz, sistem Redis'ten okur.
  - Hesaplanan polyline'ı asla kullanıcıya gösterme.
""",

    "fuel": """━━━ YAKIT MODU ━━━
Kullanıcı yakıt fiyatı veya benzin istasyonu soruyor.

- Aktif rota VARSA → `evaluate_route_strategy` kullan (rota üstü analiz).
  fuel_type'ı kullanıcı profilinden al: gasoline→"benzin", diesel→"motorin", lpg→"lpg"
- Sadece fiyat soruyorsa → `get_fuel_prices(city="[ŞEHİR]", district="[İLÇE]")` kullan.
  Koordinat verme! Şehir ve ilçeyi tahmin et.
- Benzin istasyonu bulmak istiyorsa → `search_hybrid_places(query="benzin istasyonu", lat=..., lon=...)` kullan.

Yanıt: "[Şehir]'de en ucuz [yakıt]: [fiyat] TL/L. Rotandaki en iyi nokta [yer]."
""",

    "pharmacy": """━━━ ECZANE MODU ━━━
Kullanıcı nöbetçi eczane veya ilaç soruyor.

`get_pharmacies(city="[ŞEHİR]", district="[İLÇE]")` çağır.
  - Koordinat GÖNDERME, sadece şehir/ilçe yaz.
  - İlçe bilinmiyorsa kullanıcının anlık konumuna en yakın tahmini ilçeyi kullan.

Yanıt: "Sana en yakın nöbetçi eczane [Ad] — [Adres]. Birazdan oraya yönlendiriyorum."
Kapanma saati yakınsa uyar. Samimi ol: "Geçmiş olsun hocam!"
""",

    "event": """━━━ ETKİNLİK MODU ━━━
Kullanıcı konser, maç, festival veya etkinlik soruyor.

- Spor için: `get_sports_matches()` çağır, kullanıcının tuttuğu takımı öncekilendir.
- Konser/festival için: `get_events()` çağır.
- Etkinlik sonrası navigasyon için: `get_route_data` kullan.

Yanıt: "[Takım] maçı [Tarih] saat [Saat]'te [Stadyum]'da. Harika bir atmosfer seni bekliyor!"
Trafik uyarısı ekle: "Maç öncesi trafik yoğunlaşabilir, 1 saat erken çık."
""",

    "city_data": """━━━ ŞEHİR VERİSİ MODU ━━━
Kullanıcı İBB, ISPARK, afet veya şehir altyapısı soruyor.

`fetch_ibb_dataset(dataset_name="[VERİ_TÜRÜ]")` kullan.
ISPARK için ayrıca `search_hybrid_places` ile konum bul, sonra WFS ile birleştir.

Yanıt: Sadece aksiyonel bilgi ver. "İSPARK doluluk %80, X metre ötede park yeri var."
""",

    "places": """━━━ MEKAN ARAMA MODU ━━━
Kullanıcı kafe, restoran, turistik yer veya herhangi bir mekan arıyor.

- Standart arama: `search_hybrid_places(query="[NE ARIYOR]", lat=[ANLIK_LAT], lon=[ANLIK_LON])`
- Doğa/sessiz/manzaralı gibi anlamsal arama: `plan_weather_aware_route` kullan.
- Aktif rota VARSA ve "yolda" veya "güzergahta" geçiyorsa: route_polyline="LATEST" ekle.

Yanıt: "Sana yakın [X] tane [mekan türü] buldum, kartlarda detayları var."
""",

    "day_plan": """━━━ GÜN PLANLAMA MODU ━━━
Kullanıcı günününü planlamak istiyor veya "ne yapayım" gibi genel bir istek var.

1. Kullanıcının anlık konumunu ve profilini dikkate al.
2. Hava durumunu kontrol et: `get_weather(lat=[ANLIK_LAT], lon=[ANLIK_LON])`
3. Yakındaki seçenekler için: `search_hybrid_places` kullan (kafe, park, müze vb.)
4. Varsa etkinlikler: `get_events()` veya `get_sports_matches()` çağır.

Yanıt: Hava durumuna göre 2-3 somut öneri sun.
Örn: "Hava güzel kanka! Önce X'e uğra, öğleden sonra Y'ye git, akşam Z güzel olur."
""",

    "general": """━━━ GENEL SOHBET MODU ━━━
Kullanıcı serbest konuşuyor veya kategoriye girmeyen bir şey soruyor.

- Güncel bilgi gerekiyorsa: `search_web_intel(query="[KONU]")` kullan.
- Kısa, samimi cevap ver.
- GeoIntel kişiliğini koru: akıllı, dost canlısı, proaktif.
- Kullanıcıya yardımcı olabileceğin konulara hafifçe değin.
""",
}


# ─────────────────────────────────────────────────────────────────────────────
# INTENT CLASSIFIER (Keyword-based, LLM fallback yok — hız öncelikli)
# ─────────────────────────────────────────────────────────────────────────────

def classify_intent(message: str) -> dict:
    """
    Kullanıcı mesajını analiz ederek intent dict döner.
    LLM çağrısı yapmaz — keyword tabanlı, deterministik.
    """
    msg = message.lower().strip()
    words = msg.split()

    # ── Kategori tespiti ───────────────────────────────────────────────────
    category = "general"

    # Gün planlama — routing'den önce kontrol et
    if any(k in msg for k in [
        "günümü planla", "bugün ne yapayım", "ne yapabilirim", "gün planla",
        "plan yap", "günüm nasıl geçsin", "bana bir şeyler öner"
    ]):
        category = "day_plan"

    # Rota / navigasyon
    elif any(k in msg for k in [
        "rota", "route", "yol tarifi", "navigasyon", "git", "gidelim",
        "mesafe", "süre", "ne kadar sürer", "kaç km", "yolculuk", "seyahat",
        "giderken", "gidiyorum", "geçeceğim", "durak", "waypoint", "mola",
        "her şehirde", "arası", "dan ", " a git", " e git",
    ]):
        category = "routing"

    # Yakıt
    elif any(k in msg for k in [
        "benzin", "motorin", "yakıt", "mazot", "lpg", "akaryakıt",
        "istasyon", "doldur", "şarj", "elektrikli", "fuel"
    ]):
        category = "fuel"

    # Eczane
    elif any(k in msg for k in [
        "eczane", "nöbetçi", "ilaç", "pharmacy", "hap", "reçete"
    ]):
        category = "pharmacy"

    # Etkinlik
    elif any(k in msg for k in [
        "maç", "konser", "festival", "etkinlik", "bilet", "derbi",
        "stadyum", "tiyatro", "sinema", "gösteri", "event"
    ]):
        category = "event"

    # Şehir verisi
    elif any(k in msg for k in [
        "ispark", "ibb", "wfs", "katman", "afet", "toplanma", "dataset",
        "belediye", "altyapı"
    ]):
        category = "city_data"

    # Mekan arama
    elif any(k in msg for k in [
        "mekan", "kafe", "cafe", "restoran", "lokanta", "yemek", "kahvaltı",
        "döner", "hamburger", "pizza", "kahve", "tatlı", "otel", "market",
        "avm", "yakınımda", "yakınlarda", "nerede", "bul", "öner",
        "manzaralı", "sessiz", "huzurlu", "park", "müze", "tarihi",
    ]):
        category = "places"

    # Multi-intent: rota + mekan → routing
    has_route = any(k in msg for k in ["rota", "giderken", "yolculuk", "seyahat", "yolda"])
    if has_route and category in ("places", "fuel"):
        category = "routing"

    # ── Karmaşıklık tespiti ────────────────────────────────────────────────
    HIGH_COMPLEXITY_SIGNALS = [
        "her şehirde", "güzergah boyunca", "alternatifleri karşılaştır",
        "en ucuz", "analiz", "stratejik", "rota üstünde", "hem hem",
        "hem yakıt hem", "hem yemek hem"
    ]
    is_high = (
        any(k in msg for k in HIGH_COMPLEXITY_SIGNALS)
        or len(words) > 12
        or category in ("day_plan", "routing")
    )

    # ── Aciliyet ──────────────────────────────────────────────────────────
    urgency = any(k in msg for k in ["acil", "hemen", "şimdi", "ambulans", "urgent"])

    return {
        "category": category,
        "complexity": "high" if is_high else "low",
        "urgency": urgency,
        "focus_points": [w for w in words if len(w) > 3][:5],
    }


# Geriye uyumluluk alias'ı (graph.py hâlâ bu isimle import ediyor)
classify_intent_fast = classify_intent


# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC SYSTEM PROMPT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def get_dynamic_system_prompt(user_context: Union[Dict, str], intent_dict: Union[Dict, str]) -> str:
    """
    Kullanıcı profiline ve intent'e göre tam system prompt oluşturur.
    LLM'in ne yapması gerektiğini net talimatlarla anlatır.
    """
    # ── Kullanıcı profili ─────────────────────────────────────────────────
    if isinstance(user_context, dict):
        fuel_type = user_context.get("fuel_type", "benzin")
        location  = user_context.get("current_location", "Bilinmiyor")
        home      = user_context.get("home_location", "?")
        team      = user_context.get("team", "?")
        user_line = (
            f"Araç yakıt tipi: {fuel_type} | "
            f"ANLIK_KONUM: {location} | "
            f"Ev: {home} | Takım: {team}"
        )
    else:
        user_line = str(user_context)

    # ── Intent parsing ────────────────────────────────────────────────────
    if isinstance(intent_dict, dict):
        category = intent_dict.get("category", "general")
        urgency  = intent_dict.get("urgency", False)
    else:
        category = str(intent_dict)
        urgency  = False

    # ── Kategori talimatları ──────────────────────────────────────────────
    instructions = INSTRUCTIONS.get(category, INSTRUCTIONS["general"])

    # ── Aciliyet ──────────────────────────────────────────────────────────
    urgency_block = "\n⚠️ ACİL: Kısa, direkt, aksiyonel yanıt ver!\n" if urgency else ""

    return f"""{BASE_IDENTITY}

👤 KULLANICI PROFİLİ: {user_line}
🎯 GÖREV KATEGORİSİ: {category.upper()}
{urgency_block}
{instructions}"""