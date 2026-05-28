"""
trip_strategist.py — LLM Strateji-First Plan v2

Tek LLM çağrısı:
  - LLM Türkiye coğrafyasını + yöresel lezzetleri biliyor
  - Origin-destination'a göre rota üzerindeki ilçeleri tahmin eder
  - Kullanıcı tercihlerine göre 4-6 STOP üretir (fuel/food/break/scenic)
  - Aynı çağrıda 300-500 kelime imperative narrative yazar (placeholder'lı)

Backend sonra: targeted search ile her stop'u gerçek mekana eşler, narrative
içindeki {{STOP_N}} placeholder'larını gerçek isimlerle replace eder.

Bu modül Curator'ı tamamen değiştirir — daha hızlı (tek çağrı), daha akıllı
(LLM coğrafya bilgisi), daha tutarlı (narrative + plan aynı LLM).
"""
from __future__ import annotations
import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from logger import log

STRATEGIST_TIMEOUT_S = float(os.getenv("TRIP_STRATEGIST_TIMEOUT_S", "30.0"))
STRATEGIST_MIN_NARRATIVE = int(os.getenv("TRIP_STRATEGIST_MIN_NARRATIVE", "100"))
STRATEGIST_DEBUG_DUMP = os.getenv("TRIP_STRATEGIST_DEBUG_DUMP", "true").lower() == "true"


_VALID_ROLES = {"fuel", "food", "break", "scenic"}
_VALID_ANCHORS = {"home_proximity", "start", "end"}


