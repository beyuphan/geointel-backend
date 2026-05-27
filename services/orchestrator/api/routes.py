"""
routes.py — v5.0

Değişiklikler:
- /api/v1/trip/plan endpoint eklendi (yapılandırılmış yolculuk planlama)
- POI overlay yakıt fiyatlarıyla zenginleştirildi
- Trip context Redis'e kaydediliyor, sonraki chat mesajları bağlamı biliyor
- Prompt'ta mekan adı yasagı güçlendirildi
"""
import json
import asyncio
import time
from datetime import datetime as _dt_tr
from typing import Optional, List, Dict, Tuple, Any
from fastapi import APIRouter, Depends

# 10. Tur — TR saat helper'ı. Container TZ env Europe/Istanbul, ama
# explicit ZoneInfo kullanmak yine de daha sağlam (env yoksa fallback).
try:
    from zoneinfo import ZoneInfo as _ZI
    _TR_TZ = _ZI("Europe/Istanbul")
except Exception:
    _TR_TZ = None


def _now_tr() -> _dt_tr:
    """Türkiye yerel saati. Container TZ'sinden bağımsız."""
    if _TR_TZ is not None:
        return _dt_tr.now(_TR_TZ)
    # Fallback: sistem TZ'si (container TZ env varsa OK)
    return _dt_tr.now()


# 13. Tur — custom_note'tan yola çıkış saatini parse et (forecast için)
_HOUR_PATTERNS = [
    # "saat 14:00'te", "14:30'da", "14.00 civarı"
    (r"\b(?:saat\s+)?(\d{1,2})[:.](\d{2})\b", 'hm'),
    # "sabah 8'de", "akşam 6'da", "öğle 12'de"
    (r"\b(sabah|öğle|öğleden sonra|akşam|gece)\s+(\d{1,2})\b", 'period'),
    # "8'de yola çıkacağım"
    (r"\b(\d{1,2})['\s]?(?:de|da|te|ta)\b", 'h_only'),
]


def _parse_departure_minutes(custom_note: Optional[str], now: _dt_tr) -> int:
    """custom_note'tan yola çıkış zamanını saptar, şu andan itibaren dakika döner.

    Tanınan kalıplar:
      - "yarın sabah 8'de" → yarın 08:00 (TR)
      - "akşam 6'da" → bugün 18:00
      - "saat 14:30'da" → bugün 14:30
      - "öğleden sonra 3'te" → bugün 15:00

    Geçmişte kalan saat (bugün için) ileri günler için uygun değilse 0 döner.
    """
    if not custom_note:
        return 0
    import re as _re_dp
    msg = custom_note.lower()

    has_tomorrow = bool(_re_dp.search(r"\byarın\b|\byarin\b", msg))
    has_today = bool(_re_dp.search(r"\bbugün\b|\bbugun\b", msg))

    target_hour: Optional[int] = None
    target_minute = 0

    # 1) saat:dakika (en spesifik)
    m = _re_dp.search(_HOUR_PATTERNS[0][0], msg)
    if m:
        try:
            h = int(m.group(1))
            mi = int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                target_hour, target_minute = h, mi
        except Exception:
            pass

    # 2) period + saat
    if target_hour is None:
        m = _re_dp.search(_HOUR_PATTERNS[1][0], msg)
        if m:
            period = m.group(1)
            try:
                h = int(m.group(2))
                if period in ("öğleden sonra", "akşam") and h < 12:
                    h += 12
                elif period == "gece" and h < 6:
                    h += 0  # gece 2 = 02:00
                elif period == "gece" and h >= 6:
                    pass
                elif period == "sabah" and h == 12:
                    h = 0
                target_hour = h
            except Exception:
                pass

    # 3) sadece saat ("8'de yola çıkacağım")
    if target_hour is None:
        m = _re_dp.search(_HOUR_PATTERNS[2][0], msg)
        if m:
            try:
                h = int(m.group(1))
                if 0 <= h <= 23:
                    target_hour = h
            except Exception:
                pass

    if target_hour is None:
        # Sadece "yarın sabah" gibi belirsiz → varsayılan sabah 08:00
        if has_tomorrow and ("sabah" in msg or "erken" in msg):
            target_hour, target_minute = 8, 0
        elif has_tomorrow:
            target_hour, target_minute = 9, 0
        else:
            return 0

    # Hedef tarih
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if has_tomorrow:
        from datetime import timedelta as _td_dp
        target = target + _td_dp(days=1)
    elif has_today:
        pass  # bugün
    elif target <= now:
        # Geçmiş bir saatse otomatik yarın
        from datetime import timedelta as _td_dp
        target = target + _td_dp(days=1)

    diff_min = int((target - now).total_seconds() / 60)
    return max(0, diff_min)


# LLM-First Refactor — il-bazlı hava durumu zone özetleyicisi
_SEVERITY_EMOJI = {
    "acik": "☀️", "hafif": "🌤️", "orta": "☂️", "siddetli": "⛈️",
}


def _summarize_weather_by_city(detayli_ozet: list) -> list:
    """Ardışık aynı-severity checkpoint'leri il-bazlı grupla.

    Backend zaten her checkpoint için `il`, `ilce`, `severity`, `durum`, `km_int`
    veriyor (mcp_city.tools.weather → Nominatim reverse geocoding). Buradaki iş:
    aynı severity ile yan yana gelen checkpoint'leri il-bazlı tek zona indirgemek.

    Returns: [{cities, severity, condition, emoji, km_range}, ...]
    """
    if not detayli_ozet or not isinstance(detayli_ozet, list):
        return []
    zones: list = []
    current = None  # {severity, cities (ordered set), condition, km_start, km_end, emoji}
    for pt in detayli_ozet:
        if not isinstance(pt, dict):
            continue
        sev = (pt.get("severity") or "acik").strip().lower()
        il = (pt.get("il") or "").strip()
        ilce = (pt.get("ilce") or "").strip()
        cond_raw = pt.get("durum") or ""
        # 'durum' alanı emoji+kelime ('🌧️ Sağanak Yağmur') — sadece kelime kısmını al
        cond = cond_raw.split(" ", 1)[1] if " " in cond_raw else cond_raw
        km_int = pt.get("km_int") if isinstance(pt.get("km_int"), (int, float)) else None
        emoji = pt.get("emoji") or _SEVERITY_EMOJI.get(sev, "🌤️")
        if not il:
            continue
        if current and current["severity"] == sev:
            # Şehri ordered-set'e ekle
            if il and il not in current["cities"]:
                current["cities"].append(il)
            if km_int is not None:
                current["km_end"] = km_int
        else:
            if current:
                zones.append(current)
            current = {
                "severity": sev,
                "cities": [il] if il else [],
                "condition": cond.strip().lower() or sev,
                "emoji": emoji,
                "km_start": km_int if km_int is not None else 0,
                "km_end": km_int if km_int is not None else 0,
            }
    if current:
        zones.append(current)
    # Final format
    out: list = []
    for z in zones:
        if not z["cities"]:
            continue
        cities = "-".join(z["cities"])
        km_range = (
            f"{int(z['km_start'])}-{int(z['km_end'])}. km"
            if z["km_end"] != z["km_start"] else f"{int(z['km_start'])}. km"
        )
        out.append({
            "cities": cities,
            "severity": z["severity"],
            "condition": z["condition"],
            "emoji": z["emoji"],
            "km_range": km_range,
        })
    return out


# Strategist v2 — narrative içindeki {{STOP_N}} placeholder'larını gerçek mekan
# adlarıyla replace eder. Resolved stop bulunamadıysa ilçe adıyla doldurur.
def _inject_stop_names(narrative: str, resolved_stops: list) -> str:
    """`{{STOP_1}}` gibi placeholder'ları **`MekanAdı`** (~Nkm) formatında replace.

    Çift-bold koruması: LLM bazen `**{{STOP_1}}**` yazıyor. Replace edince
    `**...**` içinde `**...**` oluyor (4 yıldız). Önce dış sarmayı temizle.
    """
    if not narrative:
        return narrative
    import re as _re_inj
    out = narrative
    seen_tokens: set = set()
    for stop in resolved_stops or []:
        if not isinstance(stop, dict):
            continue
        token = stop.get("narrative_token")
        if not token or token in seen_tokens:
            continue
        seen_tokens.add(token)
        name = (stop.get("name") or "").strip()
        district = (stop.get("district") or "").strip()
        city = (stop.get("city") or "").strip()
        km = stop.get("distance_along_route_km")
        if name:
            if km:
                inner = f"{name} (~{int(km)}. km)"
            else:
                inner = name
        else:
            loc = district or city or "yakındaki yer"
            inner = f"{loc} civarındaki bir yer"
        # ★ LLM'in placeholder etrafına eklediği **bold** sarmasını temizle,
        # sonra kendimiz tek bold ekleyelim → çift-bold problemi çözülür.
        placeholder = f"{{{{{token}}}}}"
        # \*\*{{STOP_N}}\*\* veya *{{STOP_N}}* veya çıplak — hepsini ele al
        patterns = [
            rf"\*\*\s*{_re_inj.escape(placeholder)}\s*\*\*",  # **{{STOP_1}}**
            rf"\*\s*{_re_inj.escape(placeholder)}\s*\*",      # *{{STOP_1}}*
            rf"`\s*{_re_inj.escape(placeholder)}\s*`",        # `{{STOP_1}}`
            _re_inj.escape(placeholder),                      # çıplak {{STOP_1}}
        ]
        replacement = f"**{inner}**"
        for pat in patterns:
            out = _re_inj.sub(pat, replacement, out)
    # Geriye kalan replace edilmemiş {{STOP_N}}'leri temizle
    out = _re_inj.sub(
        r"\*?\*?\s*\{\{STOP_\d+(?:_\d+)?\}\}\s*\*?\*?",
        "önerilen bir durak",
        out,
    )
    return out


# 14. Tur — custom_note'tan yakıt durağı anchor hint'lerini çıkar
def _parse_fuel_anchors(custom_note: Optional[str]) -> Dict[str, bool]:
    """custom_note'tan yakıt durağı konum hint'lerini çıkar.

    Returns:
        {"start": True/False, "end": True/False}
        - start: kullanıcı "yolun başında yakıt", "çıkar çıkmaz dolum" dediyse
        - end: "sonlara doğru", "varmadan önce yakıt" dediyse
    """
    if not custom_note:
        return {"start": False, "end": False}
    msg = custom_note.lower()
    start = any(k in msg for k in [
        "yolun başında", "yolun başı", "başta yakıt", "ilk dolum",
        "çıkar çıkmaz", "başlangıçta", "çıkışta yakıt", "başlangıçta yakıt",
    ])
    end = any(k in msg for k in [
        "sonlara doğru", "yolun sonunda", "son dolum", "varmadan",
        "sona doğru", "varış öncesi", "sonunda yakıt",
    ])
    return {"start": start, "end": end}
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from logger import log
from core.graph import intent_node, agent_node, custom_tool_node, should_continue, AgentState
from core.mcp_client import orchestrator
from core.macro_tools import RouteStrategyEvaluator, _reverse_geocode_district
from core.trip_candidates import (
    collect_candidates as _collect_candidates,
    build_legacy_search_plan as _build_legacy_search_plan,
    deterministic_select_from_pools as _det_select_pools,
    _apply_marker_budget,
    collect_targeted_stops as _collect_targeted_stops,
    simple_deterministic_stops as _simple_det_stops,
    build_simple_narrative as _build_simple_narrative,
)
from core.trip_curator import curate_trip as _curate_trip
from core.trip_strategist import plan_strategy as _plan_strategy
import os as _os_cur
from api.schemas import (
    ApiResponse as ApiEnvelope, ApiError, ApiMetadata,
    ChatResponse, MapData, MapMarker, ActionCard,
    ChatRequest as ChatRequestV1, LocationUpdateRequest as LocUpdateV1,
    TripPlanRequest, TripAddStopsRequest, PoiOverlay, PoiOverlayCard,
    DayPlanRequest, DayPlanCard, DayPlanSection, DayPlanResponse,
    DayPlanScheduleRequest, DayPlanScheduleResponse, ScheduleItem,
    ReplaceStopRequest, ReplaceStopResponse, ReplaceStopSuggestion,
)
from api.deps import get_optional_user

router = APIRouter()
memory = MemorySaver()

_BREAK_REJECT_PATTERNS = [
    "inşaat", "valilik", "vali ", "kaymakamlık",
    "belediye", "müdürlük", "müdürlüğü",
    "okul", "lise", "üniversite", "fakülte",
    "hastane", "klinik", "sağlık merkezi",
    "mahkeme", "adliye", "jandarma", "emniyet", "karakol",
    "cami", "camii", "kilise",
    "konut", "apartman", "rezidans",
    "hizmet binası", "idare binası",
]

def _is_valid_break_stop(place: dict) -> bool:
    name = (place.get("name") or "").lower()
    dev = float(place.get("deviation_meters") or 9999)
    if dev > 2000:
        return False
    for pattern in _BREAK_REJECT_PATTERNS:
        if pattern in name:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# WORKFLOW
# ─────────────────────────────────────────────────────────────────────────────

def _build_workflow():
    wf = StateGraph(AgentState)
    wf.add_node("classifier", intent_node)
    wf.add_node("agent", agent_node)
    wf.add_node("tools", custom_tool_node)
    wf.set_entry_point("classifier")
    wf.add_edge("classifier", "agent")
    wf.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    wf.add_edge("tools", "agent")
    return wf.compile(checkpointer=memory)


# ─────────────────────────────────────────────────────────────────────────────
# ACTION CARD FACTORY — tamamen deterministik, LLM metnine bağımlı değil
# ─────────────────────────────────────────────────────────────────────────────

def _build_action_cards(intent: dict, visual_data: dict) -> list:
    """
    Kullanıcının intent'ine ve görsel veriye göre action card üretir.
    LLM yanıt metnine hiç bakılmaz.
    """
    category    = intent.get("category", "general")
    has_poly    = bool(visual_data.get("polyline"))
    has_markers = bool(visual_data.get("markers"))
    cards = []

    # ── Rota çizildiyse ──────────────────────────────────────────────────
    if category == "routing" and has_poly:
        cards += [
            {"id": "nav_start",   "label": "Navigasyonu Başlat",        "action": "ui:start_navigation",   "icon": "🗺️", "style": "primary"},
            {"id": "food_route",  "label": "Yolda Yemek",               "action": "Yol güzergahımda uygun restoranları öner", "icon": "🍽️", "style": "secondary"},
            {"id": "fuel_route",  "label": "Yakıt Analizi",             "action": "ui:fuel_range_prompt",  "icon": "⛽", "style": "secondary",
             "action_template": "Yakıt analizimi yap, {range_km} km menzilim var, güzergahtaki istasyonları göster",
             "ui_component": "slider", "min_val": 0, "max_val": 1000},
            {"id": "radar",       "label": "Radar Noktaları",            "action": "Yol üzerindeki radar ve kontrol noktalarını göster", "icon": "📸", "style": "secondary"},
            {"id": "alt_routes",  "label": "Alternatif Rotalar",        "action": "ui:show_alternatives",  "icon": "🔄", "style": "secondary"},
        ]
        return cards[:5]

    # ── Mekan önerildi (POI overlay açık) ───────────────────────────────
    if has_markers and not has_poly:
        cards += [
            {"id": "add_route", "label": "Rotama Ekle",      "action": "Bu mekanı rotama ekle ve güzergahı güncelle", "icon": "➕", "style": "primary"},
            {"id": "alt_poi",   "label": "Başka Öner",        "action": "Farklı alternatifler öner", "icon": "🔍", "style": "secondary"},
            {"id": "map_show",  "label": "Haritada Göster",  "action": "ui:show_on_map",            "icon": "📍", "style": "secondary"},
        ]
        return cards[:3]

    # ── Rota VAR + mekan önerisi ─────────────────────────────────────────
    if has_markers and has_poly:
        cards += [
            {"id": "add_stop",  "label": "Durağı Ekle",      "action": "Bu mekanı ara durak olarak rotama ekle", "icon": "➕", "style": "primary"},
            {"id": "skip_stop", "label": "Geç",               "action": "Bu durağı atla",                         "icon": "⏭️", "style": "secondary"},
        ]
        return cards[:2]

    # ── Yakıt bağımsız sorgu ─────────────────────────────────────────────
    if category == "fuel":
        cards += [
            {"id": "fuel_find", "label": "Yakın İstasyon Bul", "action": "Bulunduğum yere en yakın benzin istasyonunu bul", "icon": "⛽", "style": "primary"},
        ]

    # ── Eczane ──────────────────────────────────────────────────────────
    if category == "pharmacy":
        cards += [
            {"id": "pharm_nav", "label": "Eczaneye Git", "action": "ui:navigate_to_pharmacy", "icon": "💊", "style": "primary"},
        ]

    # ── Etkinlik ─────────────────────────────────────────────────────────
    if category == "event":
        cards += [
            {"id": "event_nav", "label": "Etkinliğe Yol Tarifi", "action": "Bu etkinliğin mekanına navigasyon başlat", "icon": "🎯", "style": "primary"},
        ]

    # ── Gün planı ────────────────────────────────────────────────────────
    if category == "day_plan":
        cards += [
            {"id": "day_route",  "label": "Rota Oluştur",    "action": "Planladığım yerler için optimum rota oluştur", "icon": "🗺️", "style": "primary"},
            {"id": "day_more",   "label": "Daha Fazla Öner", "action": "Başka aktiviteler de öner", "icon": "✨", "style": "secondary"},
        ]

    return cards[:4]


# ─────────────────────────────────────────────────────────────────────────────
# POI OVERLAY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _build_poi_overlay(markers: list, intent: dict) -> Optional[PoiOverlay]:
    """Marker listesinden mobil swipe overlay oluşturur."""
    if not markers:
        return None

    category = intent.get("category", "places")
    cat_map = {
        "fuel":    ("⛽ Yakıt İstasyonları",  "benzin_istasyonu"),
        "pharmacy":("💊 Nöbetçi Eczaneler",   "eczane"),
        "event":   ("🎯 Etkinlikler",          "etkinlik"),
    }
    title, cat_label = cat_map.get(category, ("📍 Yakındaki Mekanlar", "poi"))
    if category == "routing":
        title, cat_label = "🍽️ Güzergah Üstü Mekanlar", "restoran"

    cards = []
    best_idx, best_score = 0, -1

    for i, m in enumerate(markers):
        if not isinstance(m, dict):
            continue
        dev    = m.get("deviation_meters", 9999) or 9999
        rating = m.get("rating") or 0
        score  = (1 / (dev + 1)) * 1000 + rating
        if score > best_score:
            best_score, best_idx = score, i

        dev_label = (
            "Yol üstü ✅" if dev <= 400
            else f"{dev}m sapma ⚠️" if dev <= 2000
            else f"{dev/1000:.1f}km uzatır ❌"
        )
        extra_min = round(dev / 500) if dev > 400 else 0
        impact    = f"+{extra_min} dk" if extra_min else "Sıfır ek süre"
        place_id  = f"poi_{i}_{str(m.get('lat',''))[:6]}_{str(m.get('lon',''))[:6]}"

        # Yakıt fiyat bilgisi (fuel_price inject edildiyse taşı)
        fuel_price_info = m.get("fuel_price") or None

        # Önerilen mekan için recommendation_reason hesapla
        rec_reason = None
        m_type = m.get("type", "poi")
        if m_type == "fuel_station" and fuel_price_info:
            price = fuel_price_info.get("price_per_liter")
            company = fuel_price_info.get("company", "")
            rec_reason = f"{company}: {price} TL/L" if price else "Yakıt İstasyonu"

        cards.append(PoiOverlayCard(
            id=place_id,
            name=m.get("name", "Mekan"),
            address=m.get("address") or m.get("snippet"),
            category=cat_label,
            lat=float(m["lat"]), lon=float(m["lon"]),
            deviation_meters=dev if dev < 9999 else None,
            distance_along_route_km=m.get("distance_along_route_km"),
            extra_time_min=extra_min if dev > 400 else 0,
            eta=m.get("eta"),
            on_route_side=m.get("on_route_side"),
            rating=m.get("rating"),
            review_count=m.get("review_count"),
            price_level=m.get("price_level"),
            is_open=m.get("open_now"),
            open_now=m.get("open_now"),
            opening_hours=m.get("opening_hours"),
            phone=m.get("phone"),
            deviation_label=dev_label,
            route_impact_label=impact,
            is_recommended=False,
            fuel_price_info=fuel_price_info,
            recommendation_reason=rec_reason,
        ))


    if cards:
        cards[best_idx] = cards[best_idx].model_copy(update={
            "is_recommended": True,
            "recommendation_reason": "En iyi konum + puan",
        })
        cards.sort(key=lambda c: (0 if c.is_recommended else 1, c.deviation_meters or 9999))

    return PoiOverlay(
        mode="poi_selection",
        title=title,
        subtitle=f"{len(cards)} mekan bulundu · Kaydırarak incele",
        cards=cards,
        primary_action="Rotama Ekle",
        secondary_action="Farklı Mekan Öner",
    )


# ─────────────────────────────────────────────────────────────────────────────
# HISTORY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_history(session_id: str) -> list:
    if not orchestrator.redis_client:
        return []
    try:
        raw = orchestrator.redis_client.lrange(f"chat:{session_id}", 0, -1)
        out = []
        for item in raw:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            msg = json.loads(item)
            if msg["role"] == "user":
                out.append(HumanMessage(content=msg["content"]))
            elif msg["role"] == "assistant":
                out.append(AIMessage(content=msg["content"]))
        return out
    except Exception as e:
        log.error(f"Geçmiş okunamadı: {e}")
        return []


def _save_history(session_id: str, user_msg: str, assistant_msg: str):
    if not orchestrator.redis_client:
        return
    key = f"chat:{session_id}"
    orchestrator.redis_client.rpush(key, json.dumps({"role": "user",      "content": user_msg}))
    orchestrator.redis_client.rpush(key, json.dumps({"role": "assistant", "content": assistant_msg}))
    orchestrator.redis_client.ltrim(key, -20, -1)
    orchestrator.redis_client.expire(key, 86400)


# ─────────────────────────────────────────────────────────────────────────────
# POLYLINE NORMALIZER — HER FORMAT → JSON koordinat listesi
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_polyline(raw) -> str:
    """Her türlü polyline formatını [[lat,lon],...] JSON'a çevirir."""
    if not raw:
        return ""
    try:
        import flexpolyline
        import polyline as gp

        decoded = []
        if isinstance(raw, list):
            decoded = [p[:2] for p in raw]
        elif isinstance(raw, str):
            raw = raw.strip()
            if raw.startswith("["):
                pts = json.loads(raw)
                decoded = [p[:2] for p in pts]
            else:
                try:
                    decoded = flexpolyline.decode(raw)   # HERE v6
                except Exception:
                    try:
                        decoded = gp.decode(raw)          # Google v5
                    except Exception:
                        return raw                        # olduğu gibi gönder

        if decoded:
            return json.dumps([[float(p[0]), float(p[1])] for p in decoded])
        return str(raw)
    except Exception as e:
        log.warning(f"⚠️ Polyline normalize hatası: {e}")
        return str(raw)


# ─────────────────────────────────────────────────────────────────────────────
# CORE CHAT RUNNER
# ─────────────────────────────────────────────────────────────────────────────

