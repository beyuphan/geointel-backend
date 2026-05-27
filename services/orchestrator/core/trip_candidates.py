"""
trip_candidates.py — Aday havuzu toplayıcı + Combined POI detector + Marker budget

LLM-First Curator mimarisi katman 2:
  - `collect_candidates`: search_plan'deki her item için paralel arama, ham havuz döner
  - `_detect_combined_poi`: 300m yakınlıktaki yakıt+yemek+mola → 'combined_stop'
  - `_apply_marker_budget`: kategori payına göre marker tavanı (default 15)

Bu modül LLM çağırmaz — sadece deterministik aday toplama.
LLM seçimi `trip_curator.py` tarafından yapılır.
"""
from __future__ import annotations
import asyncio
import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from logger import log

# Aşağıdaki RouteStrategyEvaluator ve _is_valid_break_stop yumuşak import:
# döngüsel import'tan kaçınmak için lazy resolve.


# ─────────────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CandidatePools:
    """Curator'a verilecek ham aday havuzu."""
    food: List[Dict[str, Any]] = field(default_factory=list)
    fuel: List[Dict[str, Any]] = field(default_factory=list)
    break_: List[Dict[str, Any]] = field(default_factory=list)
    scenic: List[Dict[str, Any]] = field(default_factory=list)
    combined: List[Dict[str, Any]] = field(default_factory=list)
    fuel_summary: Dict[str, Any] = field(default_factory=dict)

    def all_for_curator(self) -> List[Dict[str, Any]]:
        """Tüm adayları tek liste olarak LLM'e göndermek için."""
        out: List[Dict[str, Any]] = []
        out.extend(self.combined)  # combined önce — öncelik
        out.extend(self.food)
        out.extend(self.fuel)
        out.extend(self.break_)
        out.extend(self.scenic)
        return out

    def by_id(self) -> Dict[str, Dict[str, Any]]:
        """ID → aday haritası."""
        return {c["id"]: c for c in self.all_for_curator() if c.get("id")}


# ─────────────────────────────────────────────────────────────────────────────
# Combined POI Detector
# ─────────────────────────────────────────────────────────────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _detect_combined_poi(
    food: List[Dict[str, Any]],
    fuel: List[Dict[str, Any]],
    break_: List[Dict[str, Any]],
    radius_m: float = 300.0,
) -> List[Dict[str, Any]]:
    """300m yakınlıktaki farklı kategori adaylarını 'combined_stop' olarak işaretle.

    'Hüsam Dinlenme Tesisi' gibi yerler genelde 3 ayrı kategoriye (yakıt, yemek,
    mola) düşer. Aynı küme içindeki adayları birleştirip tek combined kart üretir.
    """
    combined: List[Dict[str, Any]] = []
    # Tüm adayları (kind etiketli) tek listede topla
    tagged: List[Tuple[str, Dict[str, Any]]] = (
        [("fuel", p) for p in fuel]
        + [("food", p) for p in food]
        + [("break", p) for p in break_]
    )
    used: set = set()
    for i, (kind_i, p_i) in enumerate(tagged):
        if i in used:
            continue
        if not isinstance(p_i, dict) or "lat" not in p_i or "lon" not in p_i:
            continue
        try:
            lat_i = float(p_i["lat"])
            lon_i = float(p_i["lon"])
        except Exception:
            continue
        cluster_kinds = {kind_i}
        cluster_members = [p_i]
        cluster_idx = [i]
        for j in range(i + 1, len(tagged)):
            if j in used:
                continue
            kind_j, p_j = tagged[j]
            if "lat" not in p_j or "lon" not in p_j:
                continue
            try:
                d = _haversine_m(lat_i, lon_i, float(p_j["lat"]), float(p_j["lon"]))
            except Exception:
                continue
            if d <= radius_m:
                cluster_kinds.add(kind_j)
                cluster_members.append(p_j)
                cluster_idx.append(j)
        # Combined sayılması için en az 2 farklı kind gerekli
        if len(cluster_kinds) < 2:
            continue
        # Cluster'ı kullanılmış işaretle
        for idx in cluster_idx:
            used.add(idx)
        # En yüksek rating'li üyeyi temsilci seç
        rep = max(
            cluster_members,
            key=lambda x: float(x.get("rating") or 0),
        )
        name_lower = (rep.get("name") or "").lower()
        # 'dinlenme tesisi' / 'tesisi' / 'sosyal tesis' geçenleri öne çıkar
        for m in cluster_members:
            nm = (m.get("name") or "").lower()
            if any(k in nm for k in ("dinlenme tesisi", "sosyal tesis", "mola tesisi")):
                rep = m
                name_lower = nm
                break
        combined.append({
            "id": f"combined_{rep.get('id') or rep.get('name', 'x')}_{round(float(rep['lat']), 4)}",
            "name": rep.get("name") or "Dinlenme Tesisi",
            "lat": float(rep["lat"]),
            "lon": float(rep["lon"]),
            "address": rep.get("address") or rep.get("snippet"),
            "rating": rep.get("rating"),
            "review_count": rep.get("review_count") or rep.get("user_ratings_total"),
            "deviation_meters": rep.get("deviation_meters"),
            "distance_along_route_km": rep.get("distance_along_route_km"),
            "kind": "combined",
            "sub_capabilities": sorted(cluster_kinds),
            "cluster_members": [m.get("name") for m in cluster_members],
            "_source_rep": {
                "fuel_price": rep.get("fuel_price"),
                "open_now": rep.get("open_now"),
                "phone": rep.get("phone"),
            },
        })
    return combined


# ─────────────────────────────────────────────────────────────────────────────
# Marker Budget
# ─────────────────────────────────────────────────────────────────────────────

