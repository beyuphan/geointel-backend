"""
trip_curator.py — LLM-2 Seçim + Uzun Imperative Narrative

Aday havuzunu (CandidatePools) LLM'e gönderir, LLM:
  - Hangi adayların seçileceğini belirler (sadece verilen id'lerden)
  - 300-700 kelimelik imperative tonlu narrative yazar
  - Mekan adlarını ve km'leri açıkça söyler

JSON response Pydantic ile validate edilir. Hata/timeout → None döner,
caller deterministik fallback'e düşer.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from logger import log


CURATOR_TIMEOUT_S = float(os.getenv("TRIP_CURATOR_TIMEOUT_S", "40.0"))
CURATOR_MIN_NARRATIVE_LEN = int(os.getenv("TRIP_CURATOR_MIN_NARRATIVE_LEN", "150"))


def _trim_pool_for_prompt(
    pools_all: List[Dict[str, Any]],
    limit_per_kind: int = 12,
) -> List[Dict[str, Any]]:
    """Curator prompt'una giden aday sayısını sınırla — token şişmesin."""
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for p in pools_all:
        k = p.get("kind", "food")
        by_kind.setdefault(k, []).append(p)
    out: List[Dict[str, Any]] = []
    for kind, lst in by_kind.items():
        # rating + deviation'a göre kabaca sırala (combined hep önce zaten)
        def _key(x):
            r = float(x.get("rating") or 0)
            dev = float(x.get("deviation_meters") or 9999)
            return (-r, dev)
        lst.sort(key=_key)
        out.extend(lst[: max(1, limit_per_kind)])
    return out


def _compact_candidate(p: Dict[str, Any]) -> Dict[str, Any]:
    """LLM'e gönderilecek aday — sadece karar için gerekli alanlar."""
    return {
        "id": p["id"],
        "name": p.get("name"),
        "kind": p.get("kind"),
        "lat": round(float(p["lat"]), 4),
        "lon": round(float(p["lon"]), 4),
        "km": round(float(p.get("distance_along_route_km") or 0), 1),
        "deviation_m": int(float(p.get("deviation_meters") or 0)) if p.get("deviation_meters") else None,
        "rating": p.get("rating"),
        "reviews": p.get("review_count"),
        "open_now": p.get("open_now"),
        "address": (p.get("address") or "")[:80],
        "sub_capabilities": p.get("sub_capabilities"),
        "fuel_price": (
            {
                "company": (p.get("fuel_price") or {}).get("company"),
                "price_per_liter": (p.get("fuel_price") or {}).get("price_per_liter"),
                "district": (p.get("fuel_price") or {}).get("district"),
                "city": (p.get("fuel_price") or {}).get("city"),
            }
            if p.get("fuel_price") else None
        ),
    }