def _build_system_prompt() -> str:
    return """Sen GeoIntel Trip Stratejistsin. Türkiye coğrafyasını ve yöresel \
lezzetleri biliyorsun. Görevin tek bir JSON çıktı üretmek.

━━━ NE YAPACAKSIN ━━━

1. Origin → Destination rotasının geçtiği büyük şehir/ilçeleri tahmin et.
   Örnek: Samsun → İstanbul ⇒ Samsun, Amasya (Merzifon), Çorum, Çankırı, \
Bolu (Mengen/Gerede), Sakarya (Düzce/Hendek), Kocaeli (Kartepe), İstanbul.

2. Kullanıcı tercihlerine göre 4-6 STOP üret:
   - role: "fuel" | "food" | "break" | "scenic"
   - city: il adı (Samsun, Bolu vb.)
   - district: ilçe adı (Atakum, Merzifon, Mengen vb. — coğrafya bilgine güven)
     ⚠️ BÜYÜK İLÇE TERCİH ET: Of/Araklı/Çayeli gibi KÜÇÜK kasabalar yerine
     Trabzon Merkez/Sürmene/Rize Merkez gibi MERKEZ/BÜYÜK ilçeler seç. Google
     Places küçük yerlerde sonuç döndüremez → search timeout/fallback yaşanır.
   - route_fraction: 0.0-1.0 arası float — bu stop rotanın yüzde kaçında?
       anchor="home_proximity" → 0.05
       anchor="start" → 0.10
       anchor="end" → 0.95
       ortada → rota konumuna göre (300km'de 0.5, 280km'de 0.47 vb.)
   - query_hint: Google Places'e gidecek query (KISA + spesifik olsun)
       ✅ "pideci"   ✅ "köfteci"   ✅ "balıkçı iskele"   ✅ "sahil çay bahçesi"
       ✅ "manzara seyir"   ✅ "benzin"
       ❌ ÇOK UZUN ("pideci fırın Türk lokantası" gibi) — Google bunlarla zayıf
   - anchor: "home_proximity" (yolun başında yakıt + eve yakın olsun) |
             "start" (rotanın ilk ilçesi) |
             "end" (varış öncesi son ilçe) |
             null
   - rationale: tek cümle gerekçe
   - narrative_token: "STOP_1", "STOP_2", "STOP_3"... unique

3. YÖRESEL LEZZET ZEKASI KULLAN — kullanıcı food_specific belirtmediyse:
   - Akçaabat → köfte         - Bafra → pide               - Mengen (Bolu) → yöresel/aşçılık
   - Sakarya → ıslak hamburger - Rize → çay/balık          - Ordu → kavurma/balık
   - Trabzon → lahmacun/balık  - Konya → fırın kebabı      - Şanlıurfa → çiğ köfte
   - Tokat → kebap             - Mersin → tantuni          - Adana → kebap
   - Gaziantep → baklava/lahmacun                          - Antep → katmer
   Kullanıcı food_specific belirttiyse (örn "pide"), o yemek için en iyi ilçeyi seç:
   pide → Bafra/Merzifon       balık → Trabzon/Ordu/Rize   köfte → Akçaabat/Tekirdağ

4. ANCHOR + İLÇE EŞLEŞMESİ — ZORUNLU KURAL:
   - anchor="home_proximity" → city MUTLAKA origin'in **il'i**,
     district MUTLAKA origin'in il'inin bir **ilçesi**.
     ÖRNEK: origin="Samsun" → city="Samsun", district="Atakum" veya "İlkadım"
     veya "Tekkeköy" (Samsun ilçeleri). **ASLA "Tirebolu", "Görele", "Ordu"
     yazma** — bunlar rotanın ortasında, kullanıcının evine yakın değil!
   - anchor="end" → city MUTLAKA destination'ın **il'i**,
     district MUTLAKA destination'ın il'inin bir **ilçesi**.
     ÖRNEK: destination="Rize" → city="Rize", district="Rize Merkez" veya
     "İyidere" veya "Çayeli" (Rize ilçeleri). **ASLA "Görele", "Tirebolu",
     "Trabzon" yazma** — bunlar varış öncesinden değil, ortadan!
   - anchor=null → orta rota ilçe (rota %30-70 arası).

   ⚠️ home_proximity ve end için **ORTADAKİ İLÇELERİ ASLA SEÇME** — kullanıcı
      evine veya varışa yakın yer istiyor, ortayı değil.

   ⚠️ "YOLUN SONU" KESİN TANIM (food_location="Sonları" veya "sonlara doğru"):
   = Rotanın **SON %20-25**'inde, **destination'a yakın** ilçe.
   - Samsun→Rize: Sürmene/Of/Çayeli/Ardeşen (Trabzon-Rize arası). ASLA Giresun/Ordu.
   - Samsun→İstanbul: Sakarya/Kocaeli/Gebze. ASLA Bolu/Düzce.
   - İstanbul→Antalya: Manavgat/Serik/Kemer. ASLA Antalya merkez başı.
   "YOLUN BAŞI" (food_location="Başları"): rota ilk %20-25'i.
   "ORTALAR" (food_location="Ortaları"): %40-60 arası.

5. STOP SAYISI — KULLANICI TERCİHİ ÖNCELİKLİ (KESİN):
   ⚠️ SADECE kullanıcının açıkça ifade ettiği duraklar üret.

   ─── YEMEk ───
   food_specific'i TAM OLARAK oku — her bir yiyecek+konum çifti için AYRI stop üret:

   PATTERN A — TEK YEMEK ("kavurma", "pide", "balık"):
     → 1 food stop, food_location veya food_preference'a göre konum belirle.

   PATTERN B — KONUM'LU ÇOKLU YEMEK ("ortada pide, sonlarda cağ kebap"):
     → 2 AYRI food stop üret. Konum anahtar kelimeleri:
       "ortada/ortaları/rotanın ortasında" → route_fraction=0.50, anchor=null
       "sonlarda/sona doğru/yolun sonu/varmadan" → route_fraction=0.82, anchor="end"
       "başlarda/başa doğru/yolun başında" → route_fraction=0.12, anchor="start"
       ŞEHİR ADI ("Trabzon'da pide", "Ordu'da kavurma") → city=o şehir
     ÖRNEK: food_specific="ortada pide, sonlarda cağ kebap", rota=500km, passing=Samsun/Giresun/Trabzon/Artvin
       → STOP_A: role=food, query_hint="pide", city="Giresun", district="Görele",
                 route_fraction=0.50, anchor=null
       → STOP_B: role=food, query_hint="cağ kebap", city="Artvin", district="Merkez",
                 route_fraction=0.82, anchor="end"

   ⚠️ PATTERN B kuralı: food_specific'te "ortada X" veya "sonlarda X" veya "başlarda X" varsa
      MUTLAKA 2 stop üret. food_location alanını görmezden gel.

   food_location bir şehir adıysa (Rize, Trabzon, Ordu...) → o şehri city/district olarak yaz.
   food_location "Başları"/"Ortaları"/"Sonları" ise → rota %20/%50/%80'inde uygun ilçe seç.

   ─── YAKIT ───
   - custom_note'ta "yolun başında yakıt"/"evime yakın yakıt" → **1 fuel** (anchor="home_proximity")
   - custom_note'ta "sonlara doğru yakıt"/"varmadan yakıt" → **1 fuel** (anchor="end")
   - custom_note'ta İKİSİ DE varsa → 2 fuel

   ⛔ **ASLA varsayılan yakıt durağı EKLEME**. Kullanıcı istemediyse üretme.

   ⛽ ACİL YAKIT KURALI (KESİN — KULLANICI YAZMASA DA UYGULA):
      kalan_menzil / total_km < 0.25 → MUTLAKA 1 fuel stop (anchor="home_proximity",
      city=origin il'i, district=origin'e yakın ilçe, query_hint="benzin istasyonu",
      route_fraction=0.04).
      Örnek: kalan=50km, rota=433km → %11.5 < %25 → ZORUNLU.
      Örnek: kalan=100km, rota=500km → %20 < %25 → ZORUNLU.
      Örnek: kalan=200km, rota=300km → %66 → EKLEME.
      ⚠️ Bu stop'ta city = origin'in ili, district = origin'e yakın ilçe OLMALI.
      ASLA rotanın ortasındaki bir şehri seçme.

   ─── MOLA (BREAK) ─── ← SCENIC'TEN TAMAMEN BAĞIMSIZ
   - break_interval_hours > 0 VE total_km > 300 → break stop ZORUNLU:
     * total_km 300–600  → **1 break stop** (rotanın %50'sinde)
     * total_km 600–900  → **2 break stop** (rotanın %33 ve %66'sında)
     * total_km 900+     → **3 break stop** (rotanın %25, %50, %75'inde)
     break_interval_hours ≤ 1.5 ise her adımda +1 ekle (kısa intervalda daha sık mola).
     query_hint: "dinlenme tesisi" veya "mola noktası" veya "çay bahçesi mola"
     ⚠️ SCENIC STOP VARSA BİLE ayrıca break_stop ekle — ikisi FARKLI duraklar.

   ─── MANZARA ───
   - scene_filters dolu ('manzaralı','sahil','dağ',...) → **1 scenic stop**
     (break_stop varsa AYRI bir scenic stop — break = mola yeri, scenic = manzara)

   ÖRNEK: food_specific="ortada pide, sonlarda cağ kebap", kalan=100km, rota=550km, break=0
   → 1 fuel(home_prox,Samsun) + 1 food_pide(Giresun,%50) + 1 food_cağkebap(Artvin,%82) = 3 stop

   ÖRNEK: food_preference="kavurma", food_location="Ordu", break=2h, scene=['manzaralı'],
          kalan=50km, rota=836km
   → 1 fuel(home_prox) + 1 food(Ordu) + 2 break(280km,560km) + 1 scenic = 5 stop

   ÖRNEK: food_specific="pide", custom_note="yolun başında yakıt", break=0, rota=300km
   → 2 stop: 1 fuel(home_prox) + 1 food

   ─── FOOD_LOCATION ROTADA DEĞİLSE ───
   food_location bir şehir adıysa VE passing_cities içinde YOK ise:
   - Yine de food stop oluştur — o şehri istedi, alternatif öner.
   - Narrative'de şunu yaz: "Rize rotanızda değil — ancak {{STOP_X}}'de de yöresel
     lezzetler sizi bekliyor." (şehir adını belirt, kullanıcıyı bilgilendir)
   - Sapma notu: "Rize'ye gitmek rota uzunluğunu yaklaşık 30-40 km artırır."

6. NARRATIVE: **300-500 kelime**, Türkçe samimi imperative ton.
   ÖRNEK: "Yola çıkar çıkmaz **{{STOP_1}}**'de depoyu doldur. 280. km'de \
**{{STOP_2}}**'de yöresel pide molası ver — bafra pidesi meşhurdur. \
Sonra Bolu girişinde **{{STOP_3}}** manzarasıyla 15 dk kahve. Varış öncesi \
**{{STOP_4}}**'de son dolum, sonra İstanbul trafiğine takıl."

   - Mekan adlarını {{STOP_N}} placeholder olarak yaz (BACKEND replace EDECEK).
   - Km bilgisi yaz (LLM tahmini OK — backend gerçek km ile düzelterek).
   - Hava durumu uyarısını il-bazlı yaz: "Samsun-Amasya arası yağmurlu, ön farını aç."
   - "Güvenli yolculuklar" tarzı son cümle 1 kez OK.

━━━ ÇIKTI ━━━

STRICT JSON object — markdown sarmalı YOK, başka metin YOK:

{
  "passing_cities": ["Samsun","Amasya","Çorum","Bolu","Sakarya","İstanbul"],
  "stops": [
    {"role":"fuel","city":"Samsun","district":"Atakum","query_hint":"benzin istasyonu",
     "route_fraction":0.05,"anchor":"home_proximity","rationale":"yola çıkar çıkmaz, eve yakın",
     "narrative_token":"STOP_1"},
    ...
  ],
  "narrative": "Uzun imperative metin {{STOP_1}}..."
}

Bilinmeyen role/anchor üretme. JSON DIŞINDA HİÇBİR ŞEY YAZMA.
"""