def _apply_marker_budget(
    pools: CandidatePools,
    selected_ids: List[str],
    budget: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Curator'ın seçtiği ID'leri sırayla al, marker tavanını aşmadan döndür.

    Kategori payı (default budget=15):
      combined=2 (her biri 3 ihtiyaç karşılar), food=5, fuel=4, break=4, scenic=2
    """
    if budget is None:
        budget = int(os.getenv("TRIP_MARKER_BUDGET", "15"))
    by_id = pools.by_id()
    quotas = {"combined": 2, "food": 5, "fuel": 4, "break": 4, "scenic": 2}
    used = {k: 0 for k in quotas}
    out: List[Dict[str, Any]] = []
    # 1) Önce curator sırasına göre kabul et
    for sid in selected_ids:
        if len(out) >= budget:
            break
        cand = by_id.get(sid)
        if not cand:
            continue
        kind = cand.get("kind") or cand.get("type") or "food"
        if kind not in used:
            kind = "food"
        if used[kind] >= quotas[kind]:
            continue
        used[kind] += 1
        out.append(cand)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Collector
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_place(p: Dict[str, Any], kind: str, idx: int) -> Optional[Dict[str, Any]]:
    """search_hybrid_places sonucunu collector ortak formatına çevir."""
    if not isinstance(p, dict):
        return None
    # Bazı yerlerde lat/lon eksik, "coords" string'i var
    if "lat" not in p and "coords" in p:
        try:
            clat, clon = map(float, p["coords"].split(","))
            p["lat"] = clat
            p["lon"] = clon
        except Exception:
            return None
    if "lat" not in p or "lon" not in p:
        return None
    try:
        lat = float(p["lat"])
        lon = float(p["lon"])
    except Exception:
        return None
    pid = (
        p.get("id")
        or p.get("place_id")
        or f"{kind}_{idx}_{round(lat, 4)}_{round(lon, 4)}"
    )
    return {
        "id": str(pid),
        "name": p.get("name") or "Bilinmeyen",
        "lat": lat,
        "lon": lon,
        "address": p.get("address") or p.get("snippet"),
        "rating": p.get("rating"),
        "review_count": p.get("review_count") or p.get("user_ratings_total"),
        "price_level": p.get("price_level"),
        "open_now": p.get("open_now"),
        "phone": p.get("phone"),
        "deviation_meters": p.get("deviation_meters"),
        "distance_along_route_km": p.get("distance_along_route_km"),
        "on_route_side": p.get("on_route_side"),
        "eta": p.get("eta"),
        "fuel_price": p.get("fuel_price"),
        "kind": kind,
        "_raw_keys": list(p.keys())[:5],  # debug, küçük
    }


def _extract_places_from_response(raw: Any) -> List[Dict[str, Any]]:
    """search_hybrid_places çeşitli response anahtarlarını birleştir."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, dict):
        return []
    out: List[Dict[str, Any]] = []
    for k in ("strict_route_places", "relaxed_route_places", "places", "results"):
        v = raw.get(k)
        if isinstance(v, list):
            out.extend(v)
    return out


async def _collect_places_item(
    item: Dict[str, Any],
    polyline: str,
    total_km: float,
    orchestrator,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Tek bir search_plan item için search_hybrid_places çağrısı.

    Fraction range varsa: aralığın 2 farklı noktasında paralel arama → daha
    geniş havuz. Region hint varsa: şehir merkezinde tek arama.
    """
    kind = item.get("kind") or "food"
    query = item.get("query") or "restoran"
    region_hint = item.get("region_hint") or {}
    fraction_range = item.get("fraction_range")
    max_results = int(item.get("max_results") or 18)

    places_tool = orchestrator.get_tool_by_name("search_hybrid_places")
    if not places_tool:
        return (kind, [])

    city = region_hint.get("city") if isinstance(region_hint, dict) else None

    # Search çağrılarını oluştur (paralel)
    call_args_list: List[Dict[str, Any]] = []
    if city:
        call_args_list.append({"query": query, "location_name": city})
    elif polyline:
        if (
            fraction_range
            and isinstance(fraction_range, (list, tuple))
            and len(fraction_range) == 2
        ):
            try:
                a = float(fraction_range[0])
                b = float(fraction_range[1])
                # Aralığın 1/3 ve 2/3 noktalarında ayrı aramalar (havuz büyütür)
                f1 = a + (b - a) * 0.33
                f2 = a + (b - a) * 0.67
                call_args_list.append({
                    "query": query, "route_polyline": polyline,
                    "target_fraction": f1,
                })
                call_args_list.append({
                    "query": query, "route_polyline": polyline,
                    "target_fraction": f2,
                })
            except Exception:
                call_args_list.append({
                    "query": query, "route_polyline": polyline,
                    "target_fraction": 0.5,
                })
        else:
            call_args_list.append({
                "query": query, "route_polyline": polyline,
                "target_fraction": 0.5,
            })
    else:
        call_args_list.append({"query": query})

    async def _one_call(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            raw = await asyncio.wait_for(places_tool.ainvoke(args), timeout=18.0)
            return _extract_places_from_response(raw)
        except asyncio.TimeoutError:
            log.warning(f"⏱️ [Collector] {kind}/{query!r} args={args} timeout")
            return []
        except Exception as exc:
            log.warning(f"⚠️ [Collector] {kind}/{query!r} hata: {exc}")
            return []

    results = await asyncio.gather(*[_one_call(a) for a in call_args_list])
    raw_places: List[Dict[str, Any]] = []
    for r in results:
        raw_places.extend(r)

    normalized: List[Dict[str, Any]] = []
    seen: set = set()
    for i, p in enumerate(raw_places):
        norm = _normalize_place(p, kind, i)
        if not norm:
            continue
        key = (norm["name"], round(norm["lat"], 4), round(norm["lon"], 4))
        if key in seen:
            continue
        seen.add(key)
        normalized.append(norm)
        if len(normalized) >= max_results:
            break
    log.info(
        f"📍 [Collector] {kind}/{query!r} → {len(normalized)} aday "
        f"({len(call_args_list)} paralel arama)"
    )
    return (kind, normalized)


async def _collect_fuel_item(
    item: Dict[str, Any],
    origin: str,
    destination: str,
    polyline: str,
    total_km: float,
    fuel_type: str,
    fuel_range: float,
    orchestrator,
) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """Yakıt için RouteStrategyEvaluator çağrısı — anchor item.anchor'tan gelir."""
    from core.macro_tools import RouteStrategyEvaluator  # lazy
    evaluator = RouteStrategyEvaluator(orchestrator)
    anchor = item.get("anchor")
    try:
        fuel_result = await evaluator.evaluate(
            origin=origin,
            destination=destination,
            fuel_type=fuel_type,
            fuel_range=fuel_range,
            polyline=polyline,
            total_dist_km=total_km,
            anchor_at_start=(anchor == "start"),
            anchor_at_end=(anchor == "end"),
        )
    except Exception as exc:
        log.warning(f"⚠️ [Collector/fuel] hata: {exc}")
        return ("fuel", [], {})

    if not isinstance(fuel_result, dict) or fuel_result.get("status") != "success":
        return ("fuel", [], {})

    summary = {
        "cheapest_city": fuel_result.get("cheapest_fuel_city"),
        "best_station": fuel_result.get("best_station_recommendation"),
        "stops_by_km": fuel_result.get("stops_by_km", []),
    }
    normalized: List[Dict[str, Any]] = []
    for i, m in enumerate(fuel_result.get("places", [])):
        if not isinstance(m, dict):
            continue
        dist = m.get("distance_along_route_km") or 0
        if dist > total_km:
            continue  # Rota dışı (RouteStrategyEvaluator bazen aşıyor)
        norm = _normalize_place(m, "fuel", i)
        if norm:
            normalized.append(norm)
    return ("fuel", normalized, summary)


async def collect_candidates(
    search_plan: List[Dict[str, Any]],
    polyline: str,
    total_km: float,
    origin: str,
    destination: str,
    fuel_type: str,
    fuel_range: float,
    orchestrator,
    break_filter=None,
) -> CandidatePools:
    """search_plan'deki tüm item'ları paralel çalıştır, havuzları doldur.

    break_filter: opsiyonel callable(place_dict) -> bool — break adaylarında filtre.
    """
    if not search_plan:
        return CandidatePools()

    place_items = []
    fuel_items = []
    for it in search_plan:
        if it.get("kind") == "fuel":
            fuel_items.append(it)
        else:
            place_items.append(it)

    # Place tasks (food/break/scenic) paralel
    place_tasks = [
        _collect_places_item(it, polyline, total_km, orchestrator)
        for it in place_items
    ]
    fuel_tasks = [
        _collect_fuel_item(
            it, origin, destination, polyline, total_km,
            fuel_type, fuel_range, orchestrator,
        )
        for it in fuel_items
    ]

    place_results, fuel_results = await asyncio.gather(
        asyncio.gather(*place_tasks, return_exceptions=True) if place_tasks else asyncio.sleep(0, result=[]),
        asyncio.gather(*fuel_tasks, return_exceptions=True) if fuel_tasks else asyncio.sleep(0, result=[]),
    )

    pools = CandidatePools()

    # Food / break / scenic'i bucket'la
    for res in place_results:
        if isinstance(res, Exception) or not res:
            continue
        kind, items = res
        if kind == "food":
            pools.food.extend(items)
        elif kind == "break":
            if break_filter:
                items = [p for p in items if break_filter(p)]
            pools.break_.extend(items)
        elif kind == "scenic":
            pools.scenic.extend(items)
        else:
            pools.food.extend(items)

    # Fuel
    for res in fuel_results:
        if isinstance(res, Exception) or not res:
            continue
        kind, items, summary = res
        pools.fuel.extend(items)
        if summary and not pools.fuel_summary:
            pools.fuel_summary = summary

    # Tekrar eden ID'leri ele (aynı yere farklı kategoriden 2 kere gelmiş olabilir)
    def _dedupe(lst: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen: set = set()
        out: List[Dict[str, Any]] = []
        for p in lst:
            k = (round(p["lat"], 4), round(p["lon"], 4), p.get("name"))
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        return out

    pools.food = _dedupe(pools.food)
    pools.fuel = _dedupe(pools.fuel)
    pools.break_ = _dedupe(pools.break_)
    pools.scenic = _dedupe(pools.scenic)

    # Combined POI detect
    pools.combined = _detect_combined_poi(pools.food, pools.fuel, pools.break_)

    log.info(
        f"📦 [Collector] havuz: food={len(pools.food)} fuel={len(pools.fuel)} "
        f"break={len(pools.break_)} scenic={len(pools.scenic)} "
        f"combined={len(pools.combined)}"
    )
    return pools


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic Selector (Curator fallback)
# ─────────────────────────────────────────────────────────────────────────────

def _quality_filter(
    candidates: List[Dict[str, Any]],
    min_rating: float = 3.0,
    min_keep: int = 2,
) -> List[Dict[str, Any]]:
    """Düşük puanlı adayları ele — ama havuz çok küçükse eşiği gevşet.

    Rating None olanlar nötr kabul edilir (filtrelenmez) — Google'da bazı yerlerde
    rating yok ama mekan iyi olabilir. Sadece açıkça düşük rating'lileri ele.
    """
    high = [
        p for p in candidates
        if (p.get("rating") is None) or (float(p.get("rating") or 0) >= min_rating)
    ]
    if len(high) >= min_keep:
        return high
    # Yeterince yüksek puanlı yok — eşiği 2.5'e indir
    medium = [
        p for p in candidates
        if (p.get("rating") is None) or (float(p.get("rating") or 0) >= 2.5)
    ]
    if len(medium) >= min_keep:
        return medium
    return candidates  # Hiçbir şey yoksa hepsini bırak


def deterministic_select_from_pools(
    pools: "CandidatePools",
    request_dict: Dict[str, Any],
    budget: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Curator çalışmazsa: kategori payına göre top N seçim, kalite filtresi ile.

    food: 5 (target_km'ye yakın + rating dominant skor),
    fuel: 3 (anchor + en ucuz/yakın),
    break: 3 (slot'lara yakın),
    combined: 2 (önce, min rating 3.0),
    scenic: 2 (varsa).
    """
    if budget is None:
        budget = int(os.getenv("TRIP_MARKER_BUDGET", "15"))

    quotas = {"combined": 2, "food": 5, "fuel": 3, "break": 3, "scenic": 2}
    total_km = float(request_dict.get("total_km") or 0)
    food_loc = (request_dict.get("food_location") or "Ortaları").strip()
    frac_map = {"Başları": 0.2, "Ortaları": 0.5, "Sonları": 0.8}
    target_km = total_km * frac_map.get(food_loc, 0.5)
    food_specific = (request_dict.get("food_specific") or "").strip().lower()
    scene_filters = [
        str(s).lower() for s in (request_dict.get("scene_filters") or [])
        if isinstance(s, str)
    ]

    def _score(p: Dict[str, Any]) -> float:
        # Rating dominant skor: yüksek rating + ortalama yorum sayısı
        r = float(p.get("rating") or 0)
        n = int(p.get("review_count") or 0)
        dev = float(p.get("deviation_meters") or 9999)
        if r <= 0:
            # Rating yoksa sadece deviation
            return -dev / 1000.0
        # Rating ağırlıklı: rating^2 * log(reviews+1) → 4.5 puan >> 3.5 puan
        return (r * r) * math.log10(max(n, 5) + 1) - dev / 8000.0

    def _name_matches(p: Dict[str, Any], keywords: List[str]) -> bool:
        name = (p.get("name") or "").lower()
        addr = (p.get("address") or "").lower()
        return any(k in name or k in addr for k in keywords)

    out: List[Dict[str, Any]] = []

    # Combined — min rating 3.0 zorunlu (kötü puanlı yer "tek durakta hepsi" olmaz)
    combined_filtered = _quality_filter(pools.combined, min_rating=3.0, min_keep=1)
    combined_sorted = sorted(combined_filtered, key=_score, reverse=True)
    out.extend(combined_sorted[:quotas["combined"]])

    # Food — quality filter + spesifik yemek eşleşmesi bonusu
    food_quality = _quality_filter(pools.food, min_rating=3.2, min_keep=3)

    def _food_key(p):
        dist = float(p.get("distance_along_route_km") or 0)
        # Spesifik yemek isminin adında geçmesi → en üst
        specific_bonus = 0
        if food_specific and _name_matches(p, [food_specific]):
            specific_bonus = -10000  # negatif çünkü ascending sort
        # Scene match (manzaralı/sahil) bonusu
        scene_bonus = 0
        if scene_filters and _name_matches(p, scene_filters):
            scene_bonus = -1000
        return (specific_bonus + scene_bonus, abs(dist - target_km), -_score(p))

    food_sorted = sorted(food_quality, key=_food_key)
    out.extend(food_sorted[:quotas["food"]])

    # Fuel
    def _fuel_key(p):
        fp = p.get("fuel_price") or {}
        ppl = fp.get("price_per_liter")
        has = isinstance(ppl, (int, float))
        return (0 if has else 1, ppl if has else 9999, float(p.get("deviation_meters") or 9999))
    fuel_sorted = sorted(pools.fuel, key=_fuel_key)
    out.extend(fuel_sorted[:quotas["fuel"]])

    # Break — quality filter + rating-dominant skor
    if pools.break_:
        break_quality = _quality_filter(pools.break_, min_rating=3.0, min_keep=2)
        break_sorted = sorted(break_quality, key=_score, reverse=True)
        out.extend(break_sorted[:quotas["break"]])

    # Scenic
    if pools.scenic:
        scenic_sorted = sorted(pools.scenic, key=_score, reverse=True)
        out.extend(scenic_sorted[:quotas["scenic"]])

    # Budget tavanı
    return out[:budget]


# ─────────────────────────────────────────────────────────────────────────────
# Strategist v2 — Targeted Stops Resolver
# Her strategist stop'unu paralel olarak tek search ile gerçek mekana eşler.
# Daha az çağrı (sadece N stop), daha hızlı (12s timeout/her biri paralel).
# ─────────────────────────────────────────────────────────────────────────────

TARGETED_TIMEOUT_S = float(os.getenv("TRIP_TARGETED_TIMEOUT_S", "15.0"))


def _default_query_for_role(role: str) -> str:
    return {
        "fuel": "benzin istasyonu",
        "food": "restoran lokanta",
        "break": "kafe mola",
        "scenic": "manzara seyir noktası",
    }.get(role, "restoran")


def _generic_fallback_query(role: str) -> str:
    return {
        "fuel": "benzin",
        "food": "lokanta",
        "break": "kafe",
        "scenic": "seyir",
    }.get(role, "yer")


def _score_food_break(p: Dict[str, Any]) -> float:
    """Rating-dominant skor (yüksek rating + makul yorum sayısı + düşük sapma)."""
    r = float(p.get("rating") or 0)
    n = int(p.get("review_count") or p.get("user_ratings_total") or 0)
    dev = float(p.get("deviation_meters") or 0)
    if r <= 0:
        return -dev / 1000.0
    return (r * r) * math.log10(max(n, 5) + 1) - dev / 8000.0


def _pick_best_fuel(stations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fiyat varsa en ucuz; yoksa en yakın sapma."""
    with_price = [
        s for s in stations
        if isinstance((s.get("fuel_price") or {}).get("price_per_liter"), (int, float))
    ]
    if with_price:
        return min(
            with_price,
            key=lambda s: s["fuel_price"]["price_per_liter"],
        )
    return min(
        stations,
        key=lambda s: float(s.get("deviation_meters") or 9999),
    )


def _parse_city_district_from_address(
    address: str,
    fallback_city: str = "",
    fallback_district: str = "",
) -> Tuple[str, str]:
    """Türkiye adresinden (il, ilçe) çıkar. Yaygın format:
    '..., 14030 Elmalık/Bolu Merkez/Bolu, Türkiye'
    Son virgülden önceki segment '/' ile bölünür → district/city.
    Bulunamazsa fallback'lere düşülür.
    """
    if not address:
        return (fallback_city, fallback_district)
    parts = [p.strip() for p in address.split(",") if p.strip()]
    # Türkiye suffix'i atla
    parts = [p for p in parts if p.lower() not in ("türkiye", "turkey")]
    if not parts:
        return (fallback_city, fallback_district)
    # En sondan başla — Türk adres formatında il/ilçe sondan önce
    for part in reversed(parts):
        if "/" in part:
            segs = [s.strip() for s in part.split("/") if s.strip()]
            # Posta kodu varsa at ('14030 Elmalık' → 'Elmalık')
            segs = [
                (s.split(" ", 1)[1] if s.split(" ", 1)[0].isdigit() and " " in s else s)
                for s in segs
            ]
            if len(segs) >= 2:
                # Format: mahalle/ilçe/il veya ilçe/il
                city = segs[-1]
                district = segs[-2]
                # 'Bolu Merkez' → district='Bolu Merkez' → ilçeyi olduğu gibi tut
                return (city, district)
    return (fallback_city, fallback_district)


async def _enrich_fuel_price(
    station: Dict[str, Any],
    city: str,
    district: str,
    fuel_type: str,
    orchestrator,
) -> Optional[Dict[str, Any]]:
    """get_fuel_prices ile istasyon fiyatını eşle (marka eşleşmesi)."""
    if not (city or district):
        return None
    fuel_tool = orchestrator.get_tool_by_name("get_fuel_prices")
    if not fuel_tool:
        return None
    try:
        args = {"city": city or district}
        if district:
            args["district"] = district
        raw = await asyncio.wait_for(fuel_tool.ainvoke(args), timeout=8.0)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return None
        if not isinstance(raw, dict):
            return None
        prices = raw.get("data") or raw.get("prices") or []
        if not prices:
            return None
        st_name = (station.get("name") or "").lower().replace(" ", "")
        type_map = {"benzin": "gasoline", "motorin": "diesel", "dizel": "diesel", "lpg": "lpg"}
        price_key = type_map.get((fuel_type or "").lower(), "gasoline")
        # Marka eşleşmesi
        brand_match = None
        for p in prices:
            company = (p.get("company") or "").lower().replace(" ", "")
            if company and (company in st_name or st_name in company):
                brand_match = p
                break
        if brand_match and isinstance(brand_match.get(price_key), (int, float)):
            ppl = brand_match[price_key]
            return {
                "price_per_liter": ppl,
                "company": brand_match.get("company"),
                "fuel_type": fuel_type,
                "city": city,
                "district": district,
                "price_label": f"{ppl:.2f} ₺/L",
            }
        # Brand match yoksa ilçe ortalaması
        valid = [p.get(price_key) for p in prices if isinstance(p.get(price_key), (int, float))]
        if valid:
            avg = sum(valid) / len(valid)
            return {
                "price_per_liter": None,
                "company": None,
                "fuel_type": fuel_type,
                "city": city,
                "district": district,
                "price_label": f"{district or city} ort. ~{avg:.2f} ₺/L",
            }
    except asyncio.TimeoutError:
        log.warning(f"⏱️ [Targeted] fuel_prices timeout @ {city}/{district}")
    except Exception as exc:
        log.warning(f"⚠️ [Targeted] fuel_prices hata: {exc}")
    return None


async def _resolve_one_stop(
    stop: Dict[str, Any],
    polyline: str,
    total_km: float,
    current_lat: Optional[float],
    current_lon: Optional[float],
    fuel_type: str,
    orchestrator,
) -> Optional[Dict[str, Any]]:
    """Tek stop'u gerçek mekana eşle (paralel search + role-based pick)."""
    role = stop.get("role") or "food"
    city = (stop.get("city") or "").strip()
    district = (stop.get("district") or "").strip()
    location_name = district or city
    query = (stop.get("query_hint") or "").strip() or _default_query_for_role(role)
    anchor = stop.get("anchor")
    narrative_token = stop.get("narrative_token") or ""

    places_tool = orchestrator.get_tool_by_name("search_hybrid_places")
    if not places_tool:
        return None

    # ★ Yakıt + home_proximity: ev koordinatı + rota intersect
    use_home_proximity = (
        role == "fuel"
        and anchor == "home_proximity"
        and current_lat is not None
        and current_lon is not None
    )

    base_args: Dict[str, Any] = {"query": query}
    if polyline:
        base_args["route_polyline"] = polyline
    if use_home_proximity:
        # ★ Google handler route_polyline varsa lat/lon'u görmezden gelir.
        # Bu yüzden hem lat/lon hem target_fraction=0.03 (rotanın başı = eve yakın)
        # geçeriz. Search rotanın ilk %3'ünde yapılır, sonuçlar eve yakın olur.
        base_args["lat"] = current_lat
        base_args["lon"] = current_lon
        base_args["target_fraction"] = 0.03
    elif location_name:
        base_args["location_name"] = location_name
        # Anchor=end ise rotanın sonuna yakın arama (varış öncesi)
        if anchor == "end":
            base_args["target_fraction"] = 0.95
        elif anchor == "start":
            base_args["target_fraction"] = 0.05

    async def _call(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            raw = await asyncio.wait_for(places_tool.ainvoke(args), timeout=TARGETED_TIMEOUT_S)
            return _extract_places_from_response(raw)
        except asyncio.TimeoutError:
            log.warning(f"⏱️ [Targeted] {role}/{query!r} @ {location_name} timeout")
            return []
        except Exception as exc:
            log.warning(f"⚠️ [Targeted] {role}/{query!r} hata: {exc}")
            return []

    raw_places = await _call(base_args)

    # Fallback 1: query'siz, jenerik
    if not raw_places:
        fb_args = dict(base_args)
        fb_args["query"] = _generic_fallback_query(role)
        log.info(f"🔁 [Targeted] {role} 0 sonuç, fallback query='{fb_args['query']}'")
        raw_places = await _call(fb_args)

    # Fallback 2: location_name'den city'ye geri çık (eğer district aramayı kısıtlamışsa)
    if not raw_places and district and city and district != city:
        fb_args = dict(base_args)
        fb_args.pop("lat", None)
        fb_args.pop("lon", None)
        fb_args["location_name"] = city
        fb_args["query"] = _generic_fallback_query(role)
        log.info(f"🔁 [Targeted] {role} il fallback: {city}")
        raw_places = await _call(fb_args)

    if not raw_places:
        log.warning(
            f"⚠️ [Targeted] {role}/{narrative_token} {city}/{district} aday bulunamadı"
        )
        return None

    # Normalize
    valid: List[Dict[str, Any]] = []
    seen_keys: set = set()
    for i, p in enumerate(raw_places):
        norm = _normalize_place(p, role, i)
        if not norm:
            continue
        # Rota üstü filter (route_polyline geçtikse deviation ≤ 1200m)
        if polyline and (norm.get("deviation_meters") or 0) > 1500:
            continue
        key = (norm["name"], round(norm["lat"], 4), round(norm["lon"], 4))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        valid.append(norm)

    if not valid:
        # Filter çok katı olduysa tüm adayları kabul et
        valid = [_normalize_place(p, role, i) for i, p in enumerate(raw_places)]
        valid = [v for v in valid if v]

    if not valid:
        return None

    # Role-based seçim
    if role == "fuel":
        best = _pick_best_fuel(valid)
        # ★ Fuel price için mekanın gerçek adresinden city/district çıkar.
        real_city, real_district = _parse_city_district_from_address(
            best.get("address") or "", city, district,
        )
        fp = await _enrich_fuel_price(best, real_city, real_district, fuel_type, orchestrator)
        if fp:
            best["fuel_price"] = fp
        if real_city:
            best["city"] = real_city
        if real_district:
            best["district"] = real_district
    else:
        # ★ FOOD için top-3 (kullanıcı seçim yapsın); break/scenic için top-1
        sorted_valid = sorted(valid, key=_score_food_break, reverse=True)
        top_n = 3 if role == "food" else 1
        bests = sorted_valid[:top_n]
        for b in bests:
            real_city, real_district = _parse_city_district_from_address(
                b.get("address") or "", city, district,
            )
            if real_city:
                b["city"] = real_city
            if real_district:
                b["district"] = real_district
        best = bests[0]
        # Diğer alternatifleri "_alternatives" alanında topla — caller listeye açar
        if len(bests) > 1:
            best["_alternatives"] = bests[1:]

    # Stop metadata'yı resolved mekana + alternatiflere ekle (top-3 food için)
    _type_map = {
        "fuel": "fuel_station", "food": "restaurant",
        "scenic": "scenic_stop", "break": "break_stop",
    }
    _resolved_type = _type_map.get(role, "poi")
    _all_markers = [best] + list(best.get("_alternatives") or [])
    for _mm in _all_markers:
        _mm["role"] = role
        _mm["kind"] = role
        _mm["narrative_token"] = narrative_token
        _mm["city"] = _mm.get("city") or city
        _mm["district"] = _mm.get("district") or district
        _mm["rationale"] = stop.get("rationale") or ""
        _mm["anchor"] = anchor
        _mm["type"] = _resolved_type
    return best


async def collect_targeted_stops(
    stops: List[Dict[str, Any]],
    polyline: str,
    total_km: float,
    current_lat: Optional[float],
    current_lon: Optional[float],
    fuel_type: str,
    orchestrator,
) -> List[Dict[str, Any]]:
    """Tüm stop'ları paralel olarak gerçek mekana eşle.

    Returns: resolved stop listesi. Eşleşmeyen stop'lar atlanır.
    """
    if not stops:
        return []
    tasks = [
        _resolve_one_stop(s, polyline, total_km, current_lat, current_lon, fuel_type, orchestrator)
        for s in stops
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    resolved: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, dict):
            resolved.append(r)
            # ★ food role için _alternatives varsa onları da ekle (top-3 seçim)
            alts = r.pop("_alternatives", None) or []
            for alt in alts:
                # narrative_token paylaşılıyor ama mobile her marker'ı ayrı render edecek
                alt["narrative_token"] = r.get("narrative_token")
                alt["rationale"] = r.get("rationale")
                resolved.append(alt)
        elif isinstance(r, Exception):
            log.warning(f"⚠️ [Targeted] task exception: {r}")
    log.info(
        f"🎯 [Targeted] {len(resolved)} marker / {len(stops)} stop resolve edildi"
    )
    return resolved


# ─────────────────────────────────────────────────────────────────────────────
# Simple Deterministic Stops — Strategist fail olduğunda kullanılır.
# Eski karmaşık RouteStrategyEvaluator (Nominatim sample) YOK; sadece direkt
# search_hybrid_places çağrıları (lat+lon+polyline veya location_name+polyline).
# ─────────────────────────────────────────────────────────────────────────────

_SIMPLE_FOOD_QUERY = {
    "Yöresel Lezzetler": "yöresel restoran lokanta",
    "Fast Food": "fast food hamburger",
    "Ev Yemekleri": "ev yemeği lokanta",
    "Kahve & Tatlı": "kafe kahve tatlı",
    "Fark etmez": "restoran lokanta",
}


_FOOD_LOC_FRACTION = {
    "Başları": 0.20,
    "Ortaları": 0.50,
    "Sonları": 0.80,
}


async def simple_deterministic_stops(
    *,
    custom_note: str,
    food_preference: str,
    food_specific: str,
    food_quality_hint: str,
    food_location: str = "Ortaları",
    scene_filters: List[str],
    break_interval_hours: float,
    destination: str,
    polyline: str,
    total_km: float,
    current_lat: Optional[float],
    current_lon: Optional[float],
    fuel_type: str,
    orchestrator,
    timeout_s: float = 25.0,
) -> List[Dict[str, Any]]:
    """Strategist fail → bu deterministic akış devreye girer.

    Akış:
      - Yakıt "yolun başında" → lat/lon + polyline (eve yakın + rota üstü)
      - Yakıt "sonlara doğru" → destination + polyline
      - Yakıt default → rotanın ortasında
      - Yemek → food_specific varsa onu, yoksa food_preference query → orta fraction
      - Mola → break_interval_hours > 0 ise → 0.4 fraction
    Hepsi paralel. Top-2 sonuç (yakıt için top-3).
    """
    places_tool = orchestrator.get_tool_by_name("search_hybrid_places")
    if not places_tool:
        return []

    # Yakıt anchor parse (basit)
    msg = (custom_note or "").lower()
    anchor_start = any(k in msg for k in [
        "yolun başında", "başta yakıt", "ilk dolum", "çıkar çıkmaz",
        "başlangıçta", "çıkışta yakıt", "evime yakın", "evden yakıt",
    ])
    anchor_end = any(k in msg for k in [
        "sonlara doğru", "yolun sonunda", "son dolum", "varmadan",
        "sona doğru", "varış öncesi", "sonunda yakıt",
    ])

    # Yemek query'sini oluştur
    food_q_parts: List[str] = []
    if food_specific:
        food_q_parts.append(food_specific)
    base_food_q = _SIMPLE_FOOD_QUERY.get(food_preference, "restoran lokanta")
    food_q_parts.append(base_food_q)
    if food_quality_hint:
        food_q_parts.append(food_quality_hint)
    for s in (scene_filters or []):
        if isinstance(s, str) and s and s not in food_q_parts:
            food_q_parts.append(s)
    food_query = " ".join(dict.fromkeys(food_q_parts)).strip() or "restoran"

    # Task'leri hazırla
    task_specs: List[Dict[str, Any]] = []

    # Yakıt
    if anchor_start and current_lat and current_lon:
        task_specs.append({
            "role": "fuel",
            "anchor": "home_proximity",
            "args": {
                "query": "benzin istasyonu",
                "lat": current_lat, "lon": current_lon,
                "route_polyline": polyline,
                "target_fraction": 0.03,  # ★ rotanın başı = eve yakın
            },
            "take": 2,
        })
    if anchor_end:
        task_specs.append({
            "role": "fuel",
            "anchor": "end",
            "args": {
                "query": "benzin istasyonu",
                "location_name": destination,
                "route_polyline": polyline,
                "target_fraction": 0.95,  # ★ rotanın sonu = varış öncesi
            },
            "take": 1,
        })
    if not anchor_start and not anchor_end:
        # Ortada bir yakıt
        task_specs.append({
            "role": "fuel",
            "anchor": None,
            "args": {
                "query": "benzin istasyonu",
                "route_polyline": polyline,
                "target_fraction": 0.5,
            },
            "take": 2,
        })

    # Yemek — food_location'a göre fraction (Sonları=0.8, Başları=0.2)
    food_frac = _FOOD_LOC_FRACTION.get((food_location or "Ortaları").strip(), 0.5)
    task_specs.append({
        "role": "food",
        "anchor": None,
        "args": {
            "query": food_query,
            "route_polyline": polyline,
            "target_fraction": food_frac,
        },
        "take": 3,
    })

    # Mola — break istenmişse
    if break_interval_hours > 0:
        break_q_parts = ["kafe", "dinlenme tesisi", "mola"]
        for s in (scene_filters or []):
            if isinstance(s, str) and s and s not in break_q_parts:
                break_q_parts.append(s)
        task_specs.append({
            "role": "break",
            "anchor": None,
            "args": {
                "query": " ".join(break_q_parts),
                "route_polyline": polyline,
                "target_fraction": 0.4,
            },
            "take": 2,
        })

    async def _run_one(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            raw = await asyncio.wait_for(
                places_tool.ainvoke(spec["args"]),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning(f"⏱️ [SimpleDet] {spec['role']} timeout")
            return []
        except Exception as exc:
            log.warning(f"⚠️ [SimpleDet] {spec['role']} hata: {exc}")
            return []
        raw_places = _extract_places_from_response(raw)
        normalized: List[Dict[str, Any]] = []
        seen: set = set()
        for i, p in enumerate(raw_places):
            n = _normalize_place(p, spec["role"], i)
            if not n:
                continue
            if polyline and (n.get("deviation_meters") or 0) > 1500:
                continue
            key = (n["name"], round(n["lat"], 4), round(n["lon"], 4))
            if key in seen:
                continue
            seen.add(key)
            normalized.append(n)
        # Sort: rating-dominant + deviation
        normalized.sort(key=lambda p: -_score_food_break(p))
        for p in normalized:
            p["role"] = spec["role"]
            p["kind"] = spec["role"]
            p["anchor"] = spec["anchor"]
            p["type"] = {
                "fuel": "fuel_station",
                "food": "restaurant",
                "break": "break_stop",
                "scenic": "scenic_stop",
            }.get(spec["role"], "poi")
        return normalized[:int(spec.get("take") or 2)]

    raw_groups = await asyncio.gather(*[_run_one(s) for s in task_specs])
    out: List[Dict[str, Any]] = []
    for group in raw_groups:
        out.extend(group)

    # Fuel için fiyat enrich (varsa kullanıcı yakıt fiyatı önemli — async best-effort)
    fuel_results = [r for r in out if r.get("role") == "fuel"]
    if fuel_results:
        async def _try_enrich(fuel_marker: Dict[str, Any]):
            city, district = _parse_city_district_from_address(
                fuel_marker.get("address") or "", "", "",
            )
            if not city and not district:
                return
            try:
                fp = await _enrich_fuel_price(fuel_marker, city, district, fuel_type, orchestrator)
                if fp:
                    fuel_marker["fuel_price"] = fp
                if city:
                    fuel_marker["city"] = city
                if district:
                    fuel_marker["district"] = district
            except Exception:
                pass

        await asyncio.gather(*[_try_enrich(f) for f in fuel_results], return_exceptions=True)

    log.info(
        f"📊 [SimpleDet] {len(out)} marker ürettim "
        f"(fuel={sum(1 for r in out if r['role']=='fuel')}, "
        f"food={sum(1 for r in out if r['role']=='food')}, "
        f"break={sum(1 for r in out if r['role']=='break')}) "
        f"anchor_start={anchor_start} anchor_end={anchor_end}"
    )
    return out


def build_simple_narrative(
    *,
    origin: str,
    destination: str,
    total_km: float,
    total_min: int,
    eta_display: str,
    resolved: List[Dict[str, Any]],
    custom_note: str,
    food_specific: str,
    weather_zones: List[Dict[str, Any]],
) -> str:
    """Strategist fail durumunda: deterministic stop'lardan akıcı bir narrative üret.
    Markdown, mekan adlarını **kalın** + km bilgisi.
    """
    hours = total_min // 60
    mins = total_min % 60
    lines: List[str] = []
    lines.append(f"**{origin} → {destination}** — **{int(total_km)} km**, ~**{hours} sa {mins} dk**, varış **{eta_display}**.")
    lines.append("")

    by_role: Dict[str, List[Dict[str, Any]]] = {"fuel": [], "food": [], "break": []}
    for r in resolved:
        role = r.get("role", "food")
        by_role.setdefault(role, []).append(r)

    if by_role["fuel"]:
        lines.append("### ⛽ Yakıt")
        for f in by_role["fuel"][:3]:
            anchor = f.get("anchor")
            anchor_note = ""
            if anchor == "home_proximity":
                anchor_note = "yola çıkar çıkmaz · "
            elif anchor == "end":
                anchor_note = "varış öncesi son dolum · "
            km = f.get("distance_along_route_km")
            km_str = f"~{int(km)}. km" if km else "rota üstü"
            fp = f.get("fuel_price") or {}
            price = fp.get("price_per_liter")
            price_str = f" · **{price:.2f} ₺/L**" if isinstance(price, (int, float)) else ""
            lines.append(f"- {anchor_note}**{f.get('name')}** ({km_str}){price_str}")
        lines.append("")

    if by_role["food"]:
        food_label = f" ({food_specific})" if food_specific else ""
        lines.append(f"### 🍽️ Yemek{food_label}")
        for f in by_role["food"][:3]:
            km = f.get("distance_along_route_km")
            km_str = f"~{int(km)}. km" if km else "rota üstü"
            rating = f.get("rating")
            rating_str = f" · ⭐ {rating}" if rating else ""
            lines.append(f"- **{f.get('name')}** ({km_str}){rating_str}")
        lines.append("")

    if by_role["break"]:
        lines.append("### ☕ Mola")
        for f in by_role["break"][:2]:
            km = f.get("distance_along_route_km")
            km_str = f"~{int(km)}. km" if km else "rota üstü"
            lines.append(f"- **{f.get('name')}** ({km_str})")
        lines.append("")

    if weather_zones:
        lines.append("### 🌤️ Hava")
        for z in weather_zones[:4]:
            if isinstance(z, dict):
                emoji = z.get("emoji", "🌤️")
                lines.append(f"- {emoji} **{z.get('cities')}** · {z.get('condition')} ({z.get('km_range')})")
        lines.append("")

    if custom_note:
        lines.append(f"_Not_: {custom_note}")

    lines.append("\nİyi yolculuklar dostum.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Legacy Search Plan Builder
# ─────────────────────────────────────────────────────────────────────────────

_LEGACY_FRACTION_MAP = {
    "Başları": 0.2,
    "Ortaları": 0.5,
    "Sonları": 0.8,
}
_LEGACY_FOOD_QUERY = {
    "Yöresel Lezzetler": "yöresel restoran lokanta",
    "Fast Food": "fast food hamburger",
    "Ev Yemekleri": "ev yemeği lokanta",
    "Kahve & Tatlı": "kafe kahve tatlı",
    "Fark etmez": "restoran",
}


def build_legacy_search_plan(request) -> List[Dict[str, Any]]:
    """LLM-1 search_plan üretmediyse eski deterministik mantığı item listesine çevir.

    request: TripPlanRequest pydantic instance veya dict.
    """
    rq = request if isinstance(request, dict) else request.model_dump()
    plan: List[Dict[str, Any]] = []

    # Food
    food_pref = rq.get("food_preference") or "Fark etmez"
    food_loc = (rq.get("food_location") or "Ortaları").strip()
    food_specific = (rq.get("food_specific") or "").strip()
    quality_hint = (rq.get("food_quality_hint") or "").strip()
    scene_filters = rq.get("scene_filters") or []
    base_query = _LEGACY_FOOD_QUERY.get(food_pref, "restoran")
    query_parts = []
    if food_specific:
        query_parts.append(food_specific)
    query_parts.append(base_query)
    if quality_hint and quality_hint not in query_parts:
        query_parts.append(quality_hint)
    for s in scene_filters:
        if isinstance(s, str) and s and s not in query_parts:
            query_parts.append(s)
    food_query = " ".join(query_parts).strip() or "restoran"

    if food_loc in _LEGACY_FRACTION_MAP:
        frac = _LEGACY_FRACTION_MAP[food_loc]
        plan.append({
            "kind": "food",
            "query": food_query,
            "fraction_range": [max(0.0, frac - 0.1), min(1.0, frac + 0.1)],
            "max_results": 12,
        })
    elif food_loc and food_loc != "Fark etmez":
        plan.append({
            "kind": "food",
            "query": food_query,
            "region_hint": {"city": food_loc},
            "max_results": 12,
        })
    else:
        plan.append({
            "kind": "food",
            "query": food_query,
            "fraction_range": [0.4, 0.6],
            "max_results": 12,
        })

    # Break (mola) — scene_filters'a göre query zenginleştirme
    break_query_parts = ["dinlenme tesisi", "kafe", "mola"]
    for s in scene_filters:
        if isinstance(s, str) and s and s not in break_query_parts:
            break_query_parts.append(s)
    plan.append({
        "kind": "break",
        "query": " ".join(break_query_parts),
        "fraction_range": [0.3, 0.7],
        "max_results": 15,
    })

    # Scenic — sadece scene_filters varsa
    if any(s in ("manzaralı", "sahil", "doğa", "manzara") for s in scene_filters):
        scenic_q = " ".join([s for s in scene_filters if isinstance(s, str)]) + " manzara seyir noktası"
        plan.append({
            "kind": "scenic",
            "query": scenic_q.strip(),
            "fraction_range": [0.2, 0.8],
            "max_results": 8,
        })

    # Fuel — anchor custom_note'tan parse edilecek, burada plan item'ı koy
    custom = (rq.get("custom_note") or "").lower()
    anchor = None
    if any(k in custom for k in ("yolun başında", "başta yakıt", "ilk dolum", "çıkışta yakıt")):
        anchor = "start"
    elif any(k in custom for k in ("sonlara doğru", "yolun sonunda", "son dolum", "varmadan")):
        anchor = "end"
    plan.append({
        "kind": "fuel",
        "query": "benzin istasyonu",
        "anchor": anchor,
        "max_results": 16,
    })
    return plan