def _build_system_prompt(marker_budget: int) -> str:
    return f"""Sen GeoIntel'in Trip Curator'sın — kullanıcının yolculuğu için
aday havuzundan en doğru durakları seçer ve uzun, sıcak, imperative tonlu bir
yolculuk anlatımı yazarsın.

━━━ KURALLAR (KESİN) ━━━

1) SADECE VERİLEN id'LERDEN SEÇ. Yeni mekan/aday icat etme. id havuzda yoksa
   o id'yi yazma. Bilinmeyen id Pydantic validasyonunda reddedilir.

2) MARKER BÜTÇESİ: {marker_budget}. Daha fazla seçme. Kategori payı tavsiye:
   combined=2, food=5, fuel=4, break=4, scenic=2 (combined öncelikli).

3) COMBINED POI ÖNCELİKLİ: sub_capabilities=['food','fuel','rest'] gibi
   birleşik adaylar (örn 'Hüsam Dinlenme Tesisi') varsa ÖNCE onları seç ve
   narrative'de "Hüsam'da hem yakıt al hem ye, hem dinlen" diye yaz.

4) KULLANICI HİNT'LERİNE GERÇEKTEN UYAN ADAYLARI ÜST SIRAYA AL:
   - food_specific='pide' → adın içinde 'pide/pideci/fırın' geçenleri önce.
     'balık' → 'balık/iskele/deniz'. 'kebap' → 'kebap/ocakbaşı/ızgara'.
   - scene_filters'da 'manzaralı'/'sahil' varsa, ad/adreste 'sahil/deniz/
     manzara/tepe' geçenler önce.
   - fuel anchor='start' → km<80 olan fuel önce. 'end' → km>(total-80) önce.
   - food_location 'Sonları' → km/total>0.7 olan food önce. 'Başları' → <0.3.
     'Ortaları' → 0.4-0.6. Varış noktasına yakın iki şehir varsa varışa
     daha yakın olanı seç (örn Samsun→Rize'de 'sonlara doğru' → Trabzon
     Ordu'dan ÜST SIRADA).

5) NARRATIVE — UZUN, IMPERATIVE, SOMUT:
   - 300-700 kelime. Markdown serbest (### başlık iyi).
   - Türkçe, samimi ("dostum/kanka/hocam" 1-2 kez yeter, abartma).
   - Mekan adlarını **kalın** yaz, yanına km bilgisini koy:
     "**~85. km**'de **Hüsam Dinlenme Tesisi**'nde dur — hem motorini doldur
     hem **pide** ye, çay molasıyla yola devam et."
   - İmperative ton: "şuraya git", "şurada dur", "şunu ye", "şurada yakıt al".
   - Hava durumunu il-bazlı yaz: "Samsun-Trabzon arası **yağmurlu**, ön farını
     aç, takip mesafeni artır. Rize'de hava açık."
   - Mekan adlarını sayma listesi YAPMA — bir hikaye gibi akıt: yola çıkış →
     ilk yakıt → ortada yemek → manzara molası → varış öncesi son hazırlık.
   - 'Güvenli yolculuklar' tarzı klişeyi son cümlede tek sefer kullanabilirsin.

6) ÇIKTI **STRICT JSON OBJECT** — başka hiçbir şey yazma, markdown blok ```json
   sarmalama. Şema:

{{
  "selected_stops": [
    {{"id": "<aday id>", "role": "food|fuel|break|combined|scenic",
      "reason": "kısa neden", "suggested_order": 0}}
  ],
  "narrative": "<uzun markdown text>",
  "weather_paragraph": "<opsiyonel kısa hava paragrafı veya null>"
}}

Hatalı id veya boş narrative → tüm yanıt reddedilir. Dikkatli ol.
"""


def _build_user_prompt(
    route_meta: Dict[str, Any],
    intent: Dict[str, Any],
    weather_zones: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
) -> str:
    parts = [
        "## ROTA METADATA",
        json.dumps(route_meta, ensure_ascii=False, indent=2),
        "",
        "## KULLANICI NİYETİ",
        json.dumps(intent, ensure_ascii=False, indent=2),
        "",
        "## HAVA ZONLARI (il-bazlı)",
        json.dumps(weather_zones, ensure_ascii=False, indent=2),
        "",
        f"## ADAY HAVUZU ({len(candidates)} aday)",
        json.dumps(candidates, ensure_ascii=False, indent=2),
        "",
        "Yukarıdaki adaylardan kurallara göre seç ve narrative'i yaz. "
        "JSON object çıkar.",
    ]
    return "\n".join(parts)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    # ```json ... ``` bloğunu temizle
    cleaned = re.sub(r"```(?:json)?", "", text).strip("` \n")
    # İlk { ... son } arasındaki bloğu yakala
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception as exc:
        log.warning(f"⚠️ [Curator] JSON parse: {exc}")
        return None


def _validate_curated(
    parsed: Dict[str, Any],
    valid_ids: set,
) -> Optional[Dict[str, Any]]:
    """Pydantic-free hızlı validasyon: id'ler havuzda mı, narrative dolu mu."""
    if not isinstance(parsed, dict):
        return None
    narrative = (parsed.get("narrative") or "").strip()
    if len(narrative) < CURATOR_MIN_NARRATIVE_LEN:
        log.warning(f"⚠️ [Curator] narrative çok kısa: {len(narrative)} char")
        return None
    raw_stops = parsed.get("selected_stops") or []
    if not isinstance(raw_stops, list):
        return None
    cleaned_stops: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for s in raw_stops:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip()
        if not sid or sid in seen_ids:
            continue
        if sid not in valid_ids:
            log.warning(f"⚠️ [Curator] bilinmeyen id reddedildi: {sid}")
            continue
        role = s.get("role")
        if role not in ("food", "fuel", "break", "combined", "scenic"):
            role = "food"
        seen_ids.add(sid)
        cleaned_stops.append({
            "id": sid,
            "role": role,
            "reason": str(s.get("reason") or "")[:200],
            "suggested_order": int(s.get("suggested_order") or 0),
        })
    if not cleaned_stops:
        log.warning("⚠️ [Curator] hiçbir geçerli id seçilmedi")
        return None
    return {
        "selected_stops": cleaned_stops,
        "narrative": narrative,
        "weather_paragraph": parsed.get("weather_paragraph"),
    }