def _build_user_prompt(
    origin: str,
    destination: str,
    total_km: float,
    total_min: int,
    eta_display: str,
    fuel_type: str,
    fuel_remaining_km: float,
    current_lat: Optional[float],
    current_lon: Optional[float],
    intent: Dict[str, Any],
    weather_zones: List[Dict[str, Any]],
) -> str:
    parts = [
        "## ROTA",
        f"Origin: {origin}",
        f"Destination: {destination}",
        f"Mesafe: {int(total_km)} km",
        f"Süre: {total_min // 60} sa {total_min % 60} dk",
        f"ETA: {eta_display}",
        f"Yakıt tipi: {fuel_type}",
        f"Kalan menzil: {fuel_remaining_km} km" if fuel_remaining_km > 0 else "Kalan menzil: bilinmiyor",
    ]
    if current_lat and current_lon:
        parts.append(f"Anlık konum (ev başlangıç): {current_lat:.4f}, {current_lon:.4f}")

    parts.append("\n## KULLANICI TERCİHLERİ")
    parts.append(json.dumps(intent, ensure_ascii=False, indent=2))

    if weather_zones:
        parts.append("\n## HAVA ZONLARI (il-bazlı)")
        parts.append(json.dumps(weather_zones, ensure_ascii=False, indent=2))

    parts.append(
        "\nYukarıdaki bilgilere göre passing_cities + stops + narrative üret. "
        "STRICT JSON object."
    )
    return "\n".join(parts)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Çok defansif JSON parser — markdown sarmalı, escape, trailing comma tolere."""
    if not text:
        return None
    # Markdown sarmalı temizle
    cleaned = re.sub(r"```(?:json)?", "", text)
    cleaned = cleaned.replace("```", "").strip("` \n\t")
    # İlk { ... son } arasındaki en geniş bloğu yakala
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first == -1 or last == -1 or last < first:
        return None
    candidate = cleaned[first:last + 1]
    # Trailing comma temizle (LLM bazen üretiyor)
    candidate = re.sub(r",(\s*[\}\]])", r"\1", candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        # Son fallback: kontrol karakterlerini kaldır
        candidate2 = re.sub(r"[\x00-\x1f\x7f]", " ", candidate)
        try:
            return json.loads(candidate2)
        except Exception:
            log.warning(f"⚠️ [Strategist] JSON parse fail: {exc}")
            return None
    except Exception as exc:
        log.warning(f"⚠️ [Strategist] JSON parse fail: {exc}")
        return None


def _validate_strategy(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Çok defansif validasyon: narrative + en az 1 stop yeterli.
    Eksik alanları default'a düşür, yanlış olanları atla (reddetme).
    """
    if not isinstance(parsed, dict):
        log.warning("⚠️ [Strategist] parsed dict değil")
        return None
    narrative = (parsed.get("narrative") or "").strip()
    if len(narrative) < STRATEGIST_MIN_NARRATIVE:
        log.warning(
            f"⚠️ [Strategist] narrative çok kısa ({len(narrative)} char, "
            f"min {STRATEGIST_MIN_NARRATIVE}). İlk 200: {narrative[:200]!r}"
        )
        return None
    raw_stops = parsed.get("stops") or []
    if not isinstance(raw_stops, list) or not raw_stops:
        log.warning(f"⚠️ [Strategist] stops boş veya list değil: {type(raw_stops).__name__}")
        return None
    cleaned: List[Dict[str, Any]] = []
    seen_tokens: set = set()
    for i, s in enumerate(raw_stops):
        if not isinstance(s, dict):
            log.warning(f"⚠️ [Strategist] stop #{i} dict değil, atlanıyor")
            continue
        role = (s.get("role") or "").strip().lower()
        # Role normalize — break_stop, food_stop gibi LLM hatalarını tolere
        if role.startswith("food"):
            role = "food"
        elif role.startswith("fuel") or "yakıt" in role:
            role = "fuel"
        elif role.startswith("scenic") or "manzara" in role:
            role = "scenic"
        elif role.startswith("break") or "mola" in role:
            role = "break"
        if role not in _VALID_ROLES:
            log.warning(f"⚠️ [Strategist] stop #{i} bilinmeyen role: {s.get('role')!r}, default 'break'")
            role = "break"
        city = str(s.get("city") or "").strip()
        district = str(s.get("district") or "").strip()
        # En azından city VEYA district olmalı
        if not city and not district:
            log.warning(f"⚠️ [Strategist] stop #{i} city/district boş, atlanıyor")
            continue
        query_hint = str(s.get("query_hint") or "").strip()
        if not query_hint:
            query_hint = _default_query_for(role)
        anchor = (s.get("anchor") or "").strip().lower() if s.get("anchor") else None
        if anchor and anchor not in _VALID_ANCHORS:
            # "home" gibi kısa formları normalize et
            if "home" in anchor or "ev" in anchor:
                anchor = "home_proximity"
            elif anchor.startswith("start") or "başla" in anchor:
                anchor = "start"
            elif anchor.startswith("end") or "son" in anchor or "varış" in anchor:
                anchor = "end"
            else:
                anchor = None
        token = str(s.get("narrative_token") or "").strip() or f"STOP_{i+1}"
        if token in seen_tokens:
            token = f"STOP_{i+1}_alt{len(cleaned)}"
        seen_tokens.add(token)
        cleaned.append({
            "role": role,
            "city": city,
            "district": district,
            "query_hint": query_hint,
            "anchor": anchor,
            "rationale": str(s.get("rationale") or "")[:200],
            "narrative_token": token,
        })
    if not cleaned:
        log.warning("⚠️ [Strategist] hiç geçerli stop yok")
        return None
    passing = parsed.get("passing_cities") or []
    if not isinstance(passing, list):
        passing = []
    return {
        "passing_cities": [str(c).strip() for c in passing if isinstance(c, str) and c.strip()][:12],
        "stops": cleaned[:8],
        "narrative": narrative,
    }