async def _run_chat(message: str, session_id: str,
                    current_lat=None, current_lon=None, fcm_token=None) -> dict:
    # Konum + FCM kaydet
    if orchestrator.redis_client and current_lat and current_lon:
        _loc_value = f"{current_lat},{current_lon}"
        orchestrator.redis_client.setex(f"loc:{session_id}", 3600, _loc_value)
        # Mobile mode değişimleriyle session_id regenerate olduğunda eski Redis
        # key'leri kayıp olmasın diye user-bazlı fallback key de yaz.
        # session_id formatı: "user_uuid:mode_timestamp" (auth) veya "mode_timestamp" (anon)
        if ":" in session_id:
            _user_id = session_id.split(":", 1)[0]
            orchestrator.redis_client.setex(
                f"loc:user:{_user_id}", 3600, _loc_value,
            )
        else:
            # Anon: mode prefix'ini at, ortak global key (multi-user'da güvensiz
            # ama anon zaten user yok). 'chat_x_y' → 'anon_x_y'.
            _parts = session_id.split("_", 1)
            if len(_parts) == 2 and _parts[0] in ("chat", "trip", "dayplan", "free"):
                orchestrator.redis_client.setex(
                    f"loc:anon:{_parts[1]}", 3600, _loc_value,
                )
    if orchestrator.redis_client and fcm_token:
        orchestrator.redis_client.setex(f"fcm:{session_id}", 86400 * 30, fcm_token)

    # Geçmiş + yeni mesaj
    history = _load_history(session_id)
    history.append(HumanMessage(content=message))

    executor = _build_workflow()
    config   = {"configurable": {"thread_id": session_id}}

    input_state = {
        "messages":    history,
        "intent":      {},
        "retry_count": 0,
        "session_id":  session_id,
        "visual_data": {"markers": [], "polyline": None, "geojson_layers": []},
        "route_polyline": None,
    }

    final_state = await executor.ainvoke(input_state, config)

    # Yanıt metni
    raw = final_state["messages"][-1].content
    if isinstance(raw, list):
        response_text = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
    else:
        response_text = str(raw)

    _save_history(session_id, message, response_text)

    # Polyline: state → Redis
    raw_poly = final_state.get("visual_data", {}).get("polyline")
    if not raw_poly and orchestrator.redis_client:
        cached = orchestrator.redis_client.get(f"route:{session_id}")
        if cached:
            raw_poly = cached if isinstance(cached, str) else cached.decode("utf-8")

    # Kullanılan araçlar
    tools_used = []
    for msg in final_state.get("messages", []):
        for tc in getattr(msg, "tool_calls", []) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name and name not in tools_used:
                tools_used.append(name)

    return {
        "response_text": response_text,
        "visual_data":   final_state.get("visual_data", {}),
        "route_polyline": raw_poly,
        "intent":        final_state.get("intent", {}),
        "tools_used":    tools_used,
        "retry_count":   final_state.get("retry_count", 0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/chat")
async def chat_legacy(request: ChatRequestV1):
    """Geriye uyumluluk endpoint."""
    t0 = time.monotonic()
    result = await _run_chat(
        message=request.message, session_id=request.session_id,
        current_lat=request.current_lat, current_lon=request.current_lon,
        fcm_token=request.fcm_token,
    )
    return {
        "response":       result["response_text"],
        "session_id":     request.session_id,
        "visual_data":    result["visual_data"],
        "route_polyline": result["route_polyline"],
        "action_cards":   _build_action_cards(result["intent"], result["visual_data"]),
        "metadata":       {"response_time_ms": int((time.monotonic() - t0) * 1000)},
    }


@router.post("/location/update")
async def update_location_legacy(req: LocUpdateV1):
    sid = req.session_id or "default_session"
    if orchestrator.redis_client:
        loc = f"{req.lat},{req.lon}"
        orchestrator.redis_client.setex(f"loc:{sid}", 3600, loc)
        return {"status": "ok", "location": loc}
    return {"status": "error", "message": "Redis unavailable"}


@router.get("/health")
async def health_check():
    redis_ok = False
    if orchestrator.redis_client:
        try:
            orchestrator.redis_client.ping()
            redis_ok = True
        except Exception:
            pass
    return {
        "status": "ok",
        "redis": redis_ok,
        "agents_connected": list(orchestrator.sessions.keys()),
        "tool_count": len(orchestrator.runtime_tools),
    }


async def _llm_invoke_with_fallback(messages, timeout: float = 15.0) -> str:
    """
    Önce Gemini'yi dener; PERMISSION_DENIED / 403 / rate-limit gibi hatalarda
    Claude'a otomatik düşer. Çağıran kod tek bir string yanıt alır.
    Boş string dönüyorsa caller fallback metni göstermeli.
    """
    last_error: Optional[Exception] = None
    # 1) Gemini denemesi
    if orchestrator.llm_gemini:
        try:
            res = await asyncio.wait_for(
                orchestrator.llm_gemini.ainvoke(messages), timeout=timeout
            )
            text = res.content if hasattr(res, "content") else str(res)
            if isinstance(text, list):
                text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
            text = (text or "").strip()
            if text:
                return text
        except Exception as e:
            last_error = e
            log.warning(f"⚠️ [LLM/Gemini] {type(e).__name__}: {str(e)[:120]}")

    # 2) Claude fallback
    if orchestrator.llm_claude:
        try:
            log.info("🔁 [LLM] Gemini başarısız → Claude'a düşülüyor")
            res = await asyncio.wait_for(
                orchestrator.llm_claude.ainvoke(messages), timeout=timeout
            )
            text = res.content if hasattr(res, "content") else str(res)
            if isinstance(text, list):
                text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
            return (text or "").strip()
        except Exception as e:
            last_error = e
            log.warning(f"⚠️ [LLM/Claude] {type(e).__name__}: {str(e)[:120]}")

    if last_error:
        log.error(f"🔥 [LLM] Hem Gemini hem Claude başarısız: {last_error}")
    return ""


async def _serve_pharmacy_request(
    *, lat: float, lon: float, session_id: str, t0: float
) -> Optional["ApiEnvelope"]:
    """
    Eczane fast-path:
    1) konumdan il/ilçe bul
    2) get_pharmacies çağır
    3) eczaneleri kullanıcı konumuna olan mesafeye göre sırala, en yakın 5
    4) LLM ile samimi öneri metni üret
    5) markers'ı Redis'e kaydet (sonraki "rota oluştur" mesajı için)
    """
    import httpx as _httpx
    from core.macro_tools import _haversine_km as _haversine

    nom_headers = {"User-Agent": "GeoIntel_Orchestrator/4.0"}
    rg: Optional[dict] = None
    async with _httpx.AsyncClient(headers=nom_headers) as nom_client:
        rg = await _reverse_geocode_district(lat, lon, nom_client)
    if not rg:
        log.warning(f"⚠️ [PharmacyFast] {lat},{lon} reverse geocode başarısız")
        return None

    city = rg["city"]
    district = rg["district"]
    log.info(f"💊 [PharmacyFast] Konum: {city}/{district}")

    pharm_tool = orchestrator.get_tool_by_name("get_pharmacies")
    if not pharm_tool:
        return None
    try:
        raw = await asyncio.wait_for(
            pharm_tool.ainvoke({"city": city, "district": district}),
            timeout=30.0,
        )
    except Exception as e:
        log.warning(f"⚠️ [PharmacyFast] get_pharmacies hata: {e}")
        return None

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    pharm_list = []
    if isinstance(raw, dict):
        pharm_list = raw.get("data") or raw.get("items") or []
    elif isinstance(raw, list):
        pharm_list = raw

    # Koordinatlıları parse et + mesafe hesabı
    parsed: List[dict] = []
    for ph in pharm_list:
        if not isinstance(ph, dict):
            continue
        coord_str = ph.get("coordinates") or ph.get("koordinat")
        plat: Optional[float] = None
        plon: Optional[float] = None
        if isinstance(coord_str, str) and "," in coord_str:
            try:
                a, b = coord_str.split(",", 1)
                plat = float(a.strip())
                plon = float(b.strip())
            except Exception:
                plat = plon = None
        if plat is None or plon is None:
            continue
        name = ph.get("name") or ph.get("isim") or "Eczane"
        addr = ph.get("address") or ph.get("adres") or ""
        phone = ph.get("phone") or ph.get("tel") or ""
        dist_label = ph.get("district") or ph.get("ilce") or district
        dist_km = _haversine(lat, lon, plat, plon)
        parsed.append({
            "name": name,
            "address": addr,
            "phone": phone,
            "district": dist_label,
            "lat": plat,
            "lon": plon,
            "distance_km": dist_km,
        })

    if not parsed:
        log.info(f"💊 [PharmacyFast] {city}/{district} için koordinatlı eczane yok")
        return None

    # En yakın 5'i seç
    parsed.sort(key=lambda p: p["distance_km"])
    nearest = parsed[:5]

    # Markers (mesafe ile snippet)
    markers: List[MapMarker] = []
    for i, p in enumerate(nearest):
        dist_label_m = (
            f"{int(p['distance_km'] * 1000)}m"
            if p["distance_km"] < 1
            else f"{p['distance_km']:.1f}km"
        )
        snippet_parts = [dist_label_m]
        if p["address"]:
            snippet_parts.append(p["address"])
        markers.append(MapMarker(
            lat=p["lat"],
            lon=p["lon"],
            title=p["name"],
            type="pharmacy",
            snippet=" · ".join(snippet_parts),
            poi_card={
                "address": p["address"],
                "phone": p["phone"],
                "district": p["district"],
                "distance_km": p["distance_km"],
                "distance_label": dist_label_m,
                "type": "pharmacy",
                "pharmacy_index": i,  # "ilkini istiyorum" için
            },
        ))

    # LLM ile samimi öneri metni
    response_text = await _generate_pharmacy_recommendation(
        nearest=nearest, city=city, district=district
    )

    # Markers'ı Redis'e kaydet — sonraki mesajlar referans çözebilsin
    if orchestrator.redis_client:
        try:
            payload = json.dumps([
                {
                    "name": p["name"],
                    "address": p["address"],
                    "lat": p["lat"],
                    "lon": p["lon"],
                    "distance_km": p["distance_km"],
                }
                for p in nearest
            ], ensure_ascii=False)
            orchestrator.redis_client.setex(
                f"pharm_options:{session_id}", 1800, payload
            )
        except Exception as _e:
            log.warning(f"⚠️ [PharmacyFast] Redis kayıt başarısız: {_e}")

    elapsed = int((time.monotonic() - t0) * 1000)
    return ApiEnvelope(
        success=True,
        data=ChatResponse(
            status="completed",
            message=response_text,
            intent={"category": "pharmacy", "confidence": 1.0},
            map=MapData(markers=markers),
            action_cards=[],
            tools_used=["get_pharmacies"],
            poi_overlay=None,
        ).model_dump(),
        metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
    )


async def _generate_pharmacy_recommendation(
    *, nearest: List[dict], city: str, district: str
) -> str:
    """LLM ile 2-3 cümlelik samimi eczane önerisi üret. Hata olursa basit fallback."""
    try:
        if orchestrator.llm_gemini and nearest:
            lines = []
            for i, p in enumerate(nearest):
                dist = (
                    f"{int(p['distance_km'] * 1000)}m"
                    if p["distance_km"] < 1
                    else f"{p['distance_km']:.1f}km"
                )
                line = f"{i + 1}. {p['name']} — {dist}"
                if p["address"]:
                    line += f" — {p['address']}"
                lines.append(line)
            sys = (
                "Sen bir öneri asistanısın. Kullanıcı 'nöbetçi eczane' aradı. "
                "Sana mesafeye göre sıralı eczane listesi veriliyor. **2-3 kısa "
                "cümle** yaz: en yakın olanı öner, neden onu önerdiğini söyle "
                "(en yakın olduğu için, kolay ulaşılır vs.). Diğer 1-2 alternatifi "
                "de kısaca an. Liste yapma — akıcı paragraf yaz. **Mekan adlarını "
                "kalın** (markdown ** ile) işaretle. Robotik 'Size şu eczaneyi "
                "öneririm' tarzı YASAK. Samimi, arkadaş gibi konuş.\n"
                "Sonunda kullanıcıya 'haritada pinleri görebilir veya bana "
                "hangisini istediğini yazabilirsin' diye hatırlat (1 cümle)."
            )
            usr = (
                f"Kullanıcı konumu: {city}/{district}\n"
                f"Eczaneler (yakından uzağa):\n" + "\n".join(lines)
            )
            res = await asyncio.wait_for(
                orchestrator.llm_gemini.ainvoke([
                    SystemMessage(content=sys),
                    HumanMessage(content=usr),
                ]),
                timeout=10.0,
            )
            text = res.content if hasattr(res, "content") else str(res)
            if isinstance(text, list):
                text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
            text = (text or "").strip()
            if len(text) > 20:
                return text
    except Exception as e:
        log.warning(f"⚠️ [PharmacyFast/LLM] {e}")

    # Fallback
    top = nearest[0]
    dist = (
        f"{int(top['distance_km'] * 1000)}m"
        if top["distance_km"] < 1
        else f"{top['distance_km']:.1f}km"
    )
    return (
        f"📍 **{city}/{district}** çevresinde **{len(nearest)}** nöbetçi eczane "
        f"buldum, haritada işaretledim. Sana en yakın olarak **{top['name']}** "
        f"({dist}) öneririm. Pinlere bakıp birini seçebilir ya da bana "
        f"hangisini istediğini yazarak rota başlatabilirsin."
    )


async def _serve_pharmacy_followup_navigation(
    *, message: str, current_lat: float, current_lon: float,
    session_id: str, t0: float,
) -> Optional["ApiEnvelope"]:
    """
    Eczane listesi sunulduktan sonra kullanıcı 'X eczanesine rota', 'ilkini',
    'en yakına götür' gibi referans yazarsa Redis'teki eczane listesinden eşle
    ve trip plan tetikle.
    """
    if not orchestrator.redis_client:
        return None
    raw = orchestrator.redis_client.get(f"pharm_options:{session_id}")
    if not raw:
        return None
    try:
        options = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(options, list) or not options:
        return None

    msg_l = message.lower()

    # 1) Numara referansı (1, 2, 3, ilk, ikinci, üçüncü)
    chosen: Optional[dict] = None
    _num_words = {
        "ilk": 0, "1.": 0, "1": 0, "birinci": 0,
        "ikinci": 1, "2.": 1, "2": 1,
        "üçüncü": 2, "ucuncu": 2, "3.": 2, "3": 2,
        "dördüncü": 3, "dorduncu": 3, "4.": 3, "4": 3,
        "beşinci": 4, "besinci": 4, "5.": 4, "5": 4,
    }
    for w, idx in _num_words.items():
        if w in msg_l and idx < len(options):
            chosen = options[idx]
            break

    # 2) "en yakın"
    if not chosen and ("en yakın" in msg_l or "en yakini" in msg_l or "yakınına" in msg_l):
        chosen = options[0]

    # 3) Eczane adından eşleşme
    if not chosen:
        for opt in options:
            name_l = (opt.get("name") or "").lower()
            # Eczane adının ana kelimesini bul (örn "Ay Eczanesi" → "ay")
            words = [w for w in name_l.replace("eczanesi", "").replace("eczane", "").split() if len(w) >= 3]
            for w in words:
                if w in msg_l:
                    chosen = opt
                    break
            if chosen:
                break

    if not chosen:
        return None

    log.info(
        f"💊 [PharmacyFollowup] '{chosen.get('name')}' seçildi, trip plan tetikleniyor"
    )

    # Trip plan tetikle (CURRENT_LOCATION → eczane koordinatı)
    try:
        trip_req = TripPlanRequest(
            origin="CURRENT_LOCATION",
            destination=f"{chosen['lat']},{chosen['lon']}",
            waypoints=[],
            waypoint_labels=[chosen.get("name") or "Eczane"],
            current_lat=current_lat,
            current_lon=current_lon,
            session_id=session_id,
            # Eczaneye gidiş — yakıt/mola/yemek soruları anlamsız
            break_interval_hours=0.0,
            food_preference="Fark etmez",
            fuel_remaining_km=0,
            custom_note=f"Hedef: {chosen.get('name') or 'eczane'} (nöbetçi).",
        )
        # plan_trip endpoint'ini direkt çağır (auth opsiyonel)
        resp = await plan_trip(trip_req, user=None)
        return resp
    except Exception as e:
        log.warning(f"⚠️ [PharmacyFollowup] trip plan hata: {e}")
        return None


async def _serve_free_mode_place_lookup(
    *,
    message: str,
    query: str,
    category: str,
    lat: float,
    lon: float,
    session_id: str,
    t0: float,
) -> Optional["ApiEnvelope"]:
    """
    Serbest modda genel POI araması: reverse_geocode ile ilçe bul, ardından
    search_hybrid_places'i kullanıcının konumunun çevresinde tetikle, sonuçları
    POI overlay (mode=navigate_to_poi) olarak dön.
    """
    import httpx as _httpx

    # Konum bilgisi (etiket için)
    nom_headers = {"User-Agent": "GeoIntel_Orchestrator/4.0"}
    rg: Optional[dict] = None
    async with _httpx.AsyncClient(headers=nom_headers) as nom_client:
        rg = await _reverse_geocode_district(lat, lon, nom_client)
    city = (rg or {}).get("city", "")
    district = (rg or {}).get("district", "")

    search_tool = orchestrator.get_tool_by_name("search_hybrid_places")
    if not search_tool:
        return None
    try:
        raw = await asyncio.wait_for(
            search_tool.ainvoke({
                "query": query,
                "lat": lat,
                "lon": lon,
                "session_id": f"freelookup:{session_id}",
            }),
            timeout=20.0,
        )
    except Exception as e:
        log.warning(f"⚠️ [FreeLookup] search_hybrid_places hata: {e}")
        return None

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    places: list = []
    if isinstance(raw, dict):
        places = (
            raw.get("places")
            or raw.get("strict_route_places", [])
            + raw.get("relaxed_route_places", [])
        )
    elif isinstance(raw, list):
        places = raw

    # En yakın 6 sonucu al, koordinatı olmayanları ele
    valid_places: list = []
    for p in places:
        if not isinstance(p, dict):
            continue
        plat = p.get("lat")
        plon = p.get("lon")
        if not isinstance(plat, (int, float)) or not isinstance(plon, (int, float)):
            continue
        valid_places.append(p)
    valid_places = valid_places[:8]

    if not valid_places:
        return None

    markers: List[MapMarker] = []
    cards: List[PoiOverlayCard] = []
    for i, p in enumerate(valid_places):
        name = p.get("name") or "Mekan"
        addr = p.get("address") or p.get("snippet") or ""
        plat = float(p["lat"])
        plon = float(p["lon"])
        rating = p.get("rating")
        markers.append(MapMarker(
            lat=plat,
            lon=plon,
            title=name,
            type="free_poi",
            snippet=addr,
            poi_card={
                "address": addr,
                "rating": rating,
                "type": "free_poi",
            },
        ))
        cards.append(PoiOverlayCard(
            id=f"free_{i}",
            name=name,
            address=addr,
            category="free_poi",
            lat=plat,
            lon=plon,
            rating=rating if isinstance(rating, (int, float)) else None,
            is_recommended=(i == 0),
            recommendation_reason=(f"{district}" if district else None),
            ai_recommendation=(f"📍 {addr}" if addr else None),
        ))

    loc_label = f"{district}/{city}".strip("/") if (district or city) else "konumun çevresi"
    # POI overlay YOK — sadece harita üzerinde marker. Kullanıcı marker'a tıklayıp
    # bilgi penceresinden detay görür veya Maps üzerinden gider.
    response_text = (
        f"📍 **{loc_label}** çevresinde **{len(markers)}** sonuç bulundu ve haritada "
        f"işaretlendi. Marker'a dokunarak detay görebilirsin."
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    return ApiEnvelope(
        success=True,
        data=ChatResponse(
            status="completed",
            message=response_text,
            intent={"category": category or "place_lookup", "confidence": 0.9},
            map=MapData(markers=markers),
            action_cards=[],
            tools_used=["search_hybrid_places"],
            poi_overlay=None,
        ).model_dump(),
        metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
    )


# Serbest mod intent sınıflandırması için keyword setleri
_FREE_PLACE_LOOKUP_KEYWORDS = {
    "kafe": ("kafe", "kafe"),
    "kahve": ("kafe kahvehane", "kafe"),
    "restoran": ("restoran lokanta", "restoran"),
    "lokanta": ("lokanta restoran", "restoran"),
    "yemek": ("restoran lokanta", "restoran"),
    "atm": ("atm bankamatik", "atm"),
    "market": ("market süpermarket", "market"),
    "süpermarket": ("market süpermarket", "market"),
    "benzin": ("benzin istasyonu", "yakıt"),
    "yakıt": ("benzin istasyonu", "yakıt"),
    "otopark": ("otopark", "otopark"),
    "hastane": ("hastane", "hastane"),
    "polis": ("polis karakolu", "polis"),
    "manav": ("manav", "manav"),
    "fırın": ("fırın ekmek", "fırın"),
    "berber": ("berber kuaför", "berber"),
}
_FREE_INFORMATIONAL_KEYWORDS = (
    "ne giyeyim", "üstüme",
    "hava nasıl", "yağmur mu", "yağmur var mı",
    "günbatımı", "gün doğumu", "saat kaçta",
    "şu an saat", "ne zaman",
)


# ─────────────────────────────────────────────────────────────────────────────
# FORCE ADD WAYPOINT — LLM bypass fast-path
# Kullanıcı aktif rotaya durak eklemek istediğinde LLM tutarsızlığını atla,
# kendi tool zincirimizi çalıştır: search_hybrid_places + get_route_data.
# ─────────────────────────────────────────────────────────────────────────────

_FORCE_ADD_STOP_WORDS = (
    "rotama", "rotaya", "rotamıza", "ekle", "ekleyelim", "ekleyiver",
    "ekleyiverin", "ekleyiverir", "eklesene", "eklesenize", "uğra",
    "uğrayalım", "ugra", "ugrayalim", "durup", "durak", "molam",
    "molamı", "molamızı", "şuna", "buna", "bir", "de", "da",
    "lütfen", "kanka", "knk", "dostum", "kankam", "olur", "mu", "mı",
    "olsun", "yapalım", "yapıver", "yapsana", "için", "oraya", "buraya",
    "ben", "biz", "sen", "şu", "bu", "lan", "yav", "yaa",
    "verelim", "verir", "veririz", "verirmisin",
)


_TR_CITIES = frozenset([
    "adana","adıyaman","afyon","ağrı","aksaray","amasya","ankara","antalya",
    "ardahan","artvin","aydın","balıkesir","bartın","batman","bayburt",
    "bilecik","bingöl","bitlis","bolu","burdur","bursa","çanakkale","çankırı",
    "çorum","denizli","diyarbakır","düzce","edirne","elazığ","erzincan",
    "erzurum","eskişehir","gaziantep","giresun","gümüşhane","hakkari","hatay",
    "ığdır","isparta","istanbul","izmir","kahramanmaraş","karabük","karaman",
    "kars","kastamonu","kayseri","kilis","kırıkkale","kırklareli","kırşehir",
    "kocaeli","konya","kütahya","malatya","manisa","mardin","mersin","muğla",
    "muş","nevşehir","niğde","ordu","osmaniye","rize","sakarya","samsun",
    "şanlıurfa","siirt","sinop","şırnak","sivas","tekirdağ","tokat","trabzon",
    "tunceli","uşak","van","yalova","yozgat","zonguldak",
])


def _extract_place_query_from_add_msg(message: str) -> Tuple[str, Optional[str]]:
    """Mesajdan (query, location_hint) çıkar.

    - stop_words atılır, geri kalan query
    - Türkiye il adı tespit edilirse location_hint döner (Google il bias için)
    """
    import re as _re_q
    msg = message or ""
    msg = _re_q.sub(r"[.,!?;:'\"]+", " ", msg)
    tokens = msg.split()
    cleaned: List[str] = []
    detected_city: Optional[str] = None
    for tok in tokens:
        tok_low = tok.lower().strip()
        # İl adı? — tokenin sonundaki ek'i temizle (Ordu'da → ordu)
        tok_base = _re_q.sub(r"['ʼ’][a-zçğıöşü]*$", "", tok_low)
        tok_base = _re_q.sub(r"[a-zçğıöşü]*$", lambda m: "", "")  # noop koruma
        # Daha basit: bilinen son ekleri çıkar
        for _suffix in ("'da", "'de", "'ta", "'te", "'ya", "'ye",
                        "da", "de", "ta", "te", "ya", "ye"):
            if tok_low.endswith(_suffix) and len(tok_low) > len(_suffix) + 2:
                cand = tok_low[:-len(_suffix)]
                if cand in _TR_CITIES:
                    tok_base = cand
                    break
        else:
            tok_base = tok_low
        if tok_base in _TR_CITIES:
            if not detected_city:
                detected_city = tok_base.title()
            # İl adını query'de de koru — Google relevance artar
            cleaned.append(tok)
            continue
        if tok_low in _FORCE_ADD_STOP_WORDS:
            continue
        cleaned.append(tok)
    out = " ".join(cleaned)
    out = _re_q.sub(r"\s+", " ", out).strip()
    return out, detected_city


async def _serve_force_add_waypoint(
    *,
    message: str,
    session_id: str,
    current_lat: Optional[float],
    current_lon: Optional[float],
    t0: float,
) -> Optional[ApiEnvelope]:
    """LLM bypass: 'ekle' niyetli mesaj için doğrudan search + get_route_data.

    1. trip_ctx + polyline Redis'ten oku
    2. Mesajdan query çıkar (stop_words at)
    3. search_hybrid_places → top-1 koordinat
    4. get_route_data (existing waypoints + yeni) → yeni polyline
    5. plan_markers merge + trip_ctx update
    6. Response döndür
    """
    if not orchestrator.redis_client:
        return None
    raw_tc = orchestrator.redis_client.get(f"trip_ctx:{session_id}")
    if not raw_tc:
        return None
    try:
        tc = json.loads(
            raw_tc if isinstance(raw_tc, str) else raw_tc.decode("utf-8")
        )
    except Exception:
        return None

    destination = tc.get("destination")
    if not destination:
        return None
    polyline_raw = orchestrator.redis_client.get(f"route:{session_id}")
    if not polyline_raw:
        return None
    polyline_str = (
        polyline_raw if isinstance(polyline_raw, str)
        else polyline_raw.decode("utf-8")
    )

    query, city_hint = _extract_place_query_from_add_msg(message)
    if not query or len(query) < 3:
        log.warning(f"⚡ [ForceAdd] query çıkarılamadı: msg={message[:50]!r}")
        return None

    log.info(f"⚡ [ForceAdd] başlıyor — query={query!r}, city_hint={city_hint!r}")

    places_tool = orchestrator.get_tool_by_name("search_hybrid_places")
    if not places_tool:
        return None

    _search_args: Dict[str, Any] = {
        "query": query,
        "route_polyline": polyline_str,
    }
    # İl adı tespit edildiyse location_name parametresi de geç — Google bias
    if city_hint:
        _search_args["location_name"] = city_hint

    try:
        raw_search = await asyncio.wait_for(
            places_tool.ainvoke(_search_args),
            timeout=25.0,
        )
    except asyncio.TimeoutError:
        log.warning("⚡ [ForceAdd] search timeout (25s) — fast-path fail")
        return None
    except Exception as e:
        log.warning(f"⚡ [ForceAdd] search hata: {type(e).__name__}: {e}")
        return None

    if isinstance(raw_search, str):
        try:
            raw_search = json.loads(raw_search)
        except Exception:
            return None
    if not isinstance(raw_search, dict):
        return None
    places_pool = (
        (raw_search.get("strict_route_places") or [])
        + (raw_search.get("relaxed_route_places") or [])
        + (raw_search.get("places") or [])
    )
    valid = [
        p for p in places_pool
        if isinstance(p, dict) and p.get("lat") and p.get("lon")
        and (p.get("name") or "").strip().lower() not in (
            "bilinmeyen mekan", "bilinmeyen", ""
        )
    ]

    # ★ FIX 7: Yason Burnu gibi küçük/lokal POI'ler için 2. fallback —
    # location_name'i at, sadece query + Türkiye geneli search
    if not valid:
        log.info(f"🔁 [ForceAdd] 0 sonuç → fallback: query+Türkiye geneli")
        fb_args = {
            "query": f"{query} Türkiye",
            "route_polyline": polyline_str,
        }
        try:
            raw_fb = await asyncio.wait_for(
                places_tool.ainvoke(fb_args), timeout=15.0,
            )
            if isinstance(raw_fb, str):
                try:
                    raw_fb = json.loads(raw_fb)
                except Exception:
                    raw_fb = {}
            if isinstance(raw_fb, dict):
                fb_pool = (
                    (raw_fb.get("strict_route_places") or [])
                    + (raw_fb.get("relaxed_route_places") or [])
                    + (raw_fb.get("places") or [])
                )
                valid = [
                    p for p in fb_pool
                    if isinstance(p, dict) and p.get("lat") and p.get("lon")
                    and (p.get("name") or "").strip().lower() not in (
                        "bilinmeyen mekan", "bilinmeyen", ""
                    )
                ]
                if valid:
                    log.info(f"🔁 [ForceAdd] fallback başarılı: {len(valid)} aday")
        except Exception as fbe:
            log.warning(f"🔁 [ForceAdd] fallback hata: {fbe}")

    if not valid:
        log.warning(f"⚡ [ForceAdd] hiç aday yok query={query!r}")
        # User-friendly mesaj döndür (None yerine ChatResponse with explanation)
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=True,
            data=ChatResponse(
                status="completed",
                message=(
                    f"**{query.strip().title()}** için uygun bir yer bulamadım dostum. "
                    f"Biraz daha spesifik söyler misin? Örnek: 'Trabzon Boztepe çay bahçesi' "
                    f"veya 'Ordu Yason Burnu seyir noktası'."
                ),
                intent={"category": "routing", "complexity": "low"},
                map=MapData(
                    markers=[],
                    polyline=_normalize_polyline(polyline_str),
                ),
                tools_used=["search_hybrid_places"],
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    top = valid[0]
    new_lat = float(top["lat"])
    new_lon = float(top["lon"])
    new_name = (top.get("name") or "Eklenen Durak").strip()
    new_address = top.get("address") or top.get("snippet")

    # Existing waypoints (sadece koordinat formatlı)
    import re as _re_fc
    _coord_re = _re_fc.compile(r"^-?\d+\.?\d*,-?\d+\.?\d*$")
    existing_wps = [
        w for w in (tc.get("waypoints") or [])
        if isinstance(w, str) and _coord_re.match(w.strip())
    ]
    new_coord = f"{new_lat},{new_lon}"
    if new_coord not in existing_wps:
        existing_wps.append(new_coord)

    route_tool = orchestrator.get_tool_by_name("get_route_data")
    if not route_tool:
        return None

    origin = tc.get("origin") or (
        f"{current_lat},{current_lon}" if current_lat and current_lon
        else "CURRENT_LOCATION"
    )
    waypoints_str = "|".join(f"{w}!pt" for w in existing_wps)

    try:
        route_res = await asyncio.wait_for(
            route_tool.ainvoke({
                "origin": origin,
                "destination": destination,
                "waypoints": waypoints_str,
                "session_id": session_id,
            }),
            timeout=30.0,
        )
    except Exception as e:
        log.warning(f"⚡ [ForceAdd] route hata: {e}")
        return None

    if isinstance(route_res, str):
        try:
            route_res = json.loads(route_res)
        except Exception:
            route_res = {}
    if not isinstance(route_res, dict):
        route_res = {}

    new_polyline = (
        route_res.get("polyline")
        or route_res.get("polyline_encoded")
        or polyline_str
    )
    new_distance = float(
        route_res.get("distance_km") or route_res.get("mesafe_km")
        or tc.get("total_km") or 0
    )
    new_duration = int(
        route_res.get("duration_min") or route_res.get("sure_dk")
        or tc.get("total_min") or 0
    )

    # Markers oluştur — yeni POI + plan_markers merge
    markers: List[MapMarker] = [
        MapMarker(
            lat=new_lat, lon=new_lon,
            title=new_name,
            type="poi",
            snippet=new_address,
            poi_card={
                "rating": top.get("rating"),
                "deviation_meters": top.get("deviation_meters"),
                "distance_along_route_km": top.get("distance_along_route_km"),
                "type": "poi",
                "from_plan": False,
                "added_via_chat": True,
            },
        ),
    ]
    plan_markers = tc.get("plan_markers") or []
    seen = {(round(m.lat, 4), round(m.lon, 4)) for m in markers}
    for pm in plan_markers:
        if not isinstance(pm, dict) or "lat" not in pm:
            continue
        try:
            pkey = (round(float(pm["lat"]), 4), round(float(pm["lon"]), 4))
        except Exception:
            continue
        if pkey in seen:
            continue
        seen.add(pkey)
        markers.append(MapMarker(
            lat=float(pm["lat"]),
            lon=float(pm["lon"]),
            title=pm.get("title") or "Önceki durak",
            type=pm.get("type", "poi"),
            snippet=pm.get("snippet"),
            poi_card={
                "rating": pm.get("rating"),
                "deviation_meters": pm.get("deviation_meters", 0),
                "distance_along_route_km": pm.get("distance_along_route_km"),
                "open_now": pm.get("open_now"),
                "fuel_price": pm.get("fuel_price"),
                "type": pm.get("type", "poi"),
                "from_plan": True,
            },
        ))

    # trip_ctx update — yeni waypoint + plan_markers + selected_stops
    tc["waypoints"] = existing_wps
    tc["total_km"] = new_distance
    tc["total_min"] = new_duration
    _new_plan_markers = list(plan_markers)
    _selected_entry = {
        "lat": new_lat, "lon": new_lon,
        "title": new_name, "type": "poi",
        "snippet": new_address,
        "rating": top.get("rating"),
        "deviation_meters": top.get("deviation_meters"),
        "distance_along_route_km": top.get("distance_along_route_km"),
        "added_via_chat": True,
    }
    _new_plan_markers.append(_selected_entry)
    # ★ KULLANICININ ONAYLADIĞI DURAK — RouteDetail "Duraklar" listesinde gözükür
    _existing_selected = list(tc.get("selected_stops") or [])
    # Dedup by lat/lon
    _sel_seen = {(round(float(s.get("lat", 0)), 4), round(float(s.get("lon", 0)), 4))
                 for s in _existing_selected if isinstance(s, dict)}
    _sel_key = (round(new_lat, 4), round(new_lon, 4))
    if _sel_key not in _sel_seen:
        _existing_selected.append(_selected_entry)
    tc["selected_stops"] = _existing_selected
    try:
        orchestrator.redis_client.setex(
            f"trip_ctx:{session_id}", 3600 * 8,
            json.dumps(tc, ensure_ascii=False),
        )
        if new_polyline and len(new_polyline) > 50:
            orchestrator.redis_client.setex(
                f"route:{session_id}", 3600, new_polyline,
            )
    except Exception:
        pass

    km_along = int(top.get("distance_along_route_km") or 0)
    hours = new_duration // 60
    mins = new_duration % 60
    msg_reply = (
        f"**{new_name}** rotanın **~{km_along}. km'sine** eklendi. "
        f"Yeni rota: **{int(new_distance)} km**, **{hours} sa {mins} dk**."
    )

    log.info(
        f"⚡ [ForceAdd] OK: {new_name} (~{km_along}km), "
        f"polyline_len={len(new_polyline)}, markers={len(markers)}"
    )

    # POI overlay sections — mobile RouteDetailScreen bunu okur
    # ve sortedMiddle stops için kullanır
    _sections = []
    by_kind: Dict[str, list] = {"fuel": [], "food": [], "break": []}
    for pm in tc["plan_markers"]:
        if not isinstance(pm, dict):
            continue
        _ptype = pm.get("type", "poi")
        if _ptype == "fuel_station":
            by_kind["fuel"].append(pm)
        elif _ptype == "restaurant":
            by_kind["food"].append(pm)
        elif _ptype == "break_stop":
            by_kind["break"].append(pm)
        elif _ptype == "poi" and pm.get("added_via_chat"):
            # Chat'te eklenen POI'leri food-break harici bir kategoriye koy
            by_kind.setdefault("custom", []).append(pm)
    for sec_kind, sec_items in by_kind.items():
        if not sec_items:
            continue
        cards = []
        for pi in sec_items:
            cards.append({
                "name": pi.get("title"),
                "address": pi.get("snippet"),
                "lat": pi.get("lat"),
                "lon": pi.get("lon"),
                "rating": pi.get("rating"),
                "distance_along_route_km": pi.get("distance_along_route_km"),
                "deviation_meters": pi.get("deviation_meters"),
            })
        _sections.append({
            "type": sec_kind,
            "title": {
                "fuel": "⛽ Yakıt",
                "food": "🍽️ Yemek",
                "break": "☕ Mola",
                "custom": "📍 Eklenen Duraklar",
            }.get(sec_kind, sec_kind),
            "cards": cards,
        })

    # Mobile RouteDetailScreen için trip_plan + poi_overlay
    trip_plan_for_mobile = {
        "origin": tc.get("origin"),
        "destination": destination,
        "total_km": new_distance,
        "total_min": new_duration,
        "waypoints": existing_wps,
        "weather_warnings": tc.get("weather_warnings"),
        "weather_details": tc.get("weather_details"),
        "weather_zones_summary": tc.get("weather_zones_summary"),
    }
    poi_overlay_for_mobile = PoiOverlay(
        mode="trip_plan",
        title=f"🗺️ {destination} Yolculuk Planı",
        subtitle=f"{int(new_distance)} km · ~{hours}sa {mins}dk",
        cards=[],
        sections=_sections if _sections else None,
        weather_warnings=tc.get("weather_warnings") or None,
        weather_details=tc.get("weather_details") or None,
        weather_zones_summary=tc.get("weather_zones_summary") or None,
        route_summary={
            "total_km": new_distance,
            "total_min": new_duration,
        },
    )

    elapsed = int((time.monotonic() - t0) * 1000)
    # ★ FIX 1: poi_overlay GÖNDERMİYORUZ — mobile hub_screen listener
    # poi_overlay değişimi görünce POI selection sheet açıyordu. Chat'te
    # durak ekleme sonrası bu yanlış — kullanıcı zaten seçimini yaptı.
    # trip_plan ve markers yeterli; RouteDetail bunlardan stops oluşturuyor.
    return ApiEnvelope(
        success=True,
        data=ChatResponse(
            status="completed",
            message=msg_reply,
            intent={"category": "routing", "complexity": "high"},
            map=MapData(
                markers=markers,
                polyline=_normalize_polyline(new_polyline),
            ),
            tools_used=["search_hybrid_places", "get_route_data"],
            distance_km=float(new_distance),
            duration_min=int(new_duration),
            poi_overlay=None,  # ← Chat add'de selection sheet açılmasın
            trip_plan=trip_plan_for_mobile,
        ).model_dump(),
        metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
    )


@router.post("/api/v1/chat", response_model=ApiEnvelope, tags=["Chat v1"])
async def chat_v1(request: ChatRequestV1, user: dict = Depends(get_optional_user)):
    """📱 Ana mobil chat endpoint."""
    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(f"📩 [v1/Chat] session={session_id} | msg={request.message[:50]}...")

    # Serbest mod: önceki trip_ctx ve route polyline cache'ini de temizle —
    # eski rota context'i karışmasın, yeni sorgu kendi haritasını çizebilsin.
    # ★ İSTİSNA: "ekle/uğra/durak/rotaya/waypoint" gibi durak EKLEME niyeti
    # varsa cache silme — plan_markers + polyline merge için gerekli.
    _msg_lower_chk = (request.message or "").lower()
    _add_intent = any(k in _msg_lower_chk for k in (
        "ekle", "uğra", "ugra", "durak", "rotaya", "waypoint",
        "rotama", "yolda dur", "yolda ekle",
    ))
    if request.mode == "free" and not _add_intent and orchestrator.redis_client:
        orchestrator.redis_client.delete(f"trip_ctx:{session_id}")
        orchestrator.redis_client.delete(f"route:{session_id}")
        log.info(f"🧹 [v1/Chat] Serbest mod — trip_ctx + route polyline ({session_id}) temizlendi")
    elif request.mode == "free" and _add_intent:
        log.info(f"🔗 [v1/Chat] mode=free ama 'ekle' niyeti var — trip_ctx KORUNDU ({session_id})")

    # ── Serbest Mod Router ───────────────────────────────────────────────
    # Free mode'da mesajı sınıflandır: place_lookup → POI overlay (navigate_to_poi),
    # informational/complex → mevcut LangGraph chat akışına bırak.
    # Bu router eski rota state'iyle hiç ilişki kurmaz → "Karabük'e ekledi"
    # tipi hatalar engellenir.
    msg_lower = request.message.lower()
    has_location = bool(request.current_lat and request.current_lon)

    # ★★ FORCE WAYPOINT ADD FAST-PATH — LLM bypass ★★
    # Kullanıcı aktif rotaya durak eklemek istiyorsa (Boztepe ekle, çay molası
    # yapalım vb.) ve trip_ctx Redis'te varsa, LLM'i atla — kendi tool zincirini
    # çalıştır. LLM bazen tool çağrısını TEXT olarak yazıyor, bu fast-path
    # bu sorunu %100 ortadan kaldırır.
    if (
        _add_intent
        and has_location
        and orchestrator.redis_client
        and orchestrator.redis_client.exists(f"trip_ctx:{session_id}")
    ):
        try:
            force_response = await _serve_force_add_waypoint(
                message=request.message,
                session_id=session_id,
                current_lat=request.current_lat,
                current_lon=request.current_lon,
                t0=t0,
            )
            if force_response is not None:
                return force_response
        except Exception as e:
            log.warning(f"⚠️ [v1/Chat] ForceAddWaypoint fail, LLM fallback: {e}")

    # 0) Eczane FOLLOW-UP — daha önce eczane listesi sunulmuşsa ve mesaj
    #    bir referans içeriyorsa (ilkini, en yakına, X eczanesine git), direkt
    #    trip plan tetikle.
    _followup_triggers = (
        "rota", "götür", "götur", "yol tarifi", "git ", "gidelim",
        "buraya git", "ilkini", "en yakın", "en yakini",
    )
    if (
        request.mode == "free"
        and has_location
        and any(t in msg_lower for t in _followup_triggers)
    ):
        try:
            fu_response = await _serve_pharmacy_followup_navigation(
                message=request.message,
                current_lat=request.current_lat,
                current_lon=request.current_lon,
                session_id=session_id,
                t0=t0,
            )
            if fu_response is not None:
                return fu_response
        except Exception as e:
            log.warning(f"⚠️ [v1/Chat] Pharmacy followup başarısız: {e}")

    # 1) Eczane fast-path (özel scraper, koordinatlı + nöbetçi listesi)
    _pharm_keywords = ("nöbetçi eczane", "nobetci eczane", "açık eczane",
                       "acik eczane", "eczane bul", "eczane var mı")
    if (
        request.mode == "free"
        and has_location
        and any(k in msg_lower for k in _pharm_keywords)
    ):
        try:
            pharm_response = await _serve_pharmacy_request(
                lat=request.current_lat,
                lon=request.current_lon,
                session_id=session_id,
                t0=t0,
            )
            if pharm_response is not None:
                return pharm_response
        except Exception as e:
            log.warning(f"⚠️ [v1/Chat] Eczane fast-path başarısız: {e}")

    # 2) Genel place_lookup fast-path (kafe, restoran, atm, market vb.)
    if request.mode == "free" and has_location:
        matched_kw = next(
            (k for k in _FREE_PLACE_LOOKUP_KEYWORDS if k in msg_lower),
            None,
        )
        if matched_kw:
            try:
                lookup_query, category = _FREE_PLACE_LOOKUP_KEYWORDS[matched_kw]
                place_response = await _serve_free_mode_place_lookup(
                    message=request.message,
                    query=lookup_query,
                    category=category,
                    lat=request.current_lat,
                    lon=request.current_lon,
                    session_id=session_id,
                    t0=t0,
                )
                if place_response is not None:
                    return place_response
            except Exception as e:
                log.warning(f"⚠️ [v1/Chat] place_lookup başarısız: {e}")

    try:
        result = await _run_chat(
            message=request.message, session_id=session_id,
            current_lat=request.current_lat, current_lon=request.current_lon,
            fcm_token=request.fcm_token,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        visual  = result["visual_data"]
        intent  = result["intent"]

        # ── Marker dönüşümü ───────────────────────────────────────────────
        markers = []
        _skip_titles = {"bilinmeyen mekan", "bilinmeyen", "unknown", "unnamed", ""}
        for m in visual.get("markers", []):
            if not (isinstance(m, dict) and "lat" in m):
                continue
            # ★ Title'sız OSM-only sonuçları ele (genelde rating=0, name=Bilinmeyen)
            _tlow = ((m.get("title") or m.get("name") or "").strip().lower())
            if _tlow in _skip_titles:
                continue
            side = m.get("on_route_side", "unknown")
            poi_card = {
                "on_route_side":           side,
                "side_label": (
                    "✅ Sağ taraf" if side == "right"
                    else "⚠️ Sol taraf" if side == "left"
                    else None
                ),
                "opening_hours":           m.get("opening_hours", []),
                "open_now":                m.get("open_now"),
                "eta":                     m.get("eta"),
                "deviation_meters":        m.get("deviation_meters", 0),
                "distance_along_route_km": m.get("distance_along_route_km"),
                "rating":                  m.get("rating"),
                "review_count":            m.get("review_count"),
                "price_level":             m.get("price_level"),
                "phone":                   m.get("phone"),
                "address":                 m.get("address") or m.get("snippet", ""),
                "type":                    m.get("type", "poi"),
            }
            markers.append(MapMarker(
                lat=m["lat"], lon=m.get("lon", m.get("lng", 0)),
                title=m.get("title", m.get("name")),
                type=m.get("type", "poi"),
                icon=m.get("icon"),
                snippet=m.get("snippet") or m.get("address"),
                poi_card=poi_card,
            ))

        # Deviation'a göre sırala
        markers.sort(key=lambda mk: (mk.poi_card or {}).get("deviation_meters") or 99999)

        # ── ★ MARKER MERGE — durak ekleme/rota güncelleme durumlarında ──
        # Eğer get_route_data tool çağrıldıysa (kullanıcı "X'e ekle" dedi),
        # önceki plan_trip markerlarını (yakıt/yemek/mola) kaybetme, yeniyle birleştir.
        # Ayrıca: 'ekle' niyeti var ama LLM tool atladı → eski marker'lar kaybolmasın.
        _used = result.get("tools_used") or []
        _route_tool_called = any(
            t in ("get_route_data", "evaluate_route_strategy") for t in _used
        )
        _add_intent_local = any(k in (request.message or "").lower() for k in (
            "ekle", "uğra", "ugra", "durak", "rotaya", "rotama", "waypoint",
        ))
        # Defensive: tool atlandı ama 'ekle' niyeti var ve eski plan var → markerları koru
        _should_recover = _add_intent_local and not _route_tool_called and not markers
        if (_route_tool_called or _should_recover) and orchestrator.redis_client:
            try:
                _raw_tc = orchestrator.redis_client.get(f"trip_ctx:{session_id}")
                if _raw_tc:
                    _tc_dict = json.loads(
                        _raw_tc if isinstance(_raw_tc, str) else _raw_tc.decode("utf-8")
                    )
                    _old_markers = _tc_dict.get("plan_markers") or []
                    if _old_markers:
                        _seen = {
                            (round(m.lat, 4), round(m.lon, 4)) for m in markers
                        }
                        _added = 0
                        for _om in _old_markers:
                            if not isinstance(_om, dict) or "lat" not in _om:
                                continue
                            _key = (round(float(_om["lat"]), 4), round(float(_om["lon"]), 4))
                            if _key in _seen:
                                continue
                            _seen.add(_key)
                            _added += 1
                            markers.append(MapMarker(
                                lat=float(_om["lat"]),
                                lon=float(_om["lon"]),
                                title=_om.get("title") or "Önceki durak",
                                type=_om.get("type", "poi"),
                                snippet=_om.get("snippet"),
                                poi_card={
                                    "rating": _om.get("rating"),
                                    "deviation_meters": _om.get("deviation_meters", 0),
                                    "distance_along_route_km": _om.get("distance_along_route_km"),
                                    "open_now": _om.get("open_now"),
                                    "fuel_price": _om.get("fuel_price"),
                                    "type": _om.get("type", "poi"),
                                    "from_plan": True,
                                },
                            ))
                        if _added:
                            log.info(
                                f"🔀 [Chat] {_added} eski plan markerı yeni rotaya merge edildi "
                                f"(toplam markers={len(markers)})"
                            )

                    # ★ FIX 3: Yeni eklenen markerları (Boztepe, waypoint) trip_ctx'e
                    # PERSİSTE ET — bir sonraki chat akışında veya route detay
                    # ekranında ara duraklar görünsün.
                    _new_markers_for_persist = []
                    _waypoints_to_persist: List[str] = list(_tc_dict.get("waypoints") or [])
                    for _m in visual.get("markers", []):
                        if not (isinstance(_m, dict) and "lat" in _m):
                            continue
                        _new_markers_for_persist.append({
                            "lat": float(_m["lat"]),
                            "lon": float(_m.get("lon", _m.get("lng", 0))),
                            "title": _m.get("title") or _m.get("name"),
                            "type": _m.get("type", "poi"),
                            "snippet": _m.get("address") or _m.get("snippet"),
                            "rating": _m.get("rating"),
                            "deviation_meters": _m.get("deviation_meters"),
                            "distance_along_route_km": _m.get("distance_along_route_km"),
                            "added_via_chat": True,
                        })
                        # Waypoint type ise rotaya eklenen lat/lon
                        if _m.get("type") == "waypoint":
                            _coord = f"{_m['lat']},{_m.get('lon', _m.get('lng', 0))}"
                            if _coord not in _waypoints_to_persist:
                                _waypoints_to_persist.append(_coord)
                    if _new_markers_for_persist:
                        _merged_plan_markers = list(_old_markers) + _new_markers_for_persist
                        # Dedup by lat/lon
                        _seen_pers = set()
                        _final_persist = []
                        for _pm in _merged_plan_markers:
                            _pkey = (round(float(_pm["lat"]), 4), round(float(_pm["lon"]), 4))
                            if _pkey in _seen_pers:
                                continue
                            _seen_pers.add(_pkey)
                            _final_persist.append(_pm)
                        _tc_dict["plan_markers"] = _final_persist
                        _tc_dict["waypoints"] = _waypoints_to_persist
                        orchestrator.redis_client.setex(
                            f"trip_ctx:{session_id}", 3600 * 8,
                            json.dumps(_tc_dict, ensure_ascii=False)
                        )
                        log.info(
                            f"💾 [Chat] trip_ctx güncellendi: "
                            f"plan_markers={len(_final_persist)}, waypoints={len(_waypoints_to_persist)}"
                        )
            except Exception as _merr:
                log.warning(f"⚠️ [Chat] Marker merge/persist hata: {_merr}")

        # ── Polyline ──────────────────────────────────────────────────────
        polyline = _normalize_polyline(result.get("route_polyline") or visual.get("polyline"))
        # ★ Polyline boş ama trip_ctx aktif rota varsa Redis'ten geri yükle
        # (LLM tool atladı durumunda mobile haritası boşalmasın)
        if not polyline and orchestrator.redis_client:
            try:
                _raw_route = orchestrator.redis_client.get(f"route:{session_id}")
                if _raw_route:
                    _existing_poly = (
                        _raw_route if isinstance(_raw_route, str)
                        else _raw_route.decode("utf-8")
                    )
                    polyline = _normalize_polyline(_existing_poly)
                    if polyline:
                        log.info(
                            f"🔄 [Chat] polyline boş → Redis'ten mevcut rotayı yükledim "
                            f"(len={len(polyline)})"
                        )
            except Exception:
                pass

        # ── Action cards ──────────────────────────────────────────────────
        raw_cards = _build_action_cards(intent, visual)
        cards = []
        for i, c in enumerate(raw_cards):
            act = c["action"]
            cards.append(ActionCard(
                id=c.get("id", f"card_{i}"),
                label=c["label"],
                action=act,
                icon=c.get("icon", ""),
                style=c.get("style", "secondary"),
                action_template=c.get("action_template"),
                is_ui_only=act.startswith("ui:"),
                ui_component=c.get("ui_component"),
                options=c.get("options"),
                min_val=c.get("min_val"),
                max_val=c.get("max_val"),
            ))

        # ── POI Overlay ───────────────────────────────────────────────────
        # Sadece markers varken rota yoksa overlay göster -> iptal, her zaman göster.
        has_poly = bool(polyline)
        poi_overlay = None
        raw_marker_dicts = visual.get("markers", [])
        poi_markers = [m for m in raw_marker_dicts if isinstance(m, dict) and m.get("type") != "waypoint"]
        if poi_markers:
            poi_overlay = _build_poi_overlay(poi_markers, intent)

        status = "completed"

        return ApiEnvelope(
            success=True,
            data=ChatResponse(
                status=status,
                message=result["response_text"],
                intent=intent,
                map=MapData(
                    markers=markers,
                    polyline=polyline,
                    geojson_layers=visual.get("geojson_layers", []),
                ),
                action_cards=cards,
                tools_used=result.get("tools_used", []),
                poi_overlay=poi_overlay,
                distance_km=visual.get("distance_km"),
                duration_min=visual.get("duration_min"),
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    except Exception as e:
        log.error(f"🔥 [v1/Chat] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="CHAT_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )


@router.post("/api/v1/location/update", response_model=ApiEnvelope, tags=["Location v1"])
async def update_location_v1(req: LocUpdateV1, user: dict = Depends(get_optional_user)):
    t0 = time.monotonic()
    prefix = user["user_id"] if user else "anon"
    sid = req.session_id or f"{prefix}:default"
    if orchestrator.redis_client:
        loc = f"{req.lat},{req.lon}"
        orchestrator.redis_client.setex(f"loc:{sid}", 3600, loc)
        log.info(f"📍 [Location] {prefix} → {loc}")
        return ApiEnvelope(
            success=True,
            data={"location": loc, "session_id": sid},
            metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000)),
        )
    return ApiEnvelope(
        success=False,
        error=ApiError(code="REDIS_UNAVAILABLE", message="Konum kaydedilemedi."),
        metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000)),
    )


@router.get("/api/v1/history/chat", response_model=ApiEnvelope, tags=["History"])
async def get_chat_history(session_id: str = "default_session",
                            user: dict = Depends(get_optional_user)):
    history = _load_history(session_id)
    messages = [
        {"role": "assistant" if isinstance(m, AIMessage) else "user", "content": m.content}
        for m in history
    ]
    return ApiEnvelope(success=True, data={"messages": messages}, metadata=ApiMetadata())


@router.get("/api/v1/health", tags=["System"])
async def health_v1():
    redis_ok = False
    if orchestrator.redis_client:
        try:
            orchestrator.redis_client.ping()
            redis_ok = True
        except Exception:
            pass

    connected = list(orchestrator.sessions.keys())
    services  = []
    for svc in ["mcp_city", "mcp_intel", "mcp_satellite"]:
        services.append({
            "name":   svc,
            "status": "online" if svc in connected else "offline",
        })

    return {
        "status":            "ok",
        "redis":             redis_ok,
        "services":          services,
        "agents_connected":  connected,
        "tool_count":        len(orchestrator.runtime_tools),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM NARRATIVE HELPER — Rota anlatımı (kuru metin yerine zengin paragraf)
# ─────────────────────────────────────────────────────────────────────────────

async def _generate_trip_narrative(
    *,
    origin: str,
    destination: str,
    total_km: float,
    total_min: int,
    eta_display: str,
    weather_warnings: list,
    radar_count: int,
    toll_info: Optional[dict],
    food_markers: list,
    fuel_section_summary: Optional[str],
    custom_note: Optional[str],
    waypoint_labels: Optional[list],
    break_markers: Optional[list] = None,
    fuel_type: Optional[str] = None,
) -> Optional[str]:
    """
    Toplanan deterministik rota verisini LLM'e sunup 3-5 cümlelik
    sıcak/akıcı bir anlatım üretir. Hata olursa None döner — caller fallback yapar.
    """
    try:
        from langchain_core.messages import SystemMessage, HumanMessage

        # Veriyi LLM'e geçilebilir bir özete dönüştür
        hours = total_min // 60
        mins = total_min % 60
        wp_names = [w for w in (waypoint_labels or []) if isinstance(w, str) and w.strip()]
        weather_lines = []
        for w in (weather_warnings or [])[:5]:
            if isinstance(w, dict):
                msg = w.get("message") or w.get("title") or w.get("severity")
                temp = w.get("temperature", "")
                rain = w.get("rain_probability", "")
                if msg:
                    detail = ""
                    if temp and temp != "?" and temp not in msg:
                        detail += f" {temp}"
                    if rain and rain not in msg:
                        detail += f", yağış olasılığı {rain}"
                    weather_lines.append(str(msg) + detail)
        food_names = [
            m.get("name") for m in (food_markers or [])[:5]
            if isinstance(m, dict) and m.get("name")
        ]

        ctx_parts = [
            f"Başlangıç: {origin}",
            f"Bitiş: {destination}",
            f"Mesafe: {int(total_km)} km",
            f"Süre: {hours} saat {mins} dakika",
            f"Tahmini varış: {eta_display}",
        ]
        if wp_names:
            ctx_parts.append(f"Ara duraklar: {', '.join(wp_names)}")
        if weather_lines:
            ctx_parts.append(f"Hava uyarıları: {' | '.join(weather_lines)}")
        if radar_count:
            ctx_parts.append(f"Radar noktası: {radar_count}")
        if toll_info and toll_info.get("total_tl"):
            ctx_parts.append(
                f"Ücretli geçiş: ~{toll_info.get('total_tl', 0):.0f} TL ({toll_info.get('count', 0)} nokta)"
            )
        if fuel_section_summary:
            # stops_by_km varsa bullet listesi olarak prompt'a yaz
            if isinstance(fuel_section_summary, dict):
                stops_km_list = fuel_section_summary.get("stops_by_km") or []
                if stops_km_list:
                    fuel_lines = []
                    for stop in stops_km_list[:5]:
                        best = stop.get("best", {}) or {}
                        fp = best.get("fuel_price") or {}
                        km = stop.get("stop_target_km") or best.get("distance_along_route_km")
                        district = fp.get("district") or ""
                        city = fp.get("city") or ""
                        company = fp.get("company") or best.get("name") or "İstasyon"
                        price = fp.get("price_per_liter")
                        if isinstance(price, (int, float)):
                            price_str = f"{price:.2f} ₺/L"
                        else:
                            price_str = fp.get("price_label") or "fiyat bilgisi yok"
                        loc = f"{district}/{city}".strip("/") if (district or city) else ""
                        fuel_lines.append(
                            f"~{int(km) if km else '?'}. km — {loc} — {company} — {price_str}"
                        )
                    if fuel_lines:
                        ctx_parts.append(
                            "Yakıt durakları:\n  · " + "\n  · ".join(fuel_lines)
                        )
                else:
                    cheapest = fuel_section_summary.get("cheapest_city")
                    best_st = fuel_section_summary.get("best_station") or {}
                    if cheapest or best_st:
                        ctx_parts.append(
                            f"Yakıt: cheapest_city={cheapest}, best_station={best_st.get('name')}"
                        )
            else:
                ctx_parts.append(f"Yakıt durumu: {fuel_section_summary}")
        if fuel_type:
            ctx_parts.append(f"Aracın yakıt tipi: {fuel_type}")
        if food_names:
            ctx_parts.append(f"Yol üstü yemek önerileri: {', '.join(food_names)}")
        # Mola noktaları — anlatımda mutlaka geçsin (kullanıcı dikkat çekti)
        break_names = [
            f"{m.get('name')} (~{int(m.get('distance_along_route_km') or 0)}. km)"
            for m in (break_markers or [])
            if isinstance(m, dict) and m.get("name")
        ]
        if break_names:
            ctx_parts.append(f"Önerilen mola noktaları: {' · '.join(break_names)}")
        if custom_note:
            ctx_parts.append(f"Kullanıcı notu: {custom_note}")

        # 10. Tur — Şu anki TR saatini ve günü prompt'a kat. LLM
        # custom_note'taki 'yarın sabah 8' / 'akşam 6' gibi ifadeleri
        # bu saat referansından doğru parse edebilsin.
        _now_for_prompt = _now_tr()
        ctx_parts.insert(
            0,
            f"Şu an (TR): {_now_for_prompt.strftime('%Y-%m-%d %H:%M (%A)')}",
        )

        context_block = "\n".join(f"- {p}" for p in ctx_parts)

        system = (
            "Sen GeoIntel uygulamasının yolculuk anlatıcısısın. Kullanıcı rotayı "
            "henüz başlatmadı; senin görevin rotayı BAŞLIK + MADDE LİSTESİ formatında "
            "kısa ama BİLGİ YOĞUN şekilde tanıtmak. Türkçe, samimi ama somut.\n\n"
            "**ÇIKTI MARKDOWN OLACAK** — mobile flutter_markdown render eder.\n"
            "- Her bölümde **mutlaka** `### emoji Başlık` ile aç.\n"
            "- Bölüm gövdesi: **2-4 madde işaretli satır** (`- `). Düz paragraf yazma — "
            "  satır başına bir somut bilgi yaz.\n"
            "- Mesafe, süre, km, fiyat, ilçe, marka, mekan adı → her zaman `**kalın**`.\n"
            "- Boş cümle, dolgu metni, 'güzel yolculuklar' gibi kapanış jargonları "
            "  YASAK (sadece son bölümün son satırında 1 cümle olarak).\n\n"
            "BÖLÜM SIRASI VE İÇERİĞİ:\n\n"
            "### 🛣️ Rota Özeti\n"
            "- **{distance} km** · **{hh} sa {mm} dk** · varış **{ETA}**\n"
            "- Yolun karakteri (sahil/dağ/otoyol/iç hat — konumlardan çıkar)\n"
            "- Varsa ara duraklar: **{Durak1}**, **{Durak2}**\n\n"
            "### 🌤️ Hava ve Güvenlik\n"
            "Hava uyarıları varsa **her biri için bir madde**: km + durum + sıcaklık + yağış%.\n"
            "Hiç uyarı yoksa tek satır: '- Rota boyunca hava açık.'\n"
            "Radar varsa: '- 📸 **{N}** radar noktası — hız limitine dikkat.'\n\n"
            "### 🍽️ Yol Üstü Öneriler\n"
            "food_markers'ı madde olarak listele (max 4):\n"
            "- **Mekan Adı** — yöre/km/puan kısa not\n"
            "Mola noktaları varsa altına 1-2 madde olarak ekle (kahve/dinlenme).\n\n"
            "### ⛽ Yakıt & Maliyet\n"
            "**ÖNEMLİ**: yakıt durakları context'te varsa **HER BİRİNİ AYRI MADDE** olarak yaz, "
            "asla tek satıra koşma. Format:\n"
            "- ~**{km}. km** · **{İlçe/İl}** · **{İstasyon}** · **{X.XX ₺/L}**\n"
            "Sonrasında 1 satır: 'En ucuz nokta: **{İlçe}** (**{X.XX ₺/L}**).'\n"
            "Varsa ücretli geçiş: '- 💳 Toplam **{TL}** TL (**{N}** gişe)'.\n\n"
            "### 💬 Kapanış\n"
            "- **Kullanıcı özel istekleri** — 'Kullanıcı notu'ndaki HER spesifik "
            "  istek için 1 satır madde yaz (1-4 madde):\n"
            "    - 'manzaralı duraklar' → '📸 Sahil/dağ manzaralı mola noktaları seçildi'\n"
            "    - 'ucuz ve kaliteli yemek' → '💰 Bütçe dostu yöresel restoran odaklı'\n"
            "    - 'sık sık mola' → '☕ Sık mola için ek dinlenme noktaları'\n"
            "    - 'yolun başında yakıt' → '⛽ İlk dolum yolun başında önerildi'\n"
            "    - 'sahil/manzara yolu' → '🌊 Sahil rotası tercih edildi'\n"
            "    - 'çocuk uyusun/aile' → '👨‍👩‍👧 Sessiz mola ve aile dostu yerler'\n"
            "- Son satır: 1 cümle güvenli yolculuk dileği.\n\n"
            "KESIN KURALLAR:\n"
            "- Veri yoksa o bölümü atla; ASLA uydurma.\n"
            "- Mesafe/süreyi tek bir bölümde söyle (Rota Özeti).\n"
            "- Toplam çıktı 12-22 satır olmalı (madde başına 1 satır).\n"
            "- Düz cümle paragraflar yerine madde işaretleri.\n\n"
            "ZAMAN/TARİH KURALI (10. Tur — ÖNEMLİ):\n"
            "- Context'in ilk satırındaki 'Şu an (TR)' referans olarak kullan.\n"
            "- Kullanıcı notunda 'yarın sabah 8'de', 'akşam 7'de yola çıkacağım' "
            "  gibi ifade varsa: ETA'yı bu yola çıkış saatine göre yeniden "
            "  hesapla (yola çıkış + Süre = Varış). Rota Özeti'nde **{ETA}** "
            "  yerine bu hesaplanan saati yaz.\n"
            "- 'Yarın', 'önümüzdeki Pazar' gibi gün ifadesi varsa varış "
            "  satırına o günü dahil et (örn. 'yarın 14:30 civarı').\n"
            "- Kullanıcı saat belirtmediyse context'teki ETA olduğu gibi kullan."
        )
        user_msg = (
            "Aşağıdaki rota için kullanıcıya gösterilecek 4-paragraflık tanıtımı yaz:\n\n"
            f"{context_block}"
        )

        # Gemini daha hızlı; narrative için yeterli (10. Tur: timeout 30→15sn)
        res = await asyncio.wait_for(
            orchestrator.llm_gemini.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=user_msg),
            ]),
            timeout=15.0,
        )
        text = res.content if hasattr(res, "content") else str(res)
        if isinstance(text, list):
            text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
        text = (text or "").strip()
        if len(text) < 150:
            log.warning(f"⚠️ [TripPlan] Narrative çok kısa ({len(text)} char), fallback'e düşülüyor")
            return None
        return text
    except asyncio.TimeoutError:
        log.warning("⚠️ [TripPlan] Narrative timeout (15sn) — fallback'e düşüyor")
        return None
    except Exception as e:
        log.warning(f"⚠️ [TripPlan] Narrative üretilemedi: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# TRIP PARSE INTENT — 11. Tur: serbest metni TripPlanRequest field'larına çevir
# Wizard'da "Hızlı Mod" toggle bu endpoint'i çağırır; mobile parsed JSON ile
# form'u doldurur, sonra mevcut /trip/plan akışı çalışır.
# ─────────────────────────────────────────────────────────────────────────────

from pydantic import BaseModel as _BM, Field as _F  # noqa: E402


class _TripParseIntentRequest(_BM):
    message: str = _F(min_length=4, max_length=1500)
    current_lat: Optional[float] = None
    current_lon: Optional[float] = None


@router.post("/api/v1/trip/parse_intent", tags=["Trip Planning"])
async def parse_trip_intent(
    request: _TripParseIntentRequest, user: dict = Depends(get_optional_user),
):
    """LLM ile serbest metni TripPlanRequest field'larına parse eder.

    Mobile bu parsed dict'i alıp wizard'ı doldurur, sonra mevcut /trip/plan
    endpoint'ini normal akışla çağırır. Bu endpoint TEK LLM call yapar;
    rota planlamaz, sadece niyet çıkarır.
    """
    import re as _re_pi
    t0 = time.monotonic()
    try:
        if not orchestrator.llm_gemini and not orchestrator.llm_claude:
            return ApiEnvelope(
                success=False,
                error=ApiError(code="LLM_UNAVAILABLE", message="LLM mevcut değil."),
                metadata=ApiMetadata(response_time_ms=0),
            )

        now_str = _now_tr().strftime("%Y-%m-%d %H:%M (%A)")
        system_prompt = (
            "Sen GeoIntel'in serbest-metin parser'ısın. Kullanıcının seyahat "
            "niyetinden yapısal alanları çıkarırsın. ÇIKTI STRICT JSON object.\n\n"
            f"Şu an (TR yerel): {now_str}\n\n"
            "Şema:\n"
            "{\n"
            '  "destination": "<şehir veya adres — boşsa boş string>",\n'
            '  "waypoints": ["<şehir1>", "<şehir2>"],  // opsiyonel\n'
            '  "food_preference": "Yöresel Lezzetler|Fast Food|Ev Yemekleri|Kahve & Tatlı|Fark etmez",\n'
            '  "food_location": "Başları|Ortaları|Sonları|<şehir adı>|\'\'",\n'
            '  "food_specific": "<spesifik yemek adı (pide, balık, kebap, lahmacun, döner, mantı, çorba, kahvaltı) — yoksa \'\'>",\n'
            '  "food_quality_hint": "<ucuz | kaliteli | hızlı | romantik | manzaralı — yoksa \'\'>",\n'
            '  "scene_filters": ["manzaralı","sahil","aile dostu","sessiz","doğa"],  // 0-3 etiket\n'
            '  "fuel_remaining_km": <int 0-1200; menzil belirtilmediyse 0>,\n'
            '  "break_interval_hours": <float 0-8; mola sıklığı belirtilmediyse 2.0>,\n'
            '  "custom_note": "<TÜM özel istekler — uzun ve detaylı yazılır>",\n'
            '  "search_plan": [  // opsiyonel — kullanıcı birden çok bölgede arama istiyorsa\n'
            '    {"kind":"food","query":"pide pideci","fraction_range":[0.3,0.6],"max_results":12},\n'
            '    {"kind":"food","query":"balık restoran","region_hint":{"city":"Trabzon"},"max_results":10},\n'
            '    {"kind":"break","query":"çay bahçesi sahil","fraction_range":[0.85,0.95]},\n'
            '    {"kind":"fuel","anchor":"start"}\n'
            '  ]\n'
            "}\n\n"
            "## custom_note KRİTİK KURAL\n"
            "Bu alan **kullanıcının orijinal cümlesinden TÜM özel istekleri** "
            "barındırmalı. Aşağıdaki ifadelerin HER BİRİ buraya yazılır:\n"
            "  • Saat/tarih: 'yarın sabah 8'de', 'akşam 6'da yola çıkacağım'\n"
            "  • Yakıt konumu: 'yolun başında yakıt', 'sonlarına doğru ikinci dolum'\n"
            "  • Yemek detayı: 'ucuz ve kaliteli yemek', 'balık restoranı arıyorum'\n"
            "  • Mola tercihi: 'sık sık mola', 'manzaralı duraklar', 'sahil molası'\n"
            "  • Atmosfer: 'manzaralı yol', 'sessiz/huzurlu yer', 'aile dostu'\n"
            "  • Diğer: 'çocuk uyusun', 'köpekle gidiyorum', 'engelli erişim'\n"
            "Boyut: 50-400 char. ÖZET DEĞİL, DETAY TUT.\n\n"
            "## YENİ ALANLAR — DETAY\n"
            "- food_specific: Kullanıcı 'pide istiyorum/balık yiyelim/kebap canım çekti' "
            "  derse spesifik kelimeyi buraya yaz ('pide', 'balık', 'kebap'). "
            "  Sistem bunu Google Places query'sine direkt ekler. Belirsizse boş.\n"
            "- food_quality_hint: 'ucuz kaliteli', 'romantik akşam yemeği', 'hızlı geçelim' "
            "  ifadeleri için tek kelime ('ucuz','kaliteli','hızlı','romantik','manzaralı').\n"
            "- scene_filters: ['manzaralı','sahil','aile dostu','sessiz','doğa'] etiketleri. "
            "  Kullanıcı 'sahil yolundan', 'manzaralı duraklarda mola' derse uygun "
            "  etiketleri seç. Max 3. Hiç hint yoksa boş array.\n"
            "- search_plan: SADECE kullanıcı çok-bölgeli/karmaşık istek belirttiyse üret. "
            "  Örn: 'Samsun-Rize, yolun ortasında pide, Trabzon'a varmadan balık, "
            "  yola çıkar çıkmaz yakıt' → 3 ayrı item üret. Basit isteklerde boş bırak.\n\n"
            "## DİĞER KURALLAR\n"
            "- destination metinden çıkarılamıyorsa boş string döndür.\n"
            "- food_location: 'rotanın sonuna doğru'/'sonlara' → 'Sonları'; "
            "  'ortada'/'rotanın ortası' → 'Ortaları'; 'başında' → 'Başları'.\n"
            "  Kullanıcı belirli şehir derse ('Trabzon\\'da', 'Akçaabat'ta yemek') "
            "  → şehir adı döndür.\n"
            "- food_preference: 'pide/köfte/balık/yöresel/ucuz kaliteli' → 'Yöresel Lezzetler'; "
            "  'fast food/burger/pizza' → 'Fast Food'; 'kafe/kahve/tatlı' → 'Kahve & Tatlı'; "
            "  'ev yemeği/ev tarzı' → 'Ev Yemekleri'.\n"
            "- fuel_remaining_km: 'X km menzilim var', 'Y km gidebilir' → int X/Y.\n"
            "- break_interval_hours: 'sık sık mola'/'sık mola' → 1.5; "
            "  '2 saatte bir' → 2.0; 'molasız'/'durmadan' → 0; 'uzun mola' → 3.0.\n"
            "- ÇIKTI yalnızca JSON object. Markdown/yorum YOK."
        )
        user_prompt = f"Kullanıcı niyeti:\n\"{request.message.strip()}\"\n\nJSON üret:"

        text = await _llm_invoke_with_fallback(
            [SystemMessage(content=system_prompt),
             HumanMessage(content=user_prompt)],
            timeout=15.0,
        )
        if not text:
            return ApiEnvelope(
                success=False,
                error=ApiError(code="LLM_EMPTY", message="LLM yanıt vermedi."),
                metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000)),
            )

        m = _re_pi.search(r"\{[\s\S]*\}", text)
        if not m:
            log.warning(f"⚠️ [ParseIntent] JSON bulunamadı: {text[:160]!r}")
            return ApiEnvelope(
                success=False,
                error=ApiError(code="PARSE_ERROR", message="JSON çıkarılamadı."),
                metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000)),
            )
        try:
            parsed = json.loads(m.group(0))
        except Exception as e:
            log.warning(f"⚠️ [ParseIntent] JSON parse: {e}")
            return ApiEnvelope(
                success=False,
                error=ApiError(code="JSON_INVALID", message=str(e)),
                metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000)),
            )

        # Sanitize / validate field'lar — defaultlara düş
        _VALID_FOOD_PREF = {
            "Yöresel Lezzetler", "Fast Food", "Ev Yemekleri",
            "Kahve & Tatlı", "Fark etmez",
        }
        _VALID_SCENE = {
            "manzaralı", "sahil", "aile dostu", "sessiz", "doğa",
            "huzurlu", "tarihi",
        }
        _VALID_QUALITY = {"ucuz", "kaliteli", "hızlı", "romantik", "manzaralı"}
        _VALID_KINDS = {"food", "fuel", "break", "scenic"}

        # search_plan validate (LLM uyduruyorsa atılır)
        raw_plan = parsed.get("search_plan") or []
        clean_plan: List[dict] = []
        if isinstance(raw_plan, list):
            for it in raw_plan[:6]:
                if not isinstance(it, dict):
                    continue
                kind = it.get("kind")
                if kind not in _VALID_KINDS:
                    continue
                q = str(it.get("query") or "").strip()
                if not q and kind != "fuel":
                    continue
                item = {"kind": kind, "query": q or "benzin istasyonu"}
                rh = it.get("region_hint")
                if isinstance(rh, dict) and rh.get("city"):
                    item["region_hint"] = {"city": str(rh["city"]).strip()}
                fr = it.get("fraction_range")
                if isinstance(fr, (list, tuple)) and len(fr) == 2:
                    try:
                        a, b = float(fr[0]), float(fr[1])
                        if 0.0 <= a < b <= 1.0:
                            item["fraction_range"] = [a, b]
                    except Exception:
                        pass
                anchor = it.get("anchor")
                if anchor in ("start", "end"):
                    item["anchor"] = anchor
                mr = it.get("max_results")
                if isinstance(mr, (int, float)):
                    item["max_results"] = max(1, min(30, int(mr)))
                clean_plan.append(item)

        raw_scene = parsed.get("scene_filters") or []
        scene_filters = [
            str(s).strip().lower() for s in raw_scene
            if isinstance(s, str) and str(s).strip().lower() in _VALID_SCENE
        ][:3]

        food_quality = str(parsed.get("food_quality_hint") or "").strip().lower()
        if food_quality not in _VALID_QUALITY:
            food_quality = ""

        out = {
            "destination": str(parsed.get("destination") or "").strip(),
            "waypoints": [
                str(w).strip() for w in (parsed.get("waypoints") or [])
                if isinstance(w, str) and w.strip()
            ][:5],
            "food_preference": (
                parsed.get("food_preference")
                if parsed.get("food_preference") in _VALID_FOOD_PREF
                else "Fark etmez"
            ),
            "food_location": str(parsed.get("food_location") or "").strip() or "Ortaları",
            "food_specific": str(parsed.get("food_specific") or "").strip()[:60],
            "food_quality_hint": food_quality,
            "scene_filters": scene_filters,
            "fuel_remaining_km": 0,
            "break_interval_hours": 2.0,
            "custom_note": str(parsed.get("custom_note") or "").strip(),
            "search_plan": clean_plan,
        }
        try:
            fk = float(parsed.get("fuel_remaining_km") or 0)
            out["fuel_remaining_km"] = max(0, min(1200, int(fk)))
        except Exception:
            pass
        try:
            bh = float(parsed.get("break_interval_hours") or 2.0)
            out["break_interval_hours"] = max(0, min(8, round(bh, 1)))
        except Exception:
            pass

        elapsed = int((time.monotonic() - t0) * 1000)
        log.info(
            f"✅ [ParseIntent] {elapsed}ms — dest={out['destination']!r} "
            f"food={out['food_preference']!r} specific={out['food_specific']!r} "
            f"scene={out['scene_filters']} plan_items={len(clean_plan)}"
        )
        return ApiEnvelope(
            success=True,
            data={"parsed": out},
            metadata=ApiMetadata(response_time_ms=elapsed),
        )
    except Exception as e:
        log.error(f"🔥 [ParseIntent] Hata: {e}")
        return ApiEnvelope(
            success=False,
            error=ApiError(code="PARSE_INTENT_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000)),
        )


# ─────────────────────────────────────────────────────────────────────────────
# TRIP PLAN ENDPOINT — Yapılandırılmış Yolculuk Planlama
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/v1/trip/plan", response_model=ApiEnvelope, tags=["Trip Planning"])
async def plan_trip(request: TripPlanRequest, user: dict = Depends(get_optional_user)):
    """
    🗺️ Akıllı Yolculuk Planlayıcı

    TripSetupWizard'dan gelen yapılandırılmış parametreleri alır:
    - Deterministik araç çağrısı (LLM'e doğal dil gönderilmez)
    - Rota + mola + yemek + yakıt analizini tek seferde yapar
    - Trip context'i session'a kaydeder → sonraki chat mesajları bu bağlamı bilir
    """
    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(f"🗺️ [TripPlan] session={session_id} | {request.origin} → {request.destination}")

    try:
        # ── 1. Konum çöz ──────────────────────────────────────────────────
        origin = request.origin
        if origin == "CURRENT_LOCATION":
            if request.current_lat and request.current_lon:
                origin = f"{request.current_lat},{request.current_lon}"
            elif orchestrator.redis_client:
                _cached_loc = orchestrator.redis_client.get(f"loc:{session_id}")
                if _cached_loc:
                    origin = _cached_loc if isinstance(_cached_loc, str) else _cached_loc.decode("utf-8")

        # Konum + FCM kaydet (user fallback key dahil — mode değişiminde kayıp olmasın)
        if orchestrator.redis_client and request.current_lat and request.current_lon:
            _loc_value = f"{request.current_lat},{request.current_lon}"
            orchestrator.redis_client.setex(f"loc:{session_id}", 3600, _loc_value)
            if ":" in session_id:
                _user_id = session_id.split(":", 1)[0]
                orchestrator.redis_client.setex(
                    f"loc:user:{_user_id}", 3600, _loc_value,
                )
            else:
                _parts = session_id.split("_", 1)
                if len(_parts) == 2 and _parts[0] in ("chat", "trip", "dayplan", "free"):
                    orchestrator.redis_client.setex(
                        f"loc:anon:{_parts[1]}", 3600, _loc_value,
                    )
        if orchestrator.redis_client and request.fcm_token:
            orchestrator.redis_client.setex(f"fcm:{session_id}", 86400 * 30, request.fcm_token)

        # ── 2. Kullanıcı profili — yakıt tipi ve tüketim ─────────────────
        # Yetkili kullanıcının primary aracını DB'den çek (ProfileManager
        # eski string-context döndürdüğü için inline sorgu yapıyoruz).
        fuel_type = "benzin"
        fuel_consumption = 8.0  # varsayılan L/100km
        if user and user.get("user_id"):
            try:
                from core.db import async_session_maker, UserVehicle
                from sqlmodel import select as _select
                from uuid import UUID as _UUID
                async with async_session_maker() as _s:
                    _res = await _s.execute(
                        _select(UserVehicle).where(
                            UserVehicle.user_id == _UUID(user["user_id"]),
                            UserVehicle.is_primary == True,  # noqa: E712
                        )
                    )
                    _veh = _res.scalars().first()
                    if _veh:
                        # DB'de "gasoline/diesel/lpg/electric" tutuyoruz; downstream
                        # tool'lar Türkçe ("benzin/dizel/lpg/elektrik") bekliyor.
                        _ft_map = {
                            "gasoline": "benzin", "diesel": "dizel",
                            "lpg": "lpg", "electric": "elektrik",
                            "benzin": "benzin", "dizel": "dizel",
                        }
                        fuel_type = _ft_map.get(
                            (_veh.fuel_type or "").lower(), _veh.fuel_type or "benzin"
                        )
                        if _veh.highway_consumption:
                            fuel_consumption = float(_veh.highway_consumption)
                        log.info(
                            f"🚗 [TripPlan] User vehicle: {_veh.brand} {_veh.model} "
                            f"({fuel_type}, {fuel_consumption} L/100km)"
                        )
            except Exception as _e:
                log.warning(f"⚠️ [TripPlan] Vehicle lookup failed: {_e}")

        # ── 3. Temel rota hesapla ─────────────────────────────────────────
        # Kullanıcının seçtiği duraklar passThrough modunda — section break yapma,
        # tüm rotayı tek optimizasyonda çöz (sahil/manzara yolu doğal akış)
        waypoints_str = (
            "|".join(f"{w}!pt" for w in request.waypoints)
            if request.waypoints else None
        )
        route_args = {
            "origin": origin,
            "destination": request.destination,
            "preference": "fastest",
        }
        if waypoints_str:
            route_args["waypoints"] = waypoints_str

        route_tool = orchestrator.get_tool_by_name("get_route_data")
        if not route_tool:
            raise RuntimeError("get_route_data tool bulunamadı")

        route_res = await asyncio.wait_for(route_tool.ainvoke(route_args), timeout=30.0)
        if isinstance(route_res, str):
            try:
                route_res = json.loads(route_res)
            except Exception:
                pass

        if not isinstance(route_res, dict):
            raise RuntimeError("Rota verisi alınamadı")

        # RouteResponse model key'leri: polyline, distance_km, duration_min
        polyline = (
            route_res.get("polyline")
            or route_res.get("polyline_encoded")
            or ""
        )
        total_km = float(
            route_res.get("distance_km")
            or route_res.get("mesafe_km")
            or 0
        )
        total_min = int(
            route_res.get("duration_min")
            or route_res.get("sure_dk")
            or 0
        )

        # Polyline Redis'e kaydet
        if orchestrator.redis_client and polyline:
            orchestrator.redis_client.setex(f"route:{session_id}", 3600, polyline)

        # ── ETA hesapla (TR saati — 10. Tur fix) ─────────────────────────
        from datetime import timedelta
        _now = _now_tr()
        _eta_dt = _now + timedelta(minutes=total_min)
        eta_str = _eta_dt.strftime("%H:%M")
        eta_display = (
            f"{eta_str} ({_eta_dt.strftime('%d %b')})"
            if _eta_dt.date() > _now.date()
            else eta_str
        )

        # ── 4. Marker değişkenleri (curator akışı 8.5'te dolduracak) ─────
        # break_interval_km sadece trip_context'e gidiyor (mobile bilgi için)
        break_interval_km = request.break_interval_hours * 80  # ~80km/saat
        food_markers: list = []
        fuel_markers: list = []
        break_markers: list = []
        combined_markers: list = []
        scenic_markers: list = []
        fuel_section_summary: dict = {}
        # Legacy fraction map — narrative & POI overlay subtitle için
        _FRACTION_MAP = {"Başları": 0.2, "Ortaları": 0.5, "Sonları": 0.8}
        food_loc_raw = (request.food_location or "").strip()
        food_city: Optional[str] = (
            food_loc_raw if (
                food_loc_raw and food_loc_raw not in (*_FRACTION_MAP.keys(), "Fark etmez", "")
            ) else None
        )
        food_target_km = total_km * _FRACTION_MAP.get(food_loc_raw, 0.5)

        # ── 7. Hava durumu — rota boyunca 40km aralıklarla analiz ────────
        weather_warnings: list = []
        weather_details: list = []  # tüm km-bazlı kontrol noktaları (genişletilebilir kart)
        _weather_analyzed = False
        if total_km >= 50 and polyline:
            weather_tool = orchestrator.get_tool_by_name("analyze_route_weather")
            if not weather_tool:
                log.warning("⚠️ [TripPlan] analyze_route_weather tool bulunamadı")
            else:
                # 13. Tur — custom_note'tan yola çıkış saatini çıkar
                _dep_min = _parse_departure_minutes(
                    request.custom_note, _now_tr(),
                )
                if _dep_min > 0:
                    log.info(
                        f"⏰ [TripPlan] custom_note → departure_minutes_from_now={_dep_min} "
                        f"({_dep_min/60:.1f}sa sonra)"
                    )
                try:
                    w_res = await asyncio.wait_for(
                        weather_tool.ainvoke({
                            "polyline": polyline,
                            "avg_speed_kmh": "90",
                            "departure_minutes_from_now": str(_dep_min),
                        }),
                        timeout=20.0,
                    )
                    if isinstance(w_res, str):
                        try:
                            w_res = json.loads(w_res)
                        except Exception:
                            w_res = {}
                    if isinstance(w_res, dict):
                        risk = w_res.get("risk_durumu", "BİLİNMİYOR")
                        zones = w_res.get("riskli_bolgeler", [])
                        _weather_analyzed = True
                        # Tüm km noktalarını sakla — genişletilebilir kart için
                        for pt in w_res.get("detayli_ozet", []):
                            if isinstance(pt, dict):
                                weather_details.append({
                                    "km": pt.get("km"),
                                    "km_int": pt.get("km_int"),                  # 12. Tur
                                    "saat": pt.get("tahmini_saat") or pt.get("saat"),
                                    "durum": pt.get("durum"),
                                    "sicaklik": pt.get("sicaklik"),
                                    "yagis_olasiligi": pt.get("yagis_olasiligi"),
                                    "yagis_pct": pt.get("yagis_pct"),            # 12. Tur
                                    "ruzgar": pt.get("ruzgar"),
                                    "ruzgar_ms": pt.get("ruzgar_ms"),            # 12. Tur
                                    "riskli_mi": bool(pt.get("riskli_mi")),
                                    "severity": pt.get("severity", "acik"),      # 12. Tur
                                    "intensity_pct": pt.get("intensity_pct", 0), # 12. Tur
                                    "emoji": pt.get("emoji", "🌤️"),              # 12. Tur
                                    "il": pt.get("il"),                          # 13. Tur
                                    "ilce": pt.get("ilce"),                      # 13. Tur
                                    "location_label": pt.get("location_label"),  # 13. Tur
                                    "lat": pt.get("lat"),                        # 14. Tur — mini map
                                    "lon": pt.get("lon"),                        # 14. Tur
                                })
                        log.info(f"🌦️ [TripPlan] Hava analizi: risk={risk}, {len(zones)} riskli bölge, {len(weather_details)} kontrol noktası")
                        for zone in zones:
                            if isinstance(zone, dict):
                                temp_str = zone.get("sicaklik", "")
                                rain_str = zone.get("yagis_olasiligi", "")
                                detail = f" {temp_str}" if temp_str else ""
                                if rain_str:
                                    detail += f", yağış olasılığı {rain_str}"
                                weather_warnings.append({
                                    "location": zone.get("km", "Rota"),
                                    "condition": zone.get("durum", ""),
                                    "temperature": temp_str,
                                    "rain_probability": rain_str,
                                    "severity": "warning",
                                    "message": f"Dikkat: {zone.get('durum', 'kötü hava')}{detail}",
                                })
                            elif isinstance(zone, str):
                                weather_warnings.append({
                                    "location": "Rota",
                                    "condition": zone,
                                    "severity": "warning",
                                    "message": zone,
                                })
                        # Risk varsa ama riskli_bolgeler boşsa genel özetten al
                        if not weather_warnings and risk not in ("DÜŞÜK", "YOK", "BİLİNMİYOR"):
                            for pt in w_res.get("detayli_ozet", [])[:3]:
                                temp_str = pt.get("sicaklik", "?")
                                rain_str = pt.get("yagis_olasiligi", "")
                                detail = f" {temp_str}" if temp_str else ""
                                if rain_str:
                                    detail += f", yağış olasılığı {rain_str}"
                                weather_warnings.append({
                                    "location": pt.get("km", "Rota"),
                                    "condition": pt.get("durum", ""),
                                    "temperature": temp_str,
                                    "rain_probability": rain_str,
                                    "severity": "warning" if pt.get("riskli_mi") else "info",
                                    "message": f"{pt.get('durum', 'Hava koşullarını takip edin.')}{detail}",
                                })
                    else:
                        log.warning(f"⚠️ [TripPlan] Hava analizi beklenmeyen format: {type(w_res)}")
                except asyncio.TimeoutError:
                    log.warning("⚠️ [TripPlan] Hava analizi timeout (20s)")
                except Exception as exc:
                    log.warning(f"⚠️ [TripPlan] Hava analizi hata: {exc}")

        # Hava analizi tamamlandı, risk yoksa açık hava bildirimi ekle
        if _weather_analyzed and not weather_warnings:
            weather_warnings.append({
                "location": "Tüm Rota",
                "condition": "Açık",
                "severity": "info",
                "message": "☀️ Rota boyunca hava açık, güvenli yolculuklar!",
            })

        # İl-bazlı zone özeti (mobile chip listesi + curator narrative için)
        weather_zones_summary = _summarize_weather_by_city(weather_details)
        if weather_zones_summary:
            log.info(
                f"🌤️ [TripPlan] Hava zonları: "
                f"{', '.join(z['cities'] + ':' + z['severity'] for z in weather_zones_summary[:6])}"
            )

        # ── 7.5 Radar noktaları ───────────────────────────────────────────
        radar_count = 0
        if polyline and total_km >= 50:
            radar_tool = orchestrator.get_tool_by_name("get_route_radars")
            if radar_tool:
                try:
                    r_res = await asyncio.wait_for(
                        radar_tool.ainvoke({"route_polyline": polyline}),
                        timeout=15.0,
                    )
                    if isinstance(r_res, str):
                        try:
                            r_res = json.loads(r_res)
                        except Exception:
                            r_res = {}
                    if isinstance(r_res, dict):
                        radar_count = r_res.get("radar_count", 0) or len(r_res.get("radars", []))
                        log.info(f"📸 [TripPlan] Radar: {radar_count} nokta bulundu")
                except Exception as exc:
                    log.warning(f"⚠️ [TripPlan] Radar araması hata: {exc}")

        # ── 8. Ücretli geçiş hesapla ─────────────────────────────────────
        toll_info = None
        if total_km > 50 and polyline:
            toll_tool = orchestrator.get_tool_by_name("get_toll_for_route")
            if toll_tool:
                try:
                    toll_res = await asyncio.wait_for(
                        toll_tool.ainvoke({"route_polyline": polyline}),
                        timeout=15.0,
                    )
                    if isinstance(toll_res, str):
                        try:
                            toll_res = json.loads(toll_res)
                        except Exception:
                            toll_res = {}
                    if isinstance(toll_res, dict) and toll_res.get("toll_count", 0) > 0:
                        toll_info = {
                            "total_tl": toll_res.get("total_toll_cost_tl"),
                            "count": toll_res.get("toll_count"),
                            "details": toll_res.get("tolls", [])[:5],
                        }
                except Exception:
                    pass

        # ── 8.5 ★ Strategist v2 — LLM tek çağrısı: plan + narrative ─────────
        # LLM Türkiye coğrafyasını biliyor: rota üzerindeki ilçeleri kendisi
        # tahmin eder, kullanıcı tercihlerine göre 4-6 stop üretir ve uzun
        # imperative narrative yazar (placeholder'lı). Backend sonra her stop'u
        # paralel olarak gerçek mekana eşler ve narrative'i replace eder.
        curated_narrative: Optional[str] = None
        _use_strategist = _os_cur.getenv("TRIP_USE_STRATEGIST", "true").lower() == "true"
        # Legacy curator default OFF — Strategist fail olursa SIMPLE deterministic akış kullanılır
        _use_legacy_curator = _os_cur.getenv("TRIP_USE_LEGACY_CURATOR", "false").lower() == "true"
        _strategy_ok = False
        if _use_strategist and total_km > 50 and polyline:
            try:
                _effective_fuel_km = (
                    request.fuel_remaining_km if request.fuel_remaining_km > 0
                    else (40 / fuel_consumption) * 100
                )
                _intent_payload = {
                    "food_preference": request.food_preference,
                    "food_location": request.food_location,
                    "food_specific": (request.food_specific or "").strip(),
                    "food_quality_hint": (request.food_quality_hint or "").strip(),
                    "scene_filters": list(request.scene_filters or []),
                    "custom_note": request.custom_note,
                    "break_interval_hours": request.break_interval_hours,
                    "waypoints": request.waypoint_labels or request.waypoints,
                }
                strategy = await _plan_strategy(
                    origin=origin,
                    destination=request.destination,
                    total_km=total_km,
                    total_min=total_min,
                    eta_display=eta_display,
                    fuel_type=fuel_type,
                    fuel_remaining_km=_effective_fuel_km,
                    current_lat=request.current_lat,
                    current_lon=request.current_lon,
                    intent=_intent_payload,
                    weather_zones=weather_zones_summary or [],
                    orchestrator=orchestrator,
                )
                if strategy and strategy.get("stops"):
                    resolved = await _collect_targeted_stops(
                        stops=strategy["stops"],
                        polyline=polyline,
                        total_km=total_km,
                        current_lat=request.current_lat,
                        current_lon=request.current_lon,
                        fuel_type=fuel_type,
                        orchestrator=orchestrator,
                    )
                    if resolved:
                        curated_narrative = _inject_stop_names(
                            strategy.get("narrative", ""), resolved
                        )
                        for c in resolved:
                            role = c.get("role")
                            if role == "fuel":
                                fuel_markers.append(c)
                            elif role == "food":
                                food_markers.append(c)
                            elif role == "scenic":
                                scenic_markers.append(c)
                            else:
                                break_markers.append(c)
                        _strategy_ok = True
                        log.info(
                            f"✅ [TripPlan] Strategist akışı: "
                            f"{len(fuel_markers)} fuel, {len(food_markers)} food, "
                            f"{len(break_markers)} break, {len(scenic_markers)} scenic"
                        )
                    else:
                        log.warning(
                            "⚠️ [TripPlan] Strategist stop'lar resolve edilemedi — "
                            "fallback'e düşülüyor"
                        )
                else:
                    log.warning(
                        "⚠️ [TripPlan] Strategist None döndü — fallback'e düşülüyor"
                    )
            except Exception as _strex:
                import traceback as _stb
                log.error(
                    f"🔥 [TripPlan] Strategist akışı hata: {_strex}\n{_stb.format_exc()}"
                )

        # ── 8.6 ★ SIMPLE Deterministic fallback (Strategist fail) ─────────
        # LegacyCurator default OFF — yerine basit, hızlı, doğrudan
        # search_hybrid_places çağrılarıyla stop'lar üretilir.
        if not _strategy_ok and not _use_legacy_curator and total_km > 50 and polyline:
            try:
                simple_resolved = await _simple_det_stops(
                    custom_note=request.custom_note or "",
                    food_preference=request.food_preference,
                    food_specific=(request.food_specific or "").strip(),
                    food_quality_hint=(request.food_quality_hint or "").strip(),
                    food_location=request.food_location or "Ortaları",
                    scene_filters=list(request.scene_filters or []),
                    break_interval_hours=request.break_interval_hours,
                    destination=request.destination,
                    polyline=polyline,
                    total_km=total_km,
                    current_lat=request.current_lat,
                    current_lon=request.current_lon,
                    fuel_type=fuel_type,
                    orchestrator=orchestrator,
                )
                if simple_resolved:
                    curated_narrative = _build_simple_narrative(
                        origin=origin,
                        destination=request.destination,
                        total_km=total_km,
                        total_min=total_min,
                        eta_display=eta_display,
                        resolved=simple_resolved,
                        custom_note=request.custom_note or "",
                        food_specific=(request.food_specific or "").strip(),
                        weather_zones=weather_zones_summary or [],
                    )
                    for c in simple_resolved:
                        role = c.get("role")
                        if role == "fuel":
                            fuel_markers.append(c)
                        elif role == "food":
                            food_markers.append(c)
                        elif role == "scenic":
                            scenic_markers.append(c)
                        else:
                            break_markers.append(c)
                    log.info(
                        f"✅ [TripPlan] SimpleDet akışı: "
                        f"{len(fuel_markers)} fuel, {len(food_markers)} food, "
                        f"{len(break_markers)} break"
                    )
            except Exception as _sde:
                import traceback as _stb2
                log.error(
                    f"🔥 [TripPlan] SimpleDet hata: {_sde}\n{_stb2.format_exc()}"
                )

        # ── 8.7 Legacy Curator (sadece TRIP_USE_LEGACY_CURATOR=true ise) ───
        if not _strategy_ok and _use_legacy_curator and total_km > 50 and polyline:
            try:
                _req_dict = request.model_dump()
                _req_dict["total_km"] = total_km
                _raw_plan = _req_dict.get("search_plan") or _build_legacy_search_plan(request)
                _effective_fuel_km = (
                    request.fuel_remaining_km if request.fuel_remaining_km > 0
                    else (40 / fuel_consumption) * 100
                )
                pools = await _collect_candidates(
                    search_plan=_raw_plan,
                    polyline=polyline,
                    total_km=total_km,
                    origin=origin,
                    destination=request.destination,
                    fuel_type=fuel_type,
                    fuel_range=_effective_fuel_km,
                    orchestrator=orchestrator,
                    break_filter=_is_valid_break_stop,
                )
                fuel_section_summary = pools.fuel_summary or {}
                _fuel_anchors = _parse_fuel_anchors(request.custom_note)
                _anchor_str = (
                    "start" if _fuel_anchors["start"]
                    else ("end" if _fuel_anchors["end"] else None)
                )
                _route_meta = {
                    "origin": origin,
                    "destination": request.destination,
                    "total_km": round(total_km, 1),
                    "total_min": total_min,
                    "eta_display": eta_display,
                    "fuel_type": fuel_type,
                    "radar_count": radar_count,
                    "toll": toll_info,
                }
                _intent_payload = {
                    "food_preference": request.food_preference,
                    "food_location": request.food_location,
                    "food_specific": (request.food_specific or "").strip(),
                    "food_quality_hint": (request.food_quality_hint or "").strip(),
                    "scene_filters": list(request.scene_filters or []),
                    "custom_note": request.custom_note,
                    "fuel_anchor": _anchor_str,
                    "break_interval_hours": request.break_interval_hours,
                    "waypoints": request.waypoint_labels or request.waypoints,
                }
                curated = await _curate_trip(
                    pools=pools,
                    route_meta=_route_meta,
                    intent=_intent_payload,
                    weather_zones=weather_zones_summary or [],
                    orchestrator=orchestrator,
                )
                if curated:
                    curated_narrative = curated.get("narrative")
                    _selected_ids = [s["id"] for s in curated.get("selected_stops", [])]
                    _selected = _apply_marker_budget(pools, _selected_ids)
                else:
                    _selected = _det_select_pools(pools, _req_dict)
                    log.info(
                        f"📊 [TripPlan] Legacy fallback deterministik: "
                        f"{len(_selected)} marker"
                    )
                for _c in _selected:
                    kd = _c.get("kind") or "food"
                    if kd == "combined":
                        _c["type"] = "combined_stop"
                        combined_markers.append(_c)
                    elif kd == "fuel":
                        _c["type"] = "fuel_station"
                        fuel_markers.append(_c)
                    elif kd == "food":
                        _c["type"] = "restaurant"
                        food_markers.append(_c)
                    elif kd == "scenic":
                        _c["type"] = "scenic_stop"
                        scenic_markers.append(_c)
                    else:
                        _c["type"] = "break_stop"
                        break_markers.append(_c)
            except Exception as _curex:
                import traceback as _ctb
                log.error(
                    f"🔥 [TripPlan] Legacy curator hata: {_curex}\n{_ctb.format_exc()}"
                )

        # ── 9. POI Overlay oluştur ────────────────────────────────────────
        # NOT: break_markers (mola noktaları) da haritada görünmeli — eskiden
        # eksikti, kullanıcı 2sa interval verdiğinde nokta gözükmüyordu.
        all_markers = (
            food_markers + fuel_markers + break_markers
            + combined_markers + scenic_markers
        )
        poi_overlay = None
        sections = []

        if food_markers:
            food_cards = []
            # Rota km'sine en yakın 5 mekanı al, deviation'a göre de sırala
            food_markers_sorted = sorted(
                food_markers,
                key=lambda m: (
                    abs(float(m.get("distance_along_route_km") or 0) - food_target_km),
                    m.get("deviation_meters") or 9999
                )
            )[:5]
            for i, m in enumerate(food_markers_sorted):
                if not isinstance(m, dict) or "lat" not in m:
                    continue
                dev = m.get("deviation_meters")
                if dev is None:
                    dev = 9999
                extra_min = round(dev / 500) if dev > 400 and dev != 9999 else 0
                rating = m.get("rating")
                is_open = m.get("open_now")
                dist_along = m.get("distance_along_route_km") or 0
                name = m.get("name", "Restoran")

                # AI öneri açıklaması üret
                rec_parts = []
                if dev <= 400:
                    rec_parts.append("Yol üstünde")
                elif dev < 2000:
                    rec_parts.append(f"{int(dev)}m sapmayı hak ediyor")
                if rating and float(rating) >= 4.0:
                    rec_parts.append(f"{rating}/5 puan ile çok beğenilen")
                elif rating and float(rating) >= 3.5:
                    rec_parts.append(f"{rating}/5 puan ile güvenilir")
                if is_open is True:
                    rec_parts.append("varışında açık olacak")
                elif is_open is False:
                    rec_parts.append("⚠️ varışta kapalı olabilir")
                if int(dist_along) > 0:
                    rec_parts.append(f"rotanın ~{int(dist_along)}. km'sinde")
                ai_rec = (f"{name} — " + ", ".join(rec_parts) + ".") if rec_parts else None

                food_cards.append(PoiOverlayCard(
                    id=f"food_{i}_{str(m.get('lat',''))[:6]}",
                    name=name,
                    address=m.get("address") or m.get("snippet"),
                    category="restoran",
                    lat=float(m["lat"]), lon=float(m["lon"]),
                    deviation_meters=dev if dev < 9999 else None,
                    distance_along_route_km=m.get("distance_along_route_km"),
                    extra_time_min=extra_min,
                    eta=m.get("eta"),
                    rating=rating,
                    review_count=m.get("review_count"),
                    price_level=m.get("price_level"),
                    is_open=is_open,
                    open_now=is_open,
                    opening_hours=m.get("opening_hours"),
                    phone=m.get("phone"),
                    deviation_label="Yol üstü ✅" if dev <= 400 else f"{dev}m sapma ⚠️",
                    route_impact_label=f"+{extra_min} dk" if extra_min else "Sıfır ek süre",
                    is_recommended=(i == 0),
                    recommendation_reason="Rota üstü en iyi seçenek" if i == 0 else None,
                    ai_recommendation=ai_rec,
                ))
            if food_cards:
                # 14. Tur — "Rotayı dörde böldük" metni temizlendi.
                # food_city varsa şehir merkezi, chip varsa bölge etiketi göster.
                if food_city:
                    _food_subtitle = f"{food_city} civarında {len(food_cards)} mekan"
                else:
                    _region_label_map = {
                        "Başları": "yolun başlarında",
                        "Ortaları": "rotanın ortalarında",
                        "Sonları": "yolun sonlarında",
                    }
                    _region = _region_label_map.get(
                        food_loc_raw, "rota üzerinde"
                    )
                    _food_subtitle = f"{_region} {len(food_cards)} mekan"
                sections.append({
                    "type": "food",
                    "title": f"🍽️ {request.food_preference} Önerileri",
                    "subtitle": _food_subtitle,
                    "target_km": int(food_target_km),
                    "cards": [c.model_dump() for c in food_cards],
                })

        if fuel_markers:
            fuel_cards = []
            # Önce fiyata göre sırala, fiyat yoksa sapmaya göre.
            # NOT: yeni ilçe-bazlı algoritmada fuel_price dict her zaman var ama
            # price_per_liter None olabilir (sadece price_label seti) — None karşılaştırması
            # yapma, 9999 fallback'ine düş.
            def _fuel_sort_key(m):
                fp = m.get("fuel_price") or {}
                ppl = fp.get("price_per_liter")
                has_price = isinstance(ppl, (int, float))
                dev = m.get("deviation_meters")
                if not isinstance(dev, (int, float)):
                    dev = 9999
                return (
                    0 if has_price else 1,
                    ppl if has_price else 9999,
                    dev,
                )
            fuel_markers_sorted = sorted(fuel_markers, key=_fuel_sort_key)
            for i, m in enumerate(fuel_markers_sorted):
                if not isinstance(m, dict) or "lat" not in m:
                    continue
                dev = m.get("deviation_meters")
                if dev is None:
                    dev = 9999
                fp = m.get("fuel_price")
                price_label = None
                name = m.get("name", "Benzin İstasyonu")

                if fp:
                    price_label = f"{fp.get('company', '')}: {fp.get('price_per_liter', '?')} TL/L"

                # AI öneri açıklaması üret
                rec_parts = []
                if fp and fp.get("price_per_liter"):
                    rec_parts.append(f"{fp['price_per_liter']} TL/L ile {fp.get('company', 'istasyon')}'dan doldurun")
                if dev <= 400:
                    rec_parts.append("yol üstünde")
                elif dev < 2000:
                    rec_parts.append(f"{int(dev)}m sapma ile ulaşılabilir")
                dist_along = m.get("distance_along_route_km") or 0
                if int(dist_along) > 0:
                    rec_parts.append(f"rotanın {int(dist_along)}. km'sinde")
                ai_rec = " — ".join(rec_parts).capitalize() + "." if rec_parts else None

                fuel_cards.append(PoiOverlayCard(
                    id=f"fuel_{i}_{str(m.get('lat',''))[:6]}",
                    name=name,
                    address=m.get("address") or m.get("snippet"),
                    category="benzin_istasyonu",
                    lat=float(m["lat"]), lon=float(m["lon"]),
                    deviation_meters=dev if dev < 9999 else None,
                    distance_along_route_km=m.get("distance_along_route_km"),
                    rating=m.get("rating"),
                    is_open=m.get("open_now"),
                    open_now=m.get("open_now"),
                    deviation_label="Yol üstü ✅" if dev <= 400 else f"{dev}m sapma ⚠️",
                    route_impact_label="Sıfır ek süre" if dev <= 400 else f"+{round(dev/500)} dk",
                    is_recommended=(i == 0),
                    fuel_price_info=fp,
                    recommendation_reason=price_label or "En uygun istasyon",
                    ai_recommendation=ai_rec,
                ))
            if fuel_cards:
                sections.append({
                    "type": "fuel",
                    "title": "⛽ Yakıt Durakları",
                    "subtitle": f"Menzilini aşmadan önce doldurun — {int(request.fuel_remaining_km)} km menzil kaldı",
                    "cards": [c.model_dump() for c in fuel_cards],
                })

        # Combined POI section — hem yakıt hem yemek/mola olan yerler
        if combined_markers:
            combined_cards = []
            for i, m in enumerate(combined_markers):
                if not isinstance(m, dict) or "lat" not in m:
                    continue
                caps = m.get("sub_capabilities") or []
                cap_label = " + ".join(c.title() for c in caps)
                dist_along = m.get("distance_along_route_km") or 0
                combined_cards.append(PoiOverlayCard(
                    id=f"combined_{i}_{str(m.get('lat',''))[:6]}",
                    name=m.get("name", "Dinlenme Tesisi"),
                    address=m.get("address"),
                    category="combined_stop",
                    lat=float(m["lat"]), lon=float(m["lon"]),
                    deviation_meters=m.get("deviation_meters"),
                    distance_along_route_km=m.get("distance_along_route_km"),
                    rating=m.get("rating"),
                    review_count=m.get("review_count"),
                    deviation_label="Yol üstü kombine ⭐",
                    is_recommended=(i == 0),
                    recommendation_reason=f"{cap_label} — tek durakta hepsi",
                    ai_recommendation=(
                        f"{m.get('name')} (~{int(dist_along)}. km) — {cap_label}. "
                        f"Tek durakta hem dol hem ye."
                    ),
                ))
            if combined_cards:
                sections.append({
                    "type": "combined",
                    "title": "⭐ Tek Durakta Kombine",
                    "subtitle": f"{len(combined_cards)} dinlenme tesisi — yakıt + yemek bir arada",
                    "cards": [c.model_dump() for c in combined_cards],
                })

        # Scenic (manzara/sahil/doğa) section
        if scenic_markers:
            scenic_cards = []
            for i, m in enumerate(scenic_markers):
                if not isinstance(m, dict) or "lat" not in m:
                    continue
                dist_along = m.get("distance_along_route_km") or 0
                scenic_cards.append(PoiOverlayCard(
                    id=f"scenic_{i}_{str(m.get('lat',''))[:6]}",
                    name=m.get("name", "Manzara Noktası"),
                    address=m.get("address"),
                    category="scenic_stop",
                    lat=float(m["lat"]), lon=float(m["lon"]),
                    deviation_meters=m.get("deviation_meters"),
                    distance_along_route_km=m.get("distance_along_route_km"),
                    rating=m.get("rating"),
                    review_count=m.get("review_count"),
                    is_recommended=(i == 0),
                    recommendation_reason="Manzaralı mola",
                    ai_recommendation=f"{m.get('name')} (~{int(dist_along)}. km) — manzaralı kısa mola.",
                ))
            if scenic_cards:
                sections.append({
                    "type": "scenic",
                    "title": "📸 Manzaralı Mola Noktaları",
                    "subtitle": f"{len(scenic_cards)} öneri",
                    "cards": [c.model_dump() for c in scenic_cards],
                })

        # Mola noktaları kullanıcıya SEÇTİRİLMEZ — biz öneririz, harita üzerinde
        # zaten görünüyor (all_markers'a dahil edildi). POI seçim ekranında
        # ayrı bir section çıkmıyor — kullanıcı "şu mı bu mu" diye uğraşmasın.

        if sections or all_markers:
            poi_overlay = PoiOverlay(
                mode="trip_plan",
                title=f"🗺️ {request.destination} Yolculuk Planı",
                subtitle=f"{int(total_km)} km · ~{total_min // 60}sa {total_min % 60}dk",
                cards=[],
                primary_action="Navigasyonu Başlat",
                secondary_action="Planı Düzenle",
                route_summary={
                    "total_km": total_km,
                    "total_min": total_min,
                    "eta": eta_str,
                    "eta_display": eta_display,
                    "food_stops": len(food_markers),
                    "fuel_stops": len(fuel_markers),
                    "combined_stops": len(combined_markers),
                    "fuel_summary": fuel_section_summary,
                    "toll": toll_info,
                    "radar_count": radar_count,
                },
                weather_warnings=weather_warnings if weather_warnings else None,
                weather_details=weather_details if weather_details else None,
                weather_zones_summary=weather_zones_summary if weather_zones_summary else None,
                sections=sections if sections else None,
            )

        # ── 9. Trip context Redis'e kaydet ───────────────────────────────
        # ★ Markerları da Redis'e yaz — chat akışında durak ekleme yapıldığında
        # eski food/fuel/break markers kaybolmasın (chat response sadece yeni
        # waypoint marker döndürür; merge için kaynağa ihtiyaç var).
        _persistable_markers = []
        for _src in (fuel_markers, food_markers, break_markers,
                     combined_markers, scenic_markers):
            for _m in _src:
                if not isinstance(_m, dict) or "lat" not in _m:
                    continue
                _persistable_markers.append({
                    "lat": float(_m["lat"]),
                    "lon": float(_m["lon"]),
                    "title": _m.get("name") or _m.get("title"),
                    "type": _m.get("type", "poi"),
                    "snippet": _m.get("address") or _m.get("snippet"),
                    "rating": _m.get("rating"),
                    "deviation_meters": _m.get("deviation_meters"),
                    "distance_along_route_km": _m.get("distance_along_route_km"),
                    "fuel_price": _m.get("fuel_price"),
                    "open_now": _m.get("open_now"),
                })

        # ★ FIX 5: origin koord ise friendly name çıkar (mobile UI'da koord göstermesin)
        origin_friendly = origin
        if origin and "," in origin:
            try:
                _lat_o, _lon_o = map(float, origin.split(","))
                import httpx as _httpx_o
                async with _httpx_o.AsyncClient(
                    headers={"User-Agent": "GeoIntel_Orchestrator/4.0"}
                ) as _nom_client:
                    _rg = await _reverse_geocode_district(_lat_o, _lon_o, _nom_client)
                if _rg and _rg.get("city"):
                    _district = _rg.get("district") or ""
                    _city = _rg.get("city") or ""
                    origin_friendly = (
                        f"{_district}, {_city}".strip(", ") if _district else _city
                    )
                else:
                    origin_friendly = "Mevcut Konum"
            except Exception:
                origin_friendly = "Mevcut Konum"

        trip_context = {
            "origin": origin,
            "origin_friendly": origin_friendly,  # ★ mobile bunu kullansın
            "destination": request.destination,
            "total_km": total_km,
            "total_min": total_min,
            "eta": eta_str,
            "eta_display": eta_display,
            "fuel_remaining_km": request.fuel_remaining_km,
            "fuel_type": fuel_type,
            "food_preference": request.food_preference,
            "food_specific": request.food_specific or "",
            "scene_filters": list(request.scene_filters or []),
            "break_interval_hours": request.break_interval_hours,
            "custom_note": request.custom_note,
            "waypoints": request.waypoints,
            "toll_info": toll_info,
            "weather_warnings": weather_warnings,
            "weather_details": weather_details,
            "weather_zones_summary": weather_zones_summary,
            "radar_count": radar_count,
            "break_interval_km": break_interval_km,
            "plan_markers": _persistable_markers,  # ★ ÖNERİ HAVUZU (POI sheet için)
            "selected_stops": [],  # ★ KULLANICININ ONAYLADIĞI DURAKLAR (başlangıçta boş)
                                   # add_stops endpoint VEYA chat'te "ekle" sonrası dolar
        }
        if orchestrator.redis_client:
            orchestrator.redis_client.setex(
                f"trip_ctx:{session_id}", 3600 * 8,
                json.dumps(trip_context, ensure_ascii=False)
            )

        # 9b: save_route_history zengin kayıt — narrative üretildikten sonra yapılır
        # (Aşağıda Adım 11'de _generate_trip_narrative sonrasında çağırıyoruz.)

        # ── 10. Tüm markerları harita için hazırla ──────────────────────
        import re as _re_wp
        _WP_COORD_RE = _re_wp.compile(r'^-?\d+\.?\d*,-?\d+\.?\d*$')
        waypoint_markers: List[MapMarker] = []
        for i, wp in enumerate(request.waypoints):
            if _WP_COORD_RE.match(wp.strip()):
                try:
                    wlat, wlon = map(float, wp.split(','))
                    label = (request.waypoint_labels[i]
                             if i < len(request.waypoint_labels) else f"Durak {i+1}")
                    waypoint_markers.append(MapMarker(
                        lat=wlat, lon=wlon,
                        title=label,
                        type="waypoint",
                        snippet="Ara durak",
                    ))
                except Exception:
                    pass

        map_markers: List[MapMarker] = list(waypoint_markers)
        for m in all_markers:
            if not isinstance(m, dict) or "lat" not in m:
                continue
            map_markers.append(MapMarker(
                lat=m["lat"], lon=m["lon"],
                title=m.get("name", m.get("title")),
                type=m.get("type", "poi"),
                snippet=m.get("address") or m.get("snippet"),
                poi_card={
                    "rating": m.get("rating"),
                    "deviation_meters": m.get("deviation_meters", 0),
                    "open_now": m.get("open_now"),
                    "fuel_price": m.get("fuel_price"),
                    "type": m.get("type", "poi"),
                },
            ))

        normalized_poly = _normalize_polyline(polyline)

        _severe_weather = any(w.get("severity") in ("warning", "critical") for w in weather_warnings)

        # ── LLM Anlatımı: Curator yazdıysa onu kullan, yoksa legacy üretici ──
        if curated_narrative:
            narrative_text = curated_narrative
        else:
            narrative_text = await _generate_trip_narrative(
                origin=origin,
                destination=request.destination,
                total_km=total_km,
                total_min=total_min,
                eta_display=eta_display,
                weather_warnings=weather_warnings,
                radar_count=radar_count,
                toll_info=toll_info,
                food_markers=food_markers,
                fuel_section_summary=fuel_section_summary,
                custom_note=request.custom_note,
                waypoint_labels=request.waypoint_labels,
                break_markers=break_markers,
                fuel_type=fuel_type,
            )

        # Fallback: LLM hata verirse mevcut deterministik özet
        if not narrative_text:
            note_part = f" Not: '{request.custom_note}'" if request.custom_note else ""
            toll_part = (
                f" Ücretli geçiş: yaklaşık {toll_info['total_tl']:.0f} TL ({toll_info['count']} nokta)."
                if toll_info else ""
            )
            weather_part = " ⚠️ Dikkat: rota üzerinde olumsuz hava koşulları var!" if _severe_weather else ""
            radar_part = f" 📸 Rota üzerinde {radar_count} radar noktası var." if radar_count else ""
            narrative_text = (
                f"Rotanı hazırladım! {request.destination}'a {int(total_km)} km, "
                f"yaklaşık {total_min // 60} saat {total_min % 60} dakika yol var. "
                f"Tahmini varış: {eta_display}.{toll_part}{weather_part}{radar_part}{note_part} "
                f"{'Güvenli yolculuklar!' if not _severe_weather else ''}"
            ).strip()

        reply = narrative_text

        # ── 11b. Geçmişe zengin kayıt (polyline + waypoint_labels + hava + narrative) ──
        # get_route_data zaten basic kayıt yaptı (mcp_client); bu UPDATE ile zenginleştirir.
        if user:
            try:
                from profile_manager import ProfileManager as _PM
                weather_summary_short = None
                if weather_warnings:
                    severe = [w for w in weather_warnings
                              if isinstance(w, dict) and w.get("severity") in ("warning", "critical")]
                    if severe:
                        weather_summary_short = severe[0].get("message") or severe[0].get("title")

                # Stops listesi: SADECE kullanıcının kesin tercihleri.
                # food_markers/fuel_markers/break_markers ÖNERİ — kullanıcı henüz
                # seçmedi, geçmiş rotalarda ara durak olarak görünmemeli. Bunlar
                # add_stops endpoint'inden geldiğinde kaydedilir (selected_stops).
                _enriched_stops: list = []
                _enriched_stops.append({
                    "kind": "origin",
                    "name": origin,
                    "address": origin,
                    "lat": None, "lon": None, "km": 0,
                })
                # Ara duraklar (sadece kullanıcının manuel girdiği waypoint'ler)
                import re as _re_st
                _WP_RE = _re_st.compile(r'^-?\d+\.?\d*,-?\d+\.?\d*$')
                for i, wp in enumerate(request.waypoints):
                    wp_stripped = wp.strip()
                    label = (request.waypoint_labels[i]
                             if i < len(request.waypoint_labels) else f"Durak {i+1}")
                    wplat = wplon = None
                    if _WP_RE.match(wp_stripped):
                        try:
                            wplat, wplon = map(float, wp_stripped.split(','))
                        except Exception:
                            pass
                    _enriched_stops.append({
                        "kind": "waypoint",
                        "name": label,
                        "address": wp,
                        "lat": wplat, "lon": wplon, "km": None,
                    })
                _enriched_stops.append({
                    "kind": "destination",
                    "name": request.destination,
                    "address": request.destination,
                    "lat": None, "lon": None,
                    "km": total_km,
                })
                # km'ye göre sırala (None'lar uçlarda kalır)
                _enriched_stops.sort(
                    key=lambda s: (
                        0 if s["kind"] == "origin" else (2 if s["kind"] == "destination" else 1),
                        s["km"] if isinstance(s.get("km"), (int, float)) else 0,
                    )
                )

                import asyncio as _asyncio
                _asyncio.create_task(_PM.save_route_history(
                    origin=origin,
                    destination=request.destination,
                    distance_km=total_km,
                    duration_min=total_min,
                    username=user["username"],
                    polyline_encoded=polyline,
                    waypoints=request.waypoints,
                    waypoint_labels=request.waypoint_labels,
                    weather_summary=weather_summary_short,
                    warnings=weather_warnings or None,
                    narrative=narrative_text,
                    stops=_enriched_stops,
                ))
            except Exception as _e:
                log.warning(f"⚠️ [TripPlan] Zengin kayıt başlatılamadı: {_e}")

        elapsed = int((time.monotonic() - t0) * 1000)

        # Action cards
        action_cards = [
            ActionCard(
                id="nav_start", label="Navigasyonu Başlat",
                action="ui:start_navigation", icon="🗺️", style="primary",
                is_ui_only=True,
            ),
            ActionCard(
                id="fuel_refine", label="Yakıt Analizi",
                action="Rota üzerindeki yakıt istasyonlarını analiz et",
                icon="⛽", style="secondary",
            ),
        ]
        if _severe_weather:
            action_cards.append(ActionCard(
                id="weather_detail", label="Hava Durumu Uyarısı",
                action="Rota boyunca hava durumunu detaylı göster",
                icon="🌦️", style="secondary",
            ))

        return ApiEnvelope(
            success=True,
            data=ChatResponse(
                status="completed",
                message=reply,
                intent={"category": "routing", "complexity": "high"},
                map=MapData(markers=map_markers, polyline=normalized_poly),
                action_cards=action_cards,
                poi_overlay=poi_overlay,
                distance_km=total_km,
                duration_min=total_min,
                trip_plan=trip_context,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    except Exception as e:
        import traceback as _tb
        log.error(f"🔥 [TripPlan] Hata: {e}\n{_tb.format_exc()}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="TRIP_PLAN_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )


# ─────────────────────────────────────────────────────────────────────────────
# TRIP ADD STOPS — Deterministik Durak Ekleme (LLM KULLANILMAZ)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/v1/trip/add_stops", response_model=ApiEnvelope, tags=["Trip Planning"])
async def add_stops_to_trip(request: TripAddStopsRequest, user: dict = Depends(get_optional_user)):
    """
    🛑 Seçilen durakları rotaya ekler — LLM YOK, tamamen deterministik.

    POI seçim ekranından "Rotama Ekle" basıldığında çağrılır.
    Seçilen mekanların koordinatlarını waypoint olarak get_route_data'ya inject eder.
    Sonuç: güncellenmiş polyline + mesafe/süre.
    """
    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    # ── GUARD: Serbest mod (chat_ prefix) bu endpoint'i çağıramaz ─────────
    # Free mode'da POI seçimi rotaya eklenmemeli — yeni rota olarak
    # değerlendirilmeli. Bu savunma hattı eski rotaya patch atılmasını engeller.
    raw_sid = request.session_id or ""
    if raw_sid.startswith("chat_"):
        log.warning(f"🚫 [AddStops] Chat session ({raw_sid}) add_stops çağrısı reddedildi")
        return ApiEnvelope(
            success=False,
            error=ApiError(
                code="ADD_STOPS_FORBIDDEN_IN_FREE_MODE",
                message="Serbest moddan rotaya ekleme yapılamaz. Yeni rota oluşturmak için akıllı rotaya geç.",
            ),
            metadata=ApiMetadata(response_time_ms=0, session_id=session_id),
        )

    log.info(f"🛑 [AddStops] session={session_id} | {len(request.selected_stops)} durak")

    try:
        # ── 1. Başlangıç koordinatını çöz ─────────────────────────────────
        origin = request.origin
        if not origin and request.current_lat and request.current_lon:
            origin = f"{request.current_lat},{request.current_lon}"
        if not origin and orchestrator.redis_client:
            cached_loc = orchestrator.redis_client.get(f"loc:{session_id}")
            if cached_loc:
                origin = cached_loc if isinstance(cached_loc, str) else cached_loc.decode("utf-8")
        if not origin:
            raise ValueError("Başlangıç konumu bulunamadı.")

        # ── 2. Hedefi ve eski waypointleri çöz ───────────────────────────
        destination = request.destination
        existing_waypoints = []
        tc = {}
        if orchestrator.redis_client:
            raw_tc = orchestrator.redis_client.get(f"trip_ctx:{session_id}")
            if raw_tc:
                tc = json.loads(raw_tc if isinstance(raw_tc, str) else raw_tc.decode("utf-8"))
                if not destination:
                    destination = tc.get("destination")
                # Eski waypoint'leri al
                old_wps = tc.get("waypoints")
                if old_wps:
                    if isinstance(old_wps, list):
                        existing_waypoints = old_wps
                    elif isinstance(old_wps, str):
                        existing_waypoints = old_wps.split("|")

        if not destination:
            raise ValueError("Hedef bilgisi bulunamadı. Önce rota planlayın.")

        # ── 3. Seçilen durakları (yemek + yakıt) km sırasına göre diz ───
        if not request.selected_stops:
            raise ValueError("Hiç durak seçilmedi.")

        import re as _re
        # Koordinat formatı: "lat,lon" — opsiyonel "!pt" suffix passthrough işareti
        _COORD_RE = _re.compile(r'^-?\d+\.?\d*,-?\d+\.?\d*(!pt)?$')

        def _strip_pt(w: str) -> str:
            return w[:-3] if w.endswith("!pt") else w

        existing_coord_waypoints = [
            _strip_pt(w.strip()) for w in existing_waypoints
            if isinstance(w, str) and _COORD_RE.match(w.strip())
        ]

        user_stops_sorted = sorted(
            request.selected_stops,
            key=lambda s: float(s.get("distance_along_route_km") or 0),
        )
        user_waypoints_with_dist = [
            (float(s.get("distance_along_route_km") or 0), f"{s['lat']},{s['lon']}", s)
            for s in user_stops_sorted
            if isinstance(s, dict) and "lat" in s and "lon" in s
        ]

        # ── 4. Otomatik mola araması (kullanıcıya sormadan) ────────────
        auto_break_stops: list = []
        break_interval_km_ctx = float(tc.get("break_interval_km") or (float(tc.get("break_interval_hours") or 2.0) * 80))
        total_km_ctx = float(tc.get("total_km") or 0)

        polyline_cached = None
        if orchestrator.redis_client:
            _cp = orchestrator.redis_client.get(f"route:{session_id}")
            if _cp:
                polyline_cached = _cp if isinstance(_cp, str) else _cp.decode("utf-8")

        if break_interval_km_ctx > 0 and total_km_ctx > break_interval_km_ctx and polyline_cached:
            selected_kms = [dist for dist, _, _ in user_waypoints_with_dist]
            break_slots = []
            curr = break_interval_km_ctx
            while curr < total_km_ctx - 30:
                if not any(abs(skm - curr) <= 50 for skm in selected_kms):
                    break_slots.append(curr)
                curr += break_interval_km_ctx

            if break_slots:
                places_tool_brk = orchestrator.get_tool_by_name("search_hybrid_places")
                if places_tool_brk:
                    _BRK_QUERIES = ["benzin istasyonu akaryakıt", "otoyol hizmet alanı mola", "kafe restoran mola"]
                    brk_task_list = []
                    for slot in break_slots:
                        for q in _BRK_QUERIES:
                            brk_task_list.append((
                                slot, q,
                                asyncio.wait_for(
                                    places_tool_brk.ainvoke({
                                        "query": q,
                                        "route_polyline": polyline_cached,
                                        "target_fraction": slot / max(total_km_ctx, 1),
                                    }),
                                    timeout=15.0,
                                ),
                            ))
                    brk_results = await asyncio.gather(*[t[2] for t in brk_task_list], return_exceptions=True)
                    seen_brk: set = set()
                    slot_best: dict = {}
                    for i, br in enumerate(brk_results):
                        if isinstance(br, Exception):
                            continue
                        if isinstance(br, str):
                            try:
                                br = json.loads(br)
                            except Exception:
                                continue
                        if isinstance(br, dict):
                            slot_key = brk_task_list[i][0]
                            if slot_key in slot_best:
                                continue
                            candidates = (
                                br.get("strict_route_places", [])
                                + br.get("relaxed_route_places", [])
                                + br.get("places", [])
                            )
                            for p in candidates[:10]:
                                nm = p.get("name", "")
                                if not nm or nm in seen_brk:
                                    continue
                                if "lat" not in p and "coords" in p:
                                    try:
                                        clat, clon = map(float, p["coords"].split(","))
                                        p["lat"] = clat
                                        p["lon"] = clon
                                    except Exception:
                                        continue
                                if "lat" not in p:
                                    continue
                                if not _is_valid_break_stop(p):
                                    log.debug(f"🚫 [AddStops] Mola reddedildi: {nm}")
                                    continue
                                seen_brk.add(nm)
                                p["type"] = "break_stop"
                                p["distance_along_route_km"] = slot_key
                                slot_best[slot_key] = p
                                break
                    auto_break_stops = list(slot_best.values())
                    log.info(f"☕ [AddStops] Otomatik mola: {len(auto_break_stops)} nokta eklendi (slots={break_slots})")

        # ── 4.5 Tüm waypoint'leri birleştir (13. Tur — passThrough KALDIRILDI)
        # Eski mantık: kullanıcı seçimleri "!pt" (HERE passThrough=true) — "buradan
        # geç ama durma" der. Pratikte HERE 2+ passThrough waypoint görünce
        # rotayı OTOYOL'a (Bolu/Ankara) çekiyordu → Karadeniz sahili kopuyordu.
        # Yeni: yemek/mola noktaları GERÇEK DURAK (driver duruyor) — !pt yok.
        # HERE sahil yolundaki en kısa yolu seçer.
        auto_with_dist = [
            (float(bs.get("distance_along_route_km") or 0), f"{bs['lat']},{bs['lon']}", bs)
            for bs in auto_break_stops
            if "lat" in bs and "lon" in bs
        ]
        all_stops_with_dist = [
            (d, w, s) for (d, w, s) in user_waypoints_with_dist
        ] + auto_with_dist
        all_stops_with_dist.sort(key=lambda x: x[0])

        new_waypoints = [w for _, w, _ in all_stops_with_dist]
        # Eski waypoint'lerden !pt marker'larını da SİL — passThrough yok
        existing_clean = [
            (w[:-3] if w.endswith("!pt") else w)
            for w in existing_coord_waypoints
        ]
        combined_waypoints = existing_clean + new_waypoints
        waypoints_str = "|".join(combined_waypoints)

        if not waypoints_str:
            raise ValueError("Geçerli koordinat bulunamadı.")

        # 13. Tur — Origin/waypoint sırası log'la (Ankara bug debug için)
        log.info(
            f"🗺️ [AddStops] origin={origin!r} dest={destination!r} "
            f"wp_sirali={[f'{d:.0f}km' for d, _, _ in all_stops_with_dist]} "
            f"toplam_wp={len(combined_waypoints)}"
        )

        # ── 5. Rotayı güncelle ────────────────────────────────────────────
        route_tool = orchestrator.get_tool_by_name("get_route_data")
        if not route_tool:
            raise RuntimeError("get_route_data tool bulunamadı.")

        route_args = {
            "origin": origin,
            "destination": destination,
            "waypoints": waypoints_str,
            "preference": "fastest",
        }

        log.info(f"🗺️ [AddStops] Rota güncelleniyor: {origin} → {destination} | wp={waypoints_str[:60]}")

        route_res = await asyncio.wait_for(route_tool.ainvoke(route_args), timeout=30.0)
        if isinstance(route_res, str):
            try:
                route_res = json.loads(route_res)
            except Exception:
                pass

        if not isinstance(route_res, dict):
            raise RuntimeError("Rota verisi alınamadı.")

        polyline = (
            route_res.get("polyline")
            or route_res.get("polyline_encoded")
            or ""
        )
        total_km = float(route_res.get("distance_km") or route_res.get("mesafe_km") or 0)
        total_min = int(route_res.get("duration_min") or route_res.get("sure_dk") or 0)

        # Güncel polyline'ı ve context'i Redis'e kaydet
        if orchestrator.redis_client:
            if polyline:
                orchestrator.redis_client.setex(f"route:{session_id}", 3600, polyline)
            if tc:
                tc["waypoints"] = combined_waypoints
                # ★ Kullanıcının POI sheet'ten seçtiği duraklar selected_stops'a
                # eklensin — RouteDetail "Duraklar" listesinde gözükecek
                _existing_sel = list(tc.get("selected_stops") or [])
                _sel_seen = {
                    (round(float(s.get("lat", 0)), 4),
                     round(float(s.get("lon", 0)), 4))
                    for s in _existing_sel if isinstance(s, dict)
                }
                for _us in (request.selected_stops or []):
                    if not isinstance(_us, dict) or "lat" not in _us:
                        continue
                    try:
                        _k = (round(float(_us["lat"]), 4),
                              round(float(_us["lon"]), 4))
                    except Exception:
                        continue
                    if _k in _sel_seen:
                        continue
                    _sel_seen.add(_k)
                    _existing_sel.append({
                        "lat": float(_us["lat"]),
                        "lon": float(_us["lon"]),
                        "title": _us.get("name") or _us.get("title"),
                        "type": _us.get("type", "poi"),
                        "snippet": _us.get("address") or _us.get("snippet"),
                        "rating": _us.get("rating"),
                        "distance_along_route_km": _us.get("distance_along_route_km"),
                        "added_via_poi_sheet": True,
                    })
                tc["selected_stops"] = _existing_sel
                orchestrator.redis_client.setex(f"trip_ctx:{session_id}", 3600 * 8, json.dumps(tc, ensure_ascii=False))

        # ── 6. Marker'ları hazırla (kullanıcı seçimleri + otomatik molalar) ──
        map_markers = []
        all_displayed_stops = [s for _, _, s in all_stops_with_dist]
        log.info(
            f"🗺️ [AddStops] HERE sonucu: {total_km:.0f}km, "
            f"polyline_len={len(polyline)}, expected_max ~600km for Karadeniz coast"
        )
        for i, s in enumerate(all_displayed_stops):
            if "lat" not in s or "lon" not in s:
                continue
            stype = s.get("type", "waypoint")
            map_markers.append(MapMarker(
                lat=s["lat"], lon=s["lon"],
                title=s.get("name", f"Durak {i+1}"),
                type=stype,
                snippet=s.get("address", "break_stop" if stype == "break_stop" else "Seçilen durak"),
            ))

        normalized_poly = _normalize_polyline(polyline)
        elapsed = int((time.monotonic() - t0) * 1000)

        # ── 7. Zengin yanıt metni (hava + radar dahil) ───────────────────
        weather_ctx = tc.get("weather_warnings") or []
        radar_count_ctx = int(tc.get("radar_count") or 0)
        _severe_ctx = any(w.get("severity") in ("warning", "critical") for w in weather_ctx)

        food_cnt = sum(1 for _, _, s in all_stops_with_dist if s.get("type") not in ("fuel_station", "break_stop"))
        fuel_cnt = sum(1 for _, _, s in all_stops_with_dist if s.get("type") == "fuel_station")
        brk_cnt = len(auto_break_stops)

        parts = []
        if food_cnt:
            parts.append(f"{food_cnt} yemek durağı")
        if fuel_cnt:
            parts.append(f"{fuel_cnt} yakıt durağı")
        if brk_cnt:
            parts.append(f"{brk_cnt} otomatik mola")
        stops_text = " ve ".join(parts) if parts else f"{len(request.selected_stops)} durak"

        weather_line = (
            "\n⚠️ Dikkat: Rota üzerinde olumsuz hava koşulları var, dikkatli sür!" if _severe_ctx
            else ("\n☀️ Hava durumu: Rota boyunca açık ve güvenli." if weather_ctx else "")
        )
        radar_line = f"\n📸 Rota üzerinde {radar_count_ctx} radar noktası var, dikkatli ol." if radar_count_ctx else ""

        reply = (
            f"Rotana {stops_text} eklendi!\n"
            f"Güncellenen rota: {int(total_km)} km, ~{total_min // 60}sa {total_min % 60}dk."
            f"{weather_line}{radar_line}"
        )

        # ── 8. Geçmiş rotaya zenginleştirilmiş stops kaydı ────────────────
        # SADECE kullanıcının seçtiği durakları + otomatik molaları persist et.
        # /trip/plan'da gösterilen öneri kartları (food_markers vb.) BURAYA gelmez —
        # kullanıcı seçim yapmazsa kayda da girmez.
        if user:
            try:
                from profile_manager import ProfileManager as _PM
                _enriched_stops: list = []
                _enriched_stops.append({
                    "kind": "origin", "name": origin, "address": origin,
                    "lat": None, "lon": None, "km": 0,
                })
                for _d, _w, _s in all_stops_with_dist:
                    if "lat" not in _s or "lon" not in _s:
                        continue
                    stype = _s.get("type", "waypoint")
                    kind = (
                        "fuel" if stype == "fuel_station"
                        else "rest" if stype == "break_stop"
                        else "food" if stype in ("poi", "food", "restaurant") and not _is_user
                        else "waypoint"
                    )
                    _enriched_stops.append({
                        "kind": kind,
                        "name": _s.get("name") or "Durak",
                        "address": _s.get("address") or _s.get("snippet"),
                        "lat": _s.get("lat"), "lon": _s.get("lon"),
                        "km": _s.get("distance_along_route_km"),
                    })
                _enriched_stops.append({
                    "kind": "destination", "name": destination, "address": destination,
                    "lat": None, "lon": None, "km": total_km,
                })

                import asyncio as _asyncio
                _asyncio.create_task(_PM.save_route_history(
                    origin=origin,
                    destination=destination,
                    distance_km=total_km,
                    duration_min=total_min,
                    username=user["username"],
                    polyline_encoded=polyline,
                    waypoints=combined_waypoints,
                    waypoint_labels=[s.get("name", "") for _, _, s in all_stops_with_dist],
                    weather_summary=None,
                    warnings=weather_ctx or None,
                    narrative=None,
                    stops=_enriched_stops,
                ))
            except Exception as _e:
                log.warning(f"⚠️ [AddStops] Zengin kayıt başlatılamadı: {_e}")

        return ApiEnvelope(
            success=True,
            data=ChatResponse(
                status="completed",
                message=reply,
                intent={"category": "routing", "complexity": "high"},
                map=MapData(
                    markers=map_markers,
                    polyline=normalized_poly,
                ),
                action_cards=[
                    ActionCard(
                        id="nav_start", label="Navigasyonu Başlat",
                        action="ui:start_navigation", icon="🗺️", style="primary",
                        is_ui_only=True,
                    ),
                ],
                distance_km=total_km,
                duration_min=total_min,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    except Exception as e:
        log.error(f"🔥 [AddStops] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="ADD_STOPS_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — Google Polyline 2-nokta encoder (day plan için)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# DAY PLAN ENDPOINT — Event-Anchored Günlük Plan
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/v1/day_plan", tags=["Day Planning"])
async def day_plan(request: DayPlanRequest, user: dict = Depends(get_optional_user)):
    """
    📅 Event-Anchored Günlük Plan

    Şehirdeki etkinlik/maç bilgisini anchor olarak kullanarak
    sabahtan akşama bağlantılı bir gün planı üretir.
    Toplu taşıma desteği YOK (araç / yürüyerek).
    Şehir opsiyonel — belirtilmezse mevcut konum merkez alınır.
    """
    import math as _math
    import uuid as _uuid

    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    # Aramalar için Redis route proxy'den kaçınmak için benzersiz session_id
    search_sid = f"dayplan:{_uuid.uuid4().hex[:12]}"

    city_label = request.city or "mevcut konum"
    log.info(f"📅 [DayPlan] session={session_id} | city={city_label} | date={request.date}")

    try:
        # ── 1. Tool referansları ─────────────────────────────────────────
        events_tool = orchestrator.get_tool_by_name("get_city_events")
        sports_tool = orchestrator.get_tool_by_name("get_sports_events")
        weather_tool = orchestrator.get_tool_by_name("get_weather")
        search_tool  = orchestrator.get_tool_by_name("search_hybrid_places")

        async def _safe_invoke(tool, args: dict, timeout: float = 15.0):
            if not tool:
                return None
            try:
                result = await asyncio.wait_for(tool.ainvoke(args), timeout=timeout)
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except Exception:
                        pass
                return result
            except Exception as exc:
                log.warning(f"[DayPlan] tool hatası: {exc}")
                return None

        # ── 2. Paralel: etkinlikler + maçlar + hava durumu ────────────────
        events_coro = (
            _safe_invoke(events_tool, {"city": request.city})
            if request.city and events_tool
            else asyncio.sleep(0)
        )
        events_res, sports_res, weather_res = await asyncio.gather(
            events_coro,
            _safe_invoke(sports_tool, {}),
            _safe_invoke(weather_tool, {"lat": request.city_lat, "lon": request.city_lon}, timeout=10.0),
        )

        # ── 3. Event/match parse ─────────────────────────────────────────
        events_list: list = []
        if isinstance(events_res, dict):
            events_list = events_res.get("data") or []
        elif isinstance(events_res, list):
            events_list = events_res

        sports_list: list = []
        if isinstance(sports_res, dict):
            sports_list = sports_res.get("data") or []
        elif isinstance(sports_res, list):
            sports_list = sports_res

        try:
            _dp = request.date.split("-")
            req_date_tr = f"{_dp[2]}.{_dp[1]}.{_dp[0]}"
            req_month_year = f"{_dp[1]}.{_dp[0]}"
        except Exception:
            req_date_tr = ""
            req_month_year = ""

        req_city_lower = request.city.lower()

        anchor_match = None
        for m in sports_list:
            m_time = m.get("time", "")
            m_city = m.get("city", "").lower()
            date_ok = req_date_tr and req_date_tr in m_time
            city_ok = (not req_city_lower) or (req_city_lower in m_city or m_city in req_city_lower)
            if date_ok and city_ok:
                anchor_match = m
                break

        anchor_event = None
        if not anchor_match:
            for ev in events_list:
                ev_date = ev.get("date", "")
                if not ev_date or ev_date in ("Belirtilmemiş", ""):
                    continue
                if req_date_tr in ev_date or req_month_year in ev_date:
                    anchor_event = ev
                    break

        event_anchor: dict | None = None
        if anchor_match:
            event_anchor = {
                "type": "sports",
                "title": anchor_match.get("match", "Maç"),
                "time": anchor_match.get("time", ""),
                "venue": anchor_match.get("stadium", ""),
                "city": anchor_match.get("city", city_label),
                "warning": anchor_match.get("warning", ""),
            }
        elif anchor_event:
            event_anchor = {
                "type": "event",
                "title": anchor_event.get("title", "Etkinlik"),
                "time": anchor_event.get("date", ""),
                "venue": anchor_event.get("venue", ""),
                "city": city_label,
                "link": anchor_event.get("link", ""),
            }

        # Ek etkinlikler — anchor dışında o gün için bulunan diğer event'ler.
        # Akşam slot'unda extra kartlar olarak gösterilir.
        extra_events: list[dict] = []
        for ev in events_list:
            if ev is anchor_event:
                continue
            ev_date = ev.get("date", "")
            if not ev_date or ev_date in ("Belirtilmemiş", ""):
                continue
            if req_date_tr in ev_date or (req_month_year and req_month_year in ev_date):
                extra_events.append({
                    "type": "event",
                    "title": ev.get("title", "Etkinlik"),
                    "time": ev.get("date", ""),
                    "venue": ev.get("venue", ""),
                    "link": ev.get("link", ""),
                })
            if len(extra_events) >= 3:
                break

        # ── 3b. TÜM o-gün etkinliklerini (üst banner için) topla ──────────
        # Kullanıcı "konser var mı, tiyatro var mı" diye sormadan otomatik
        # "bugün şehirde X etkinlik var, ilgileniyor musun?" diyebilelim.
        all_events: list[dict] = []
        for m in sports_list:
            m_time = m.get("time", "")
            m_city = m.get("city", "").lower()
            date_ok = req_date_tr and req_date_tr in m_time
            city_ok = (not req_city_lower) or (req_city_lower in m_city or m_city in req_city_lower)
            if date_ok and city_ok:
                all_events.append({
                    "type": "sports",
                    "title": m.get("match", "Maç"),
                    "time": m_time,
                    "venue": m.get("stadium", ""),
                    "city": m.get("city", city_label),
                })
        for ev in events_list:
            ev_date = ev.get("date", "")
            if not ev_date or ev_date in ("Belirtilmemiş", ""):
                continue
            if req_date_tr in ev_date or (req_month_year and req_month_year in ev_date):
                all_events.append({
                    "type": "event",
                    "title": ev.get("title", "Etkinlik"),
                    "time": ev_date,
                    "venue": ev.get("venue", ""),
                    "link": ev.get("link", ""),
                })
            if len(all_events) >= 8:  # üst banner'da aşırı kalabalık olmasın
                break

        # ── 4. Hava durumu özeti ─────────────────────────────────────────
        weather_summary = ""
        if isinstance(weather_res, dict):
            anlik = weather_res.get("ANLIK_DURUM") or weather_res
            if isinstance(anlik, dict):
                durum = anlik.get("durum") or anlik.get("condition") or ""
                temp  = anlik.get("sicaklik") or anlik.get("temperature") or ""
                if durum or temp:
                    weather_summary = f"{temp}°C, {durum}".strip(", ")

        # ── 5. Hedef nokta belirleme ─────────────────────────────────────
        # Anchor venue varsa onu hedef yap, yoksa şehir merkezi (yoksa mevcut konum)
        target_lat = request.city_lat
        target_lon = request.city_lon
        venue_name = ""
        if event_anchor and event_anchor.get("venue"):
            venue_name = event_anchor["venue"]

        # WALK modu: kullanıcı yürüyerek gezecek — şehir merkezine kadar
        # interpolation YAPMA, current konum etrafında kal. Slot çeşitliliği
        # için ufak offset (~500m) her slot için yeter.
        is_walk = request.transport_mode == "walk"
        if is_walk:
            target_lat = request.current_lat
            target_lon = request.current_lon
            # Akşam anchor (event venue) varsa onu koru, gerisinde current konum
            # (event venue genelde yürüme mesafesinde değil ama venue_name ile
            # search_hybrid_places location_name'i o noktayı bulur).

        # CAR modu: şehir merkezine kadar interpolation. Mevcut konum zaten
        # şehir merkezine çok yakınsa offset ekle.
        if not is_walk:
            if abs(target_lat - request.current_lat) < 0.005 and abs(target_lon - request.current_lon) < 0.005:
                target_lat += 0.015   # ~1.6 km
                target_lon += 0.015

        # ── 6. Mesafe filtresi (şehir sınırı) ────────────────────────────
        # Walk: yürüme mesafesi (4km), Car: çevre ilçeler dahil (30km)
        max_km = 3.5 if is_walk else 30.0

        # Walk modunda mesafe ölçüm noktası kullanıcının bulunduğu yer;
        # Car modunda şehir merkezi (çevre ilçeler de dahil olsun).
        ref_lat = request.current_lat if is_walk else request.city_lat
        ref_lon = request.current_lon if is_walk else request.city_lon

        def _within_city(p: dict) -> bool:
            try:
                plat = float(p.get("lat") or 0)
                plon = float(p.get("lon") or 0)
                if plat == 0 and plon == 0:
                    return False
                phi1 = _math.radians(ref_lat)
                phi2 = _math.radians(plat)
                dphi = _math.radians(plat - ref_lat)
                dlam = _math.radians(plon - ref_lon)
                a = (_math.sin(dphi / 2) ** 2
                     + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlam / 2) ** 2)
                dist_km = 2 * 6371 * _math.asin(_math.sqrt(a))
                return dist_km <= max_km
            except Exception:
                return True

        # ── 7. LLM-driven dinamik slot üretimi ────────────────────────────
        from datetime import datetime as _dt, date as _date
        import re as _re_dp
        note = (request.activity_note or "").strip()
        try:
            req_date = _dt.strptime(request.date, "%Y-%m-%d").date()
        except Exception:
            req_date = _date.today()
        now = _dt.now()
        is_today = (req_date == now.date())
        now_hhmm = now.strftime("%H:%M") if is_today else ""

        # Varsayılan KATEGORİ-bazlı slotlar (LLM çıktısı yoksa veya çağrı başarısızsa).
        # Sabah/Öğle/Akşam zaman dilimleri yerine kullanıcıya "ne tarz mekan
        # istiyorsun?" sorusunu cevaplayan kategori başlıkları.
        _DEFAULT_SLOTS: List[dict] = [
            {"label": "☕ Kafeler", "start_time": "", "end_time": "",
             "intent_query": "kafe sessiz kahve",
             "narrative_intro": "Sakin bir kahve molası ister misin? Sessiz, manzaralı yerleri seçtim.",
             "location_hint": None, "is_locked": False},
            {"label": "🍽️ Yemek", "start_time": "", "end_time": "",
             "intent_query": "restoran lokanta yemek",
             "narrative_intro": "Acıkınca uğrayabileceğin yerel mekanlar.",
             "location_hint": None, "is_locked": False},
            {"label": "🏖️ Sahil & Doğa", "start_time": "", "end_time": "",
             "intent_query": "sahil park yürüyüş manzara",
             "narrative_intro": "Bol oksijen, sahil-kıyı veya park önerileri.",
             "location_hint": None, "is_locked": False},
            {"label": "🎭 Kültür", "start_time": "", "end_time": "",
             "intent_query": "müze sanat galerisi tarihi yer",
             "narrative_intro": "Şehrin tarihine ve sanatına dokunmak istersen.",
             "location_hint": None, "is_locked": False},
            {"label": "🌃 Akşam", "start_time": "", "end_time": "",
             "intent_query": "canlı müzik bar akşam mekan",
             "narrative_intro": "Akşam keyfi için canlı müzik ve gece mekanları.",
             "location_hint": venue_name, "is_locked": False},
        ]

        def _filter_past(slots: List[dict]) -> List[dict]:
            """
            Slot'lar artık varsayılan kategori başlıklı (saatten bağımsız).
            Kullanıcı LLM çıktısında belirli start/end time belirtirse,
            bitişi şimdiden önce olan slot'ları atla (sadece bugün için).
            """
            if not is_today:
                return slots
            out = []
            for s in slots:
                start_time = s.get("start_time") or ""
                end_time = s.get("end_time") or ""
                if not start_time and not end_time:
                    # Zamansız kategori — her zaman göster
                    out.append(s)
                    continue
                try:
                    end_h, end_m = map(int, end_time.split(":"))
                    if (end_h, end_m) > (now.hour, now.minute):
                        out.append(s)
                except Exception:
                    out.append(s)
            return out

        async def _generate_dynamic_slots() -> List[dict]:
            """
            Kullanıcı notunu LLM ile kategori-bazlı + anlatıcı slot listesine çevir.
            Sabit zaman dilimleri YOK — kullanıcı saat belirttiyse korunur, etmediyse
            sadece kategori başlığı kullanılır ("☕ Kafeler", "🏖️ Sahil & Doğa").
            """
            if not orchestrator.llm_gemini:
                return _DEFAULT_SLOTS
            if not note:
                return _DEFAULT_SLOTS

            system_prompt = (
                "Sen bir günlük plan asistanısın. Kullanıcı bir gün için ne tarz "
                "mekanlara gitmek istediğini yazıyor. Cevabını **KATEGORİ + "
                "ANLATICI** olarak ver — saat blokları DEĞİL.\n\n"
                "Her kategori JSON nesnesi:\n"
                "{\n"
                '  "label": "kategori başlığı emoji+kelime (örn. \\"☕ Kafeler\\", \\"🏖️ Sahil & Doğa\\", \\"🎭 Kültür\\")",\n'
                '  "start_time": "" veya "HH:MM" (kullanıcı saat belirttiyse),\n'
                '  "end_time": "" veya "HH:MM",\n'
                '  "intent_query": "google places araması için 2-4 anahtar kelime (Türkçe)",\n'
                '  "narrative_intro": "1 cümle samimi anlatım: \\"Sahil kenarında sessiz bir kahve molası ister misin?\\"",\n'
                '  "location_hint": "konum ipucu veya null",\n'
                '  "is_locked": true/false  // kullanıcının kesin etkinliği mi\n'
                "}\n\n"
                "KURALLAR:\n"
                "- Sabit \"Sabah/Öğle/Öğleden Sonra/Akşam\" etiketi KULLANMA. "
                "Bunun yerine \"☕ Kafeler\", \"🍽️ Yemek\", \"🏖️ Sahil & Doğa\", "
                "\"🎭 Kültür\", \"🌃 Akşam Mekanları\" gibi kategori başlıkları.\n"
                "- Kullanıcı 'saat 14'te X'te 2 saatlik iş' derse → bir locked blok "
                "(start_time=14:00, end_time=16:00, label='X (iş)', is_locked=true, "
                "intent_query=null).\n"
                "- narrative_intro her kategori için ZORUNLU — kullanıcıya hitap eden, "
                "samimi 1 cümle. Robotik 'Sabah aktiviteleri:' formatı YASAK.\n"
                "- 3-6 kategori üret. Kullanıcı tek bir tip ister gibiyse (örn. sadece "
                "kahve) 2-3 alt-kategoriye böl (kahve + manzara + kitap kafe gibi).\n"
                "- Sadece JSON array dön, başka metin YOK.\n"
            )
            user_prompt = (
                f"Tarih: {request.date} ({'BUGÜN' if is_today else 'GELECEK'})\n"
                f"Şu an: {now_hhmm or '—'}\n"
                f"Ulaşım: {request.transport_mode}\n"
                f"Şehir: {city_label}\n"
                f"Kullanıcı notu: \"{note}\"\n\n"
                f"JSON kategori array'ini üret:"
            )
            try:
                text = await _llm_invoke_with_fallback(
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_prompt),
                    ],
                    timeout=15.0,
                )
                # JSON array bul
                m = _re_dp.search(r"\[\s*\{.*\}\s*\]", text, _re_dp.DOTALL)
                if not m:
                    log.warning("[DayPlan/LLM] JSON array bulunamadı, default'a düşülüyor")
                    return _filter_past(_DEFAULT_SLOTS)
                parsed = json.loads(m.group(0))
                if not isinstance(parsed, list) or not parsed:
                    return _filter_past(_DEFAULT_SLOTS)
                # Şema kontrol + normalize
                cleaned: List[dict] = []
                for s in parsed[:6]:
                    if not isinstance(s, dict):
                        continue
                    label = (s.get("label") or "").strip() or "Slot"
                    start_time = (s.get("start_time") or "").strip()
                    end_time = (s.get("end_time") or "").strip()
                    # start/end_time opsiyonel; format yanlış olursa boş bırak
                    if start_time and not _re_dp.match(r"^\d{1,2}:\d{2}$", start_time):
                        start_time = ""
                    if end_time and not _re_dp.match(r"^\d{1,2}:\d{2}$", end_time):
                        end_time = ""
                    cleaned.append({
                        "label": label,
                        "start_time": start_time,
                        "end_time": end_time,
                        "intent_query": (s.get("intent_query") or "").strip() or None,
                        "narrative_intro": (s.get("narrative_intro") or "").strip() or None,
                        "location_hint": (s.get("location_hint") or "").strip() or None,
                        "is_locked": bool(s.get("is_locked")),
                    })
                if not cleaned:
                    return _filter_past(_DEFAULT_SLOTS)
                return _filter_past(cleaned)
            except Exception as e:
                log.warning(f"[DayPlan/LLM] slot üretimi hata: {e}")
                return _filter_past(_DEFAULT_SLOTS)

        dynamic_slots = await _generate_dynamic_slots()

        def _slot_log_label(s: dict) -> str:
            base = s.get("label", "?")
            st = s.get("start_time") or ""
            et = s.get("end_time") or ""
            time_part = f" {st}-{et}" if (st or et) else ""
            lock = " 🔒" if s.get("is_locked") else ""
            return f"{base}{time_part}{lock}"

        log.info(f"📅 [DayPlan/Slots] {len(dynamic_slots)} dinamik slot üretildi: "
                 f"{[_slot_log_label(s) for s in dynamic_slots]}")

        # İsim-bazlı kategori filtresi: yeme-içme niyetinde sanat merkezi/müze ele.
        _FOOD_DRINK_KEYS = ("kafe", "kahve", "restoran", "lokanta", "yemek", "bistro", "kahvaltı")
        _CULTURE_BLOCK_KEYS = ("sanat merkezi", "sanat galerisi", "müze", "müzesi", "galeri", "kütüphane", "tiyatro")
        def _name_matches_intent(query: str, place: dict) -> bool:
            q = (query or "").lower()
            name = (place.get("name") or "").lower()
            if any(k in q for k in _FOOD_DRINK_KEYS):
                if any(b in name for b in _CULTURE_BLOCK_KEYS):
                    return False
            return True

        # ── 8. Slot başına arama: her slot kendi intent_query + location_hint'iyle ──
        def _lerp(a: float, b: float, t: float) -> float:
            return a + (b - a) * t

        def _slot_fraction(idx: int, total: int) -> float:
            """Sıralı slotu rota üzerinde dağıt (0..1)."""
            if total <= 1:
                return 0.0
            return idx / (total - 1)

        async def _search_dynamic_slot(slot: dict, idx: int, total: int) -> list:
            if not search_tool:
                return []
            if slot.get("is_locked"):
                return []  # kilitli slot — arama gerekmez
            query = slot.get("intent_query") or "gezilecek yer"
            location_hint = slot.get("location_hint")
            frac = _slot_fraction(idx, total)
            s_lat = _lerp(request.current_lat, target_lat, frac)
            s_lon = _lerp(request.current_lon, target_lon, frac)

            args = {
                "query": query,
                "lat": s_lat,
                "lon": s_lon,
                "location_name": location_hint,
                "session_id": search_sid,
            }
            args = {k: v for k, v in args.items() if v is not None}
            result = await _safe_invoke(search_tool, args, timeout=20.0)
            if not result:
                return []
            raw = (
                result.get("places")
                or result.get("strict_route_places")
                or (result if isinstance(result, list) else [])
            )
            return [
                p for p in raw
                if p.get("name") and _within_city(p) and _name_matches_intent(query, p)
            ][:4]

        # Dinamik slotlar için paralel arama
        slot_results = await asyncio.gather(*[
            _search_dynamic_slot(s, i, len(dynamic_slots))
            for i, s in enumerate(dynamic_slots)
        ])

        # Akşam slotunu belirle: en geç başlayan veya 18:00+ başlayan
        def _is_evening_slot(s: dict) -> bool:
            try:
                h = int(s["start_time"].split(":")[0])
                return h >= 18
            except Exception:
                return False

        # ── 9. Sections oluştur ───────────────────────────────────────────
        sections: List[DayPlanSection] = []
        evening_idx = None
        for i, s in enumerate(dynamic_slots):
            if _is_evening_slot(s):
                evening_idx = i

        for i, slot in enumerate(dynamic_slots):
            places = slot_results[i]
            is_evening = (i == evening_idx)
            is_event_slot = (is_evening and event_anchor is not None)
            slot_label = slot["label"]
            st = slot.get("start_time") or ""
            et = slot.get("end_time") or ""
            time_range = f"{st} – {et}" if (st and et) else ""
            is_locked = bool(slot.get("is_locked"))

            cards: List[DayPlanCard] = []

            # Kilitli (locked) slot — kullanıcının kesin etkinliği
            if is_locked:
                cards.append(DayPlanCard(
                    name=slot.get("location_hint") or slot_label,
                    address=slot.get("location_hint") or "",
                    lat=target_lat,
                    lon=target_lon,
                    description=f"{time_range} — kullanıcı planı",
                    category="locked",
                    is_anchor=True,
                ))

            # Akşam anchor event kartı (kilitli) — varsa
            if is_event_slot:
                cards.append(DayPlanCard(
                    name=event_anchor["title"],
                    address=event_anchor.get("venue", ""),
                    lat=target_lat,
                    lon=target_lon,
                    description=event_anchor.get("time", ""),
                    category="event",
                    is_anchor=True,
                ))

            # Akşam slot'unda ek etkinlikler — anchor olmasa bile bu kartlar görünür
            if is_evening:
                for ev in extra_events:
                    cards.append(DayPlanCard(
                        name=ev["title"],
                        address=ev.get("venue") or "",
                        lat=target_lat,
                        lon=target_lon,
                        description=ev.get("time") or "",
                        category="event",
                        is_anchor=False,
                    ))

            # Toplu micro-narrative: slot içindeki place'leri tek LLM çağrısıyla
            # 1-2 cümle açıklama ile zenginleştir.
            place_narratives: dict = {}
            if orchestrator.llm_gemini and places:
                try:
                    name_list = [p.get("name") for p in places if p.get("name")][:5]
                    if name_list:
                        narrative_system = (
                            "Sen sıcak, samimi bir öneri asistanısın. Verilen mekan listesi "
                            "için her birine **1-2 kısa cümlelik** açıklama yaz — atmosfer, "
                            "ne için uygun, hangi durumda iyi gider gibi.\n"
                            "Çıktı SADECE JSON object olsun: {\"Mekan Adı\": \"açıklama\", ...}. "
                            "Robotik 'X mekanı bir restorandır' yazma — kullanıcıyla konuşur gibi."
                        )
                        narrative_user = (
                            f"Kategori: {slot.get('label', '')}\n"
                            f"Niyet: {slot.get('intent_query', '')}\n"
                            f"Şehir: {city_label}\n"
                            f"Mekanlar:\n" + "\n".join(f"- {n}" for n in name_list)
                        )
                        text_n = await _llm_invoke_with_fallback(
                            [
                                SystemMessage(content=narrative_system),
                                HumanMessage(content=narrative_user),
                            ],
                            timeout=12.0,
                        )
                        m_n = _re_dp.search(r"\{[\s\S]*\}", text_n)
                        if m_n:
                            try:
                                place_narratives = json.loads(m_n.group(0))
                            except Exception:
                                place_narratives = {}
                except Exception as e:
                    log.warning(f"[DayPlan/PlaceNarrative] {slot.get('label')} hata: {e}")
                    place_narratives = {}

            for p in places:
                p_name = p.get("name", "")
                cards.append(DayPlanCard(
                    name=p_name,
                    address=p.get("address") or "",
                    lat=float(p.get("lat") or 0),
                    lon=float(p.get("lon") or 0),
                    rating=p.get("rating"),
                    description=(place_narratives.get(p_name) or "").strip(),
                    category=(slot.get("intent_query") or "poi").split()[0] if slot.get("intent_query") else "poi",
                ))

            if not cards:
                continue

            sections.append(DayPlanSection(
                slot=slot_label,
                time_range=time_range,
                cards=cards[:5],
                is_event_slot=is_event_slot,
                event_detail=event_anchor if is_event_slot else None,
                is_locked=is_locked,
                intent=slot.get("intent_query"),
                narrative_intro=slot.get("narrative_intro"),
            ))

        # ── 10. Narrative ─────────────────────────────────────────────────
        if event_anchor:
            narrative = (
                f"Bugün {city_label}'da {event_anchor['title']} var. "
                f"Gün mevcut konumundan başlayıp etkinliğe doğru ilerliyor."
            )
        else:
            narrative = (
                f"{city_label} için sabahtan akşama bir plan hazırlandı."
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        log.info(
            f"✅ [DayPlan] Tamamlandı {elapsed}ms | {len(sections)} bölüm "
            f"| anchor={bool(event_anchor)} | max_km={max_km}"
        )

        return ApiEnvelope(
            success=True,
            data=DayPlanResponse(
                success=True,
                date=request.date,
                city=city_label,
                weather_summary=weather_summary,
                event_anchor=event_anchor,
                sections=sections,
                narrative=narrative,
                all_events=all_events,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    except Exception as e:
        log.error(f"🔥 [DayPlan] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="DAY_PLAN_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )


# ─────────────────────────────────────────────────────────────────────────────
# DAY PLAN SCHEDULE — Seçilen mekanlar + orijinal not → saat-saat program
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/v1/day_plan/schedule", tags=["Day Planning"])
async def day_plan_schedule(
    request: DayPlanScheduleRequest, user: dict = Depends(get_optional_user)
):
    """
    📅 İkinci aşama: kategori önerilerinden seçilen mekanları + kullanıcının
    orijinal notunu LLM'e verip **saat-saat program** üretir.

    Mekan seçimi BOŞ olabilir — bu durumda LLM sadece kullanıcı notuna göre
    boş bir iskelet kurar (locked etkinlikler + serbest zaman).
    """
    import re as _re_sch
    from datetime import datetime as _dt, date as _date

    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(
        f"📅 [Schedule] session={session_id} | date={request.date} | "
        f"{len(request.selected_places)} mekan | note={request.activity_note[:50]!r}"
    )

    try:
        # Now bilgisi (bugün için ise saat penceresinde ilerleyen blok üret)
        try:
            req_date = _dt.strptime(request.date, "%Y-%m-%d").date()
        except Exception:
            req_date = _date.today()
        now = _dt.now()
        is_today = (req_date == now.date())
        now_hhmm = now.strftime("%H:%M") if is_today else ""

        # Seçim metnini LLM için derli toplu hazırla
        places_text = ""
        if request.selected_places:
            lines = []
            for p in request.selected_places:
                line = f"- {p.name}"
                if p.category:
                    line += f" [{p.category}]"
                if p.address:
                    line += f" — {p.address}"
                if p.lat is not None and p.lon is not None:
                    line += f" ({p.lat:.4f},{p.lon:.4f})"
                lines.append(line)
            places_text = "\n".join(lines)
        else:
            places_text = "(Kullanıcı hiç mekan seçmedi — programı sadece notuna göre kur)"

        if not orchestrator.llm_gemini:
            return ApiEnvelope(
                success=False,
                error=ApiError(code="LLM_UNAVAILABLE", message="LLM mevcut değil."),
                metadata=ApiMetadata(response_time_ms=0, session_id=session_id),
            )

        system_prompt = (
            "Sen kişisel günlük plan asistanısın. Kullanıcının notundaki "
            "kısıtlamaları (saat+yer belirtilen kesin etkinlikler) önce sabitler, "
            "sonra seçtiği mekanları o günün akışına mantıklı bir sırayla "
            "yerleştirirsin. Mekanlar arası geçiş süresini de hesaba katarsın.\n\n"
            "ÇIKTI **SADECE JSON OBJECT** olmalı:\n"
            "{\n"
            '  "summary": "Programın 1-2 cümle özeti (samimi, doğal).",\n'
            '  "schedule": [\n'
            "    {\n"
            '      "time": "HH:MM",\n'
            '      "end_time": "HH:MM" (opsiyonel),\n'
            '      "name": "Mekan/Etkinlik adı",\n'
            '      "address": "Adres" (opsiyonel),\n'
            '      "type": "locked | place | meal | travel | free",\n'
            '      "duration_min": 60,\n'
            '      "note": "Samimi 1 cümle — neden bu saat, ne yapacaksın",\n'
            '      "lat": float (varsa), "lon": float (varsa)\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "KURALLAR:\n"
            "- Kullanıcı 'saat 14'te X'te 2 saatlik iş' derse: type=locked, "
            "  time=14:00, end_time=16:00, name='X (iş)'.\n"
            "- Mekan seçimleri 'locked' bloklarla çakışmamalı. Locked'ı koru.\n"
            "- Her schedule item için **note** ZORUNLU: 'Sahil kenarında bir kahve "
            "  molası — molaların arasında nefes al.' gibi samimi 1 cümle.\n"
            "- type='meal' öğle/akşam yemeği için (saat 12-14 öğle, 19-21 akşam).\n"
            "- type='travel' uzun yol/geçiş için (>30dk seyahat).\n"
            "- type='free' kullanıcının açıklamasız serbest zamanı için.\n"
            "- BUGÜN için sorgu yapılırsa şu andan ÖNCEKİ saatlere blok koyma.\n"
            "- Mekanları kategori sırasına göre değil, **günün akışına** göre diz "
            "  (sabah→öğle→akşam mantığıyla).\n"
            "- JSON dışında metin yazma."
        )
        user_prompt = (
            f"Tarih: {request.date} ({'BUGÜN' if is_today else 'GELECEK'})\n"
            f"Şu an: {now_hhmm or '—'}\n"
            f"Şehir: {request.city or '—'}\n"
            f"Ulaşım: {request.transport_mode}\n"
            f"Kullanıcı notu:\n\"{request.activity_note}\"\n\n"
            f"Seçilen mekanlar:\n{places_text}\n\n"
            f"JSON programı üret:"
        )

        text = await _llm_invoke_with_fallback(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ],
            timeout=25.0,
        )
        m = _re_sch.search(r"\{[\s\S]*\}", text)
        if not m:
            log.warning("[Schedule] LLM JSON object dönmedi")
            return ApiEnvelope(
                success=False,
                error=ApiError(code="LLM_PARSE_ERROR", message="Program üretilemedi."),
                metadata=ApiMetadata(response_time_ms=0, session_id=session_id),
            )
        try:
            parsed = json.loads(m.group(0))
        except Exception as e:
            log.warning(f"[Schedule] JSON parse hata: {e}")
            return ApiEnvelope(
                success=False,
                error=ApiError(code="LLM_PARSE_ERROR", message="Program JSON geçersiz."),
                metadata=ApiMetadata(response_time_ms=0, session_id=session_id),
            )

        summary = (parsed.get("summary") or "").strip()
        raw_schedule = parsed.get("schedule") or []
        schedule_items: List[ScheduleItem] = []
        for it in raw_schedule:
            if not isinstance(it, dict):
                continue
            time_v = (it.get("time") or "").strip()
            if not _re_sch.match(r"^\d{1,2}:\d{2}$", time_v):
                continue
            end_v = (it.get("end_time") or "").strip() or None
            if end_v and not _re_sch.match(r"^\d{1,2}:\d{2}$", end_v):
                end_v = None
            try:
                lat_v = float(it["lat"]) if it.get("lat") is not None else None
            except Exception:
                lat_v = None
            try:
                lon_v = float(it["lon"]) if it.get("lon") is not None else None
            except Exception:
                lon_v = None
            try:
                dur_v = int(it["duration_min"]) if it.get("duration_min") is not None else None
            except Exception:
                dur_v = None
            schedule_items.append(ScheduleItem(
                time=time_v,
                end_time=end_v,
                name=(it.get("name") or "Etkinlik").strip(),
                address=(it.get("address") or None),
                type=(it.get("type") or "place").strip(),
                duration_min=dur_v,
                note=(it.get("note") or None),
                lat=lat_v,
                lon=lon_v,
            ))

        elapsed = int((time.monotonic() - t0) * 1000)
        log.info(f"✅ [Schedule] {len(schedule_items)} öğelik program {elapsed}ms")

        # ── History'e kaydet (giriş yapmış kullanıcılar için) ────────────
        if user and schedule_items:
            try:
                from core.db import async_session_maker, User, DayPlanHistory
                from sqlmodel import select as _sm_select
                from uuid import UUID as _UUID

                async def _save_history() -> None:
                    try:
                        async with async_session_maker() as s:
                            res = await s.execute(
                                _sm_select(User).where(
                                    User.id == _UUID(user["user_id"])
                                )
                            )
                            db_user = res.scalars().first()
                            if not db_user:
                                return
                            row = DayPlanHistory(
                                user_id=db_user.id,
                                plan_date=request.date,
                                city=request.city or None,
                                activity_note=request.activity_note or None,
                                summary=summary or None,
                                schedule=[it.model_dump() for it in schedule_items],
                            )
                            s.add(row)
                            await s.commit()
                    except Exception as _e:
                        log.warning(f"⚠️ [Schedule/Save] {_e}")

                asyncio.create_task(_save_history())
            except Exception as _e:
                log.warning(f"⚠️ [Schedule/SaveSetup] {_e}")

        return ApiEnvelope(
            success=True,
            data=DayPlanScheduleResponse(
                success=True,
                date=request.date,
                summary=summary,
                schedule=schedule_items,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )
    except Exception as e:
        log.error(f"🔥 [Schedule] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="SCHEDULE_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )


# ─────────────────────────────────────────────────────────────────────────────
# REPLACE STOP ENDPOINT — Rotadaki bir durağı LLM aramasıyla değiştir
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/v1/route/replace_stop", response_model=ApiEnvelope, tags=["Trip Planning"])
async def replace_stop(request: ReplaceStopRequest, user: dict = Depends(get_optional_user)):
    """
    🔄 Rotadaki bir durağı kullanıcının serbest metin talebine göre değiştirir.

    Akış:
      1. Mevcut durağın etrafında search_hybrid_places(query) ile yakın mekanları bul
      2. LLM ile en uygun ana öneriyi seç (mesafe + rating + intent uyumu)
      3. Yeni öneri + 2 alternatif dön — kullanıcı seçer
      4. Kullanıcı onaylayınca FE add_stops/trip_plan ile rotayı yeniden hesaplar
    """
    import math as _math
    t0 = time.monotonic()
    session_id = request.session_id
    if user:
        session_id = f"{user['user_id']}:{request.session_id}"

    log.info(
        f"🔄 [ReplaceStop] session={session_id} | stop_index={request.stop_index} | "
        f"query='{request.query}' | {len(request.waypoints)} waypoint"
    )

    try:
        # ── 1. Hedef durağın koordinatı (arama merkezi) ─────────────────
        target_lat = request.current_lat
        target_lon = request.current_lon
        if 0 <= request.stop_index < len(request.waypoints):
            wp_raw = request.waypoints[request.stop_index]
            wp_clean = wp_raw[:-3] if wp_raw.endswith("!pt") else wp_raw
            try:
                target_lat, target_lon = map(float, wp_clean.split(","))
            except Exception:
                pass

        # ── 2. Aramayı yap — proxy karışmasın diye temiz session_id ────
        import uuid as _uuid
        search_sid = f"replace:{_uuid.uuid4().hex[:10]}"
        search_tool = orchestrator.get_tool_by_name("search_hybrid_places")
        if not search_tool:
            return ApiEnvelope(
                success=False,
                error=ApiError(code="TOOL_MISSING", message="Arama aracı bulunamadı."),
                metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000)),
            )

        try:
            raw_search = await asyncio.wait_for(
                search_tool.ainvoke({
                    "query": request.query,
                    "lat": target_lat,
                    "lon": target_lon,
                    "session_id": search_sid,
                }),
                timeout=15.0,
            )
        except Exception as exc:
            log.warning(f"⚠️ [ReplaceStop] Arama hatası: {exc}")
            raw_search = None

        if isinstance(raw_search, str):
            try:
                raw_search = json.loads(raw_search)
            except Exception:
                raw_search = None

        places: list = []
        if isinstance(raw_search, dict):
            places = (
                raw_search.get("places")
                or raw_search.get("strict_route_places")
                or []
            )
        elif isinstance(raw_search, list):
            places = raw_search

        # En yakın 5 sonucu ele (haversine ile mesafe)
        def _dist_km(p: dict) -> float:
            try:
                plat = float(p.get("lat") or 0)
                plon = float(p.get("lon") or 0)
                phi1 = _math.radians(target_lat)
                phi2 = _math.radians(plat)
                dphi = _math.radians(plat - target_lat)
                dlam = _math.radians(plon - target_lon)
                a = (_math.sin(dphi / 2) ** 2
                     + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlam / 2) ** 2)
                return 2 * 6371 * _math.asin(_math.sqrt(a))
            except Exception:
                return 9999

        scored = [(p, _dist_km(p)) for p in places if p.get("name")]
        scored.sort(key=lambda x: x[1])
        top = scored[:5]

        if not top:
            return ApiEnvelope(
                success=True,
                data=ReplaceStopResponse(
                    success=False,
                    reason="Bu civarda eşleşen bir mekan bulamadım.",
                ).model_dump(),
                metadata=ApiMetadata(response_time_ms=int((time.monotonic() - t0) * 1000), session_id=session_id),
            )

        # ── 3. LLM seçimi — top 5 içinden en uygun olanı seç ─────────────
        reason_text: Optional[str] = None
        picked_idx = 0
        try:
            from langchain_core.messages import SystemMessage, HumanMessage
            candidates_block = "\n".join(
                f"{i}. {p.get('name')} — {p.get('address', '')} "
                f"(rating: {p.get('rating', '—')}, {dist:.1f} km uzakta)"
                for i, (p, dist) in enumerate(top)
            )
            sys = (
                "Sen GeoIntel rota asistanısın. Kullanıcı rotadaki bir durağı değiştirmek "
                "istiyor. Adaylar arasından **en uygun olanı tek bir indexle** seç ve "
                "tek cümleyle gerekçesini yaz. Format:\n"
                "INDEX: <0-N>\nGEREKÇE: <tek cümle>"
            )
            usr = (
                f"Kullanıcı isteği: \"{request.query}\"\n\n"
                f"Adaylar:\n{candidates_block}"
            )
            res = await asyncio.wait_for(
                orchestrator.llm_gemini.ainvoke([
                    SystemMessage(content=sys),
                    HumanMessage(content=usr),
                ]),
                timeout=10.0,
            )
            text = res.content if hasattr(res, "content") else str(res)
            if isinstance(text, list):
                text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
            text = str(text or "").strip()
            for line in text.splitlines():
                if line.upper().startswith("INDEX"):
                    try:
                        picked_idx = int(line.split(":", 1)[1].strip())
                    except Exception:
                        pass
                elif line.upper().startswith("GEREKÇE") or line.upper().startswith("GEREKCE"):
                    reason_text = line.split(":", 1)[1].strip() if ":" in line else None
            picked_idx = max(0, min(picked_idx, len(top) - 1))
        except Exception as e:
            log.warning(f"⚠️ [ReplaceStop] LLM seçim hatası: {e}")
            picked_idx = 0  # mesafe sıralamasından en yakını

        def _to_suggestion(p: dict) -> ReplaceStopSuggestion:
            return ReplaceStopSuggestion(
                name=str(p.get("name", "")),
                address=p.get("address"),
                lat=float(p.get("lat") or 0),
                lon=float(p.get("lon") or 0),
                rating=(p.get("rating") if isinstance(p.get("rating"), (int, float)) else None),
                description=p.get("description"),
            )

        suggestion = _to_suggestion(top[picked_idx][0])
        alternates = [
            _to_suggestion(p) for i, (p, _) in enumerate(top) if i != picked_idx
        ][:3]

        elapsed = int((time.monotonic() - t0) * 1000)
        log.info(f"✅ [ReplaceStop] Önerildi: {suggestion.name} ({elapsed}ms)")

        return ApiEnvelope(
            success=True,
            data=ReplaceStopResponse(
                success=True,
                suggestion=suggestion,
                alternates=alternates,
                reason=reason_text,
            ).model_dump(),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )

    except Exception as e:
        log.error(f"🔥 [ReplaceStop] Hata: {e}")
        elapsed = int((time.monotonic() - t0) * 1000)
        return ApiEnvelope(
            success=False,
            error=ApiError(code="REPLACE_STOP_ERROR", message=str(e)),
            metadata=ApiMetadata(response_time_ms=elapsed, session_id=session_id),
        )