async def curate_trip(
    *,
    pools,                              # CandidatePools
    route_meta: Dict[str, Any],
    intent: Dict[str, Any],
    weather_zones: List[Dict[str, Any]],
    orchestrator,
    marker_budget: int = 15,
) -> Optional[Dict[str, Any]]:
    """LLM-2 Curator: aday havuzundan seçim + uzun narrative üretir.

    Returns:
        {"selected_stops": [...], "narrative": "...", "weather_paragraph": "..."}
        veya None (timeout/parse/validasyon hatası).
    """
    if not pools or not pools.all_for_curator():
        return None

    trimmed = _trim_pool_for_prompt(pools.all_for_curator(), limit_per_kind=12)
    candidates_for_llm = [_compact_candidate(p) for p in trimmed]
    valid_ids = {c["id"] for c in trimmed}

    system = _build_system_prompt(marker_budget)
    user = _build_user_prompt(route_meta, intent, weather_zones, candidates_for_llm)

    messages = [
        SystemMessage(content=system),
        HumanMessage(content=user),
    ]

    log.info(
        f"🎨 [Curator] başlıyor — pool={len(trimmed)} aday, "
        f"timeout={CURATOR_TIMEOUT_S}s, intent.food_specific={intent.get('food_specific')!r}, "
        f"intent.scene_filters={intent.get('scene_filters')}, fuel_anchor={intent.get('fuel_anchor')}"
    )

    # Önce Gemini (JSON mode), hata olursa Claude fallback
    text: Optional[str] = None
    for attempt in ("gemini", "claude"):
        llm = (
            orchestrator.llm_gemini if attempt == "gemini"
            else orchestrator.llm_claude
        )
        if not llm:
            log.warning(f"⚠️ [Curator] {attempt} LLM yok, atlanıyor")
            continue
        try:
            # Gemini için JSON mode bind (response_mime_type)
            llm_to_call = llm
            if attempt == "gemini":
                try:
                    llm_to_call = llm.bind(
                        generation_config={"response_mime_type": "application/json"},
                    )
                except Exception:
                    llm_to_call = llm  # bind desteklenmiyorsa düz çağrı
            res = await asyncio.wait_for(
                llm_to_call.ainvoke(messages),
                timeout=CURATOR_TIMEOUT_S,
            )
            raw = res.content if hasattr(res, "content") else str(res)
            if isinstance(raw, list):
                raw = "".join(
                    (b.get("text", "") if isinstance(b, dict) else str(b))
                    for b in raw
                )
            text = (raw or "").strip()
            if text:
                log.info(f"🎨 [Curator] {attempt} yanıtı alındı ({len(text)} char)")
                break
            else:
                log.warning(f"⚠️ [Curator] {attempt} boş yanıt döndü")
        except asyncio.TimeoutError:
            log.warning(f"⏱️ [Curator] {attempt} timeout ({CURATOR_TIMEOUT_S}s)")
        except Exception as exc:
            log.warning(f"⚠️ [Curator] {attempt} hata: {type(exc).__name__}: {exc}")

    if not text:
        log.warning("⚠️ [Curator] Hiçbir LLM yanıt vermedi — fallback'e düşülüyor")
        return None

    parsed = _extract_json_object(text)
    if parsed is None:
        log.warning(
            f"⚠️ [Curator] JSON çıkarılamadı (len={len(text)}): "
            f"head={text[:200]!r} tail={text[-200:]!r}"
        )
        return None

    validated = _validate_curated(parsed, valid_ids)
    if validated is None:
        log.warning(
            f"⚠️ [Curator] Validasyon fail. parsed_keys="
            f"{list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__}, "
            f"narrative_len="
            f"{len((parsed.get('narrative') or '') if isinstance(parsed, dict) else '')}, "
            f"selected_count="
            f"{len((parsed.get('selected_stops') or []) if isinstance(parsed, dict) else [])}"
        )
        return None

    log.info(
        f"✅ [Curator] {len(validated['selected_stops'])} durak seçildi, "
        f"narrative={len(validated['narrative'])} char"
    )
    return validated