def _default_query_for(role: str) -> str:
    return {
        "fuel": "benzin istasyonu",
        "food": "restoran",
        "break": "kafe dinlenme",
        "scenic": "manzara seyir noktası",
    }.get(role, "restoran")


async def plan_strategy(
    *,
    origin: str,
    destination: str,
    total_km: float,
    total_min: int,
    eta_display: str,
    fuel_type: str,
    fuel_remaining_km: float,
    current_lat: Optional[float],
    current_lon: Optional[float],
    intent: Dict[str, Any],
    weather_zones: List[Dict[str, Any]],
    orchestrator,
) -> Optional[Dict[str, Any]]:
    """LLM tek çağrı: passing_cities + stops + narrative üret.

    Returns: {"passing_cities":[...], "stops":[{role,city,district,query_hint,
              anchor,rationale,narrative_token},...], "narrative":"..."}
    veya None (timeout/parse/validate fail).
    """
    if total_km <= 0:
        return None

    system = _build_system_prompt()
    user = _build_user_prompt(
        origin, destination, total_km, total_min, eta_display,
        fuel_type, fuel_remaining_km, current_lat, current_lon,
        intent, weather_zones,
    )
    messages = [SystemMessage(content=system), HumanMessage(content=user)]

    log.info(
        f"🧭 [Strategist] başlıyor — {origin} → {destination} "
        f"({int(total_km)}km), food_specific={intent.get('food_specific')!r}, "
        f"scene={intent.get('scene_filters')}, timeout={STRATEGIST_TIMEOUT_S}s"
    )

    text: Optional[str] = None
    for attempt in ("gemini", "claude"):
        llm = (
            orchestrator.llm_gemini if attempt == "gemini"
            else orchestrator.llm_claude
        )
        if not llm:
            continue
        try:
            llm_to_call = llm
            if attempt == "gemini":
                try:
                    llm_to_call = llm.bind(
                        generation_config={"response_mime_type": "application/json"},
                    )
                except Exception:
                    llm_to_call = llm
            res = await asyncio.wait_for(
                llm_to_call.ainvoke(messages),
                timeout=STRATEGIST_TIMEOUT_S,
            )
            raw = res.content if hasattr(res, "content") else str(res)
            if isinstance(raw, list):
                raw = "".join(
                    (b.get("text", "") if isinstance(b, dict) else str(b))
                    for b in raw
                )
            text = (raw or "").strip()
            if text:
                log.info(f"🧭 [Strategist] {attempt} yanıtı ({len(text)} char)")
                break
        except asyncio.TimeoutError:
            log.warning(f"⏱️ [Strategist] {attempt} timeout ({STRATEGIST_TIMEOUT_S}s)")
        except Exception as exc:
            log.warning(f"⚠️ [Strategist] {attempt} hata: {type(exc).__name__}: {exc}")

    if not text:
        log.warning("⚠️ [Strategist] Hiçbir LLM yanıt vermedi")
        return None

    parsed = _extract_json_object(text)
    if parsed is None:
        log.warning(
            f"⚠️ [Strategist] JSON çıkarılamadı (len={len(text)}): "
            f"head={text[:300]!r} | tail={text[-200:]!r}"
        )
        return None

    if STRATEGIST_DEBUG_DUMP:
        log.info(
            f"📜 [Strategist] parsed keys={list(parsed.keys())} "
            f"narrative_len={len(parsed.get('narrative') or '')} "
            f"stops_count={len(parsed.get('stops') or [])} "
            f"passing_cities={parsed.get('passing_cities')}"
        )

    validated = _validate_strategy(parsed)
    if validated is None:
        return None

    log.info(
        f"✅ [Strategist] {len(validated['stops'])} stop, "
        f"narrative={len(validated['narrative'])} char, "
        f"cities={validated['passing_cities']}"
    )
    return validated
