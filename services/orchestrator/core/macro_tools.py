import asyncio
import json
import math
import httpx
from loguru import logger as log
from typing import Dict, Any, List, Optional, Tuple

try:
    import flexpolyline as _flex
except Exception:
    _flex = None
try:
    import polyline as _polyline_lib  # type: ignore
except Exception:
    _polyline_lib = None


# ──────────────────────────────────────────────────────────────────────────
# Yakıt algoritması yardımcıları
# ──────────────────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _decode_polyline(polyline_str: str) -> List[Tuple[float, float]]:
    """Polyline → [(lat, lon)] decode (flexpolyline → fallback polyline lib)."""
    if not polyline_str:
        return []
    try:
        if _flex:
            return [(float(p[0]), float(p[1])) for p in _flex.decode(polyline_str)]
    except Exception:
        pass
    try:
        if _polyline_lib:
            return [(float(p[0]), float(p[1])) for p in _polyline_lib.decode(polyline_str)]
    except Exception:
        pass
    return []


def _sample_polyline_at_km(
    polyline_str: str, start_km: float, end_km: float, every_km: float = 20.0
) -> List[Dict[str, float]]:
    """Polyline'ı decode edip [start_km, end_km] arasında every_km arayla noktalar döner."""
    pts = _decode_polyline(polyline_str)
    if len(pts) < 2 or end_km <= start_km:
        return []
    samples: List[Dict[str, float]] = []
    cum_km = 0.0
    target = max(start_km, 0.0)
    for i in range(1, len(pts)):
        seg_km = _haversine_km(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
        next_cum = cum_km + seg_km
        while target <= next_cum and target <= end_km:
            t = (target - cum_km) / seg_km if seg_km > 1e-6 else 0.0
            lat = pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t
            lon = pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t
            samples.append({"lat": lat, "lon": lon, "km": target})
            target += every_km
            if target > end_km:
                break
        cum_km = next_cum
        if cum_km > end_km:
            break
    return samples


# Modül-seviyesi cache: aynı koordinatları tekrar reverse-geocode etmeyiz.
_REVERSE_GEOCODE_CACHE: Dict[Tuple[float, float], Optional[Dict[str, str]]] = {}


async def _reverse_geocode_district(
    lat: float, lon: float, client: httpx.AsyncClient
) -> Optional[Dict[str, str]]:
    """Nominatim reverse geocoding → {city, district} or None. Cache'li."""
    key = (round(lat, 2), round(lon, 2))
    if key in _REVERSE_GEOCODE_CACHE:
        return _REVERSE_GEOCODE_CACHE[key]
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": lat,
        "lon": lon,
        "format": "json",
        # zoom=12 → ilçe (admin_level=6) ayrımı keskinleşir. Önceki 10 değeri
        # Atakum/Tekkeköy gibi komşu ilçelerin karışmasına yol açıyordu.
        "zoom": 12,
        "accept-language": "tr",
    }
    try:
        resp = await client.get(url, params=params, timeout=10.0)
        if resp.status_code != 200:
            log.warning(
                f"⚠️ [ReverseGeocode] HTTP {resp.status_code} - body: {resp.text[:200]!r}"
            )
            _REVERSE_GEOCODE_CACHE[key] = None
            return None
        data = resp.json()
        addr = data.get("address", {}) or {}
        city = (
            addr.get("province")
            or addr.get("state")
            or addr.get("city")
            or ""
        ).strip()
        district = (
            addr.get("county")
            or addr.get("town")
            or addr.get("district")
            or addr.get("suburb")
            or "merkez"
        ).strip()
        for suffix in (" Province", " Il", " ili", " İli"):
            if city.endswith(suffix):
                city = city[: -len(suffix)].strip()
        if not city:
            log.warning(
                f"⚠️ [ReverseGeocode] city boş — addr keys: {list(addr.keys())} "
                f"data: {str(data)[:300]}"
            )
            _REVERSE_GEOCODE_CACHE[key] = None
            return None
        result = {"city": city, "district": district}
        _REVERSE_GEOCODE_CACHE[key] = result
        log.info(f"📍 [ReverseGeocode] {lat:.4f},{lon:.4f} → {result}")
        return result
    except Exception as e:
        log.warning(f"⚠️ [ReverseGeocode] {lat},{lon} EXCEPTION: {type(e).__name__}: {e}")
        _REVERSE_GEOCODE_CACHE[key] = None
        return None


class RouteStrategyEvaluator:
    """
    Kullanıcının rotasını hesaplayıp, o rota üzerindeki şehirlerde yakıt fiyatlarını
    sorgulayan ve fiyat/performans (eta/sapma) analizi yapan Macro-Tool yöneticisi.
    Bu sınıf Orchestrator'da barındırılır çünkü hem 'mcp_city' hem 'mcp_intel' ile konuşur.

    Feature 1: Rota polyline'ı oluşturulduktan sonra midpoint üzerinden benzinlik arar.
    Feature 3: mcp_intel pipeline — her istasyona gerçek akaryakıt fiyatı inject eder,
               sonuçları ucuzdan pahalıya sıralar.
    """

    def __init__(self, mcp_client):
        self.mcp = mcp_client

    async def _safe_call(self, service: str, tool_name: str, args: dict) -> dict:
        """MCP RPC call with error handling. Uses orchestrator's _process_mcp_result."""
        try:
            res = await self.mcp.mcp_rpc_call(service, "tools/call", {"name": tool_name, "arguments": args})
            if isinstance(res, dict):
                return res
            return {"status": "error", "message": f"Unexpected response type: {type(res)}"}
        except Exception as e:
            msg = f"API Error in {tool_name}: {str(e)}"
            log.error(msg)
            return {"status": "error", "message": msg}

    async def evaluate(
        self,
        origin: str,
        destination: str,
        fuel_type: str = "benzin",
        fuel_range: Optional[float] = None,
        polyline: Optional[str] = None,
        total_dist_km: Optional[float] = None,
        anchor_at_start: bool = False,
        anchor_at_end: bool = False,
    ) -> Dict[str, Any]:
        """
        İlçe-bazlı yakıt önerisi:
        cur_km'den safe_max=fuel_range*0.85 mesafesine kadar olan segmentteki
        ilçeleri reverse-geocode et → paralel fiyat çek → en ucuz 2-3 ilçe →
        o ilçelerde yol üstü istasyonları topla → best + alternatives.
        Dolum yapıldıktan sonra cur_km güncellenir, döngü devam eder.
        """
        log.info(f"🧠 [Macro-Tool] Yakıt Stratejisi Başlatıldı: {origin} -> {destination}")

        # ─── 1. BASE ROUTE ─────────────────────────────────────────────────
        if polyline and total_dist_km is not None:
            safe_route_summary = {
                "distance_km": total_dist_km,
                "polyline": polyline,
                "status": "success",
            }
            total_dist = float(total_dist_km)
        else:
            route_res = await self._safe_call(
                "city", "get_route_data", {"origin": origin, "destination": destination}
            )
            if "error" in route_res or route_res.get("status") == "error":
                return {
                    "status": "error",
                    "message": f"Rota oluşturulamadı: {route_res.get('message', route_res.get('error', 'Bilinmeyen hata'))}",
                }
            polyline = route_res.get("polyline") or route_res.get("polyline_encoded", "")
            if not polyline or "GİZLENDİ" in str(polyline) or "HARİTA" in str(polyline):
                return {"status": "error", "message": "Geçerli bir polyline bulunamadı."}
            safe_route_summary = {
                "distance_km": route_res.get("mesafe_km") or route_res.get("distance_km"),
                "duration_min": route_res.get("sure_dk") or route_res.get("duration_min"),
                "polyline": polyline,
                "status": "success",
            }
            total_dist_raw = route_res.get("mesafe_km", 0) or route_res.get("distance_km", 0)
            try:
                total_dist = float(total_dist_raw)
            except Exception:
                total_dist = 500.0

        # ─── 2. KÜMÜLATIF DOLUM MANTIĞI ─────────────────────────────────────
        # Kullanıcının gerçek menzili: her dolumdan sonra menzil tazelenir.
        # safe_max = fuel_range * 0.85 → %15 tampon, kullanıcı menzil sınırını aşmasın.
        if not fuel_range or fuel_range <= 0:
            fuel_range = 400.0  # default
        safe_max = float(fuel_range) * 0.85
        type_key_map = {
            "benzin": "gasoline",
            "motorin": "diesel",
            "dizel": "diesel",
            "lpg": "lpg",
        }
        price_key = type_key_map.get(fuel_type.lower(), "gasoline")

        stops_by_km: List[dict] = []
        all_enriched: List[dict] = []
        seen_station_keys: set = set()

        cur_km = 0.0
        iteration_safety = 0

        # 10. Tur — Iteration sınırını dinamik yap: rota uzunluğu / menzil + 1
        # Eski: sabit 6 — uzun rotalarda fazla, kısa rotalarda yeterli
        # Yeni: 600km/400km = 2, 1200km/400km = 4 — gerçek ihtiyaca göre
        _max_iter = max(2, int(total_dist / max(float(fuel_range) or 400.0, 200.0)) + 1)

        # Tek bir httpx client'ı tüm reverse-geocode çağrıları için paylaş
        nom_headers = {"User-Agent": "GeoIntel_Orchestrator/4.0"}
        async with httpx.AsyncClient(headers=nom_headers) as nom_client:
            while cur_km + 20 < total_dist and iteration_safety < _max_iter:
                iteration_safety += 1
                segment_end = min(cur_km + safe_max, total_dist - 5)

                # 14. Tur — Kullanıcı "yolun başında yakıt" dediyse ilk
                # segment'i 60 km'ye sınırla (segment'in EN UCUZ ilçesi
                # genelde 100-160 km'ye düşüyordu; bu mantığı bozar).
                if anchor_at_start and iteration_safety == 1:
                    segment_end = min(cur_km + 60, segment_end)
                    log.info(
                        f"⛽ [Fuel/anchor=start] İlk segment 60km'ye sınırlandı"
                    )
                # 14. Tur — "Sona doğru yakıt" → son segment'te varış
                # öncesi 60 km'lik aralıkta ara
                if anchor_at_end and iteration_safety > 1:
                    # Eğer şu anki segment varış'tan uzaksa atla — sadece
                    # son segment için anchor uygula
                    distance_to_end = total_dist - cur_km
                    if distance_to_end > 100:
                        # Henüz son segment değil, normal akış
                        pass
                    else:
                        # Son segment — son 60 km'yi tara
                        segment_end = min(cur_km + 60, total_dist - 5)
                        log.info(
                            f"⛽ [Fuel/anchor=end] Son segment 60km'ye sınırlandı"
                        )

                if segment_end - cur_km < 40:
                    # Çok kısa segment → son dolum gerekmez
                    break

                # 2a. Polyline'ı örnekle
                # 10. Tur — sample_start 30→5: yol başında (Atakum çıkışı,
                # Tekkeköy/Çarşamba) istasyon önerebilelim
                sample_start = cur_km + 5
                if sample_start >= segment_end:
                    break
                samples = _sample_polyline_at_km(
                    polyline, sample_start, segment_end, every_km=20.0
                )
                if not samples:
                    break

                # 2b. Her örneği reverse-geocode et → benzersiz (city, district) seti
                unique_districts: Dict[Tuple[str, str], Dict[str, float]] = {}
                for s in samples:
                    rg = await _reverse_geocode_district(s["lat"], s["lon"], nom_client)
                    if rg:
                        key = (rg["city"], rg["district"])
                        if key not in unique_districts:
                            unique_districts[key] = s
                    # 10. Tur — Nominatim rate limit gevşetildi (0.4→0.1).
                    # Cache hit oranı yüksek; gerçek API hit'lerde 0.1 yine
                    # 10 req/sec limitini aşmaz.
                    await asyncio.sleep(0.1)

                if not unique_districts:
                    log.warning(
                        f"⛽ [Fuel] cur_km={cur_km:.0f} reverse geocode başarısız, segment atlanıyor"
                    )
                    cur_km = segment_end
                    continue

                log.info(
                    f"⛽ [Fuel] cur_km={cur_km:.0f}..{segment_end:.0f}km — "
                    f"{len(unique_districts)} ilçe: {list(unique_districts.keys())}"
                )

                # 2c. Paralel fiyat çek
                district_list = list(unique_districts.keys())

                async def _get_district_prices(city: str, district: str) -> list:
                    res = await self._safe_call(
                        "intel",
                        "get_fuel_prices",
                        {"city": city, "district": district},
                    )
                    return res.get("data", []) if isinstance(res, dict) else []

                price_results = await asyncio.gather(
                    *[_get_district_prices(c, d) for c, d in district_list]
                )

                # 2d. En ucuz ilçeler — min fiyat (target fuel_type)
                district_min: List[dict] = []
                for (city, dist), prices in zip(district_list, price_results):
                    valid = [
                        p
                        for p in prices
                        if isinstance(p.get(price_key), (int, float))
                        and p.get(price_key, 0) > 5
                    ]
                    if valid:
                        min_p = min(p.get(price_key, 999) for p in valid)
                        district_min.append({
                            "city": city,
                            "district": dist,
                            "min_price": min_p,
                            "prices": valid,
                            "sample": unique_districts[(city, dist)],
                        })

                district_min.sort(key=lambda x: x["min_price"])
                # Anchor filter (refactor): "yolun başında" derse en ucuz olsa bile
                # 80km dışındaki ilçeleri at; "sonlara doğru" derse varış öncesi 80km dışı at.
                if anchor_at_start and iteration_safety == 1:
                    _filtered = [d for d in district_min if d["sample"]["km"] <= 80]
                    if _filtered:
                        district_min = _filtered
                        log.info(
                            f"⛽ [Fuel/anchor=start] İlçe filtresi: "
                            f"{len(district_min)} ilçe ≤80km'de kaldı"
                        )
                if anchor_at_end and iteration_safety > 1:
                    distance_to_end = total_dist - cur_km
                    if distance_to_end <= 100:
                        _filtered = [
                            d for d in district_min
                            if d["sample"]["km"] >= total_dist - 80
                        ]
                        if _filtered:
                            district_min = _filtered
                            log.info(
                                f"⛽ [Fuel/anchor=end] İlçe filtresi: "
                                f"{len(district_min)} ilçe son 80km'de kaldı"
                            )
                cheap_districts = district_min[:3]

                if not cheap_districts:
                    # Fiyat verisi olmayan ilçeler — yine istasyon ara (fiyatsız)
                    cheap_districts = [
                        {
                            "city": c,
                            "district": d,
                            "min_price": None,
                            "prices": [],
                            "sample": unique_districts[(c, d)],
                        }
                        for c, d in district_list[:3]
                    ]

                # 2e. Bu ilçelerde rota üstü istasyon ara
                async def _search_in_district(cd: dict) -> list:
                    s = cd["sample"]
                    fraction = (s["km"] / total_dist) if total_dist > 0 else 0.5
                    res = await self._safe_call(
                        "city",
                        "search_hybrid_places",
                        {
                            "query": f"{fuel_type} istasyonu",
                            "lat": s["lat"],
                            "lon": s["lon"],
                            "route_polyline": polyline,
                            "target_fraction": fraction,
                        },
                    )
                    if isinstance(res, dict):
                        return (
                            res.get("places")
                            or res.get("strict_route_places", [])
                            + res.get("relaxed_route_places", [])
                        )
                    return []

                station_results = await asyncio.gather(
                    *[_search_in_district(cd) for cd in cheap_districts]
                )

                # 2f. Aday istasyonları topla + fiyat eşle
                def _norm_brand(s: str) -> str:
                    return s.lower().replace(" ", "").replace("-", "").replace("_", "")

                candidates: List[dict] = []
                for cd, places in zip(cheap_districts, station_results):
                    for p in places or []:
                        if not isinstance(p, dict):
                            continue
                        d_km = p.get("distance_along_route_km") or 0
                        dev = p.get("deviation_meters", 9999) or 9999
                        if not (cur_km < d_km <= segment_end):
                            continue
                        if dev > 500:
                            continue

                        # Marka eşleşmesi → varsa o fiyatı kullan
                        st_norm = _norm_brand(p.get("name", ""))
                        brand_match = next(
                            (
                                pr
                                for pr in cd["prices"]
                                if _norm_brand(pr.get("company", "")) in st_norm
                                or st_norm in _norm_brand(pr.get("company", ""))
                            ),
                            None,
                        )
                        if brand_match:
                            p["fuel_price"] = {
                                "price_per_liter": brand_match.get(price_key),
                                "company": brand_match.get("company"),
                                "fuel_type": fuel_type,
                                "city": cd["city"],
                                "district": cd["district"],
                                "price_label": f"{brand_match.get(price_key):.2f} ₺/L",
                            }
                            p["fuel_price_enriched"] = True
                        else:
                            label = "Fiyat bilgisi yok"
                            if cd["min_price"]:
                                label = (
                                    f"{cd['district']} ortalama ~"
                                    f"{cd['min_price']:.2f} ₺/L"
                                )
                            p["fuel_price"] = {
                                "price_per_liter": None,
                                "company": None,
                                "fuel_type": fuel_type,
                                "city": cd["city"],
                                "district": cd["district"],
                                "price_label": label,
                            }
                            p["fuel_price_enriched"] = False

                        p["_district_min_price"] = cd["min_price"]
                        candidates.append(p)

                if not candidates:
                    log.info(
                        f"⛽ [Fuel] cur_km={cur_km:.0f} aday yok, segment ilerletiliyor"
                    )
                    cur_km = segment_end
                    continue

                # 2g. Sırala: ilçe min fiyatı, sonra deviation
                def _candidate_key(p):
                    mp = p.get("_district_min_price")
                    fp = p.get("fuel_price") or {}
                    own_price = fp.get("price_per_liter")
                    # Önce marka fiyatı olanlar, sonra ilçe ortalaması
                    price = own_price if isinstance(own_price, (int, float)) else (mp or 9999)
                    dev = p.get("deviation_meters") or 9999
                    return (price, dev)

                candidates.sort(key=_candidate_key)

                # Tekrarları ele
                deduped: List[dict] = []
                for p in candidates:
                    k = (p.get("name"), round(p.get("lat", 0), 4), round(p.get("lon", 0), 4))
                    if k in seen_station_keys:
                        continue
                    seen_station_keys.add(k)
                    deduped.append(p)

                if not deduped:
                    cur_km = segment_end
                    continue

                best = deduped[0]
                alternatives_list = deduped[1:5]

                stops_by_km.append({
                    "stop_target_km": round(
                        best.get("distance_along_route_km") or (cur_km + safe_max * 0.9), 1
                    ),
                    "best": {
                        "name": best.get("name"),
                        "address": best.get("address"),
                        "lat": best.get("lat"),
                        "lon": best.get("lon"),
                        "distance_along_route_km": best.get("distance_along_route_km"),
                        "deviation_meters": best.get("deviation_meters"),
                        "fuel_price": best.get("fuel_price"),
                    },
                    "alternatives": [
                        {
                            "name": s.get("name"),
                            "address": s.get("address"),
                            "lat": s.get("lat"),
                            "lon": s.get("lon"),
                            "distance_along_route_km": s.get("distance_along_route_km"),
                            "deviation_meters": s.get("deviation_meters"),
                            "fuel_price": s.get("fuel_price"),
                        }
                        for s in alternatives_list
                    ],
                    "cheap_districts": [
                        {
                            "city": cd["city"],
                            "district": cd["district"],
                            "min_price": cd["min_price"],
                        }
                        for cd in cheap_districts
                    ],
                })

                all_enriched.extend(deduped)

                # 2h. Sonraki segment: cur_km = seçilen istasyonun km'si
                new_km = best.get("distance_along_route_km")
                if not isinstance(new_km, (int, float)) or new_km <= cur_km:
                    log.info(
                        f"⛽ [Fuel] cur_km ilerleyemedi (new_km={new_km}), döngü kırılıyor"
                    )
                    break
                cur_km = float(new_km)

        if not all_enriched:
            return {
                "status": "success",
                "route": safe_route_summary,
                "analysis": "Rota üzerinde uygun istasyon bulunamadı.",
                "stops_by_km": [],
            }

        # Geriye uyumluluk: best_station + cheapest_city + tüm enriched
        best_station = stops_by_km[0]["best"] if stops_by_km else {}
        all_cities: Dict[str, float] = {}
        for p in all_enriched:
            fp = p.get("fuel_price") or {}
            ppl = fp.get("price_per_liter")
            if isinstance(ppl, (int, float)) and fp.get("city"):
                # En düşük fiyatı tut
                cur = all_cities.get(fp["city"])
                if cur is None or ppl < cur:
                    all_cities[fp["city"]] = ppl
        cheapest_city = min(all_cities, key=all_cities.get) if all_cities else None

        return {
            "status": "success",
            "route_summary": {
                "distance": safe_route_summary.get("distance_km"),
                "duration": safe_route_summary.get("duration_min"),
            },
            "polyline": polyline,
            "cheapest_fuel_city": {
                "city": cheapest_city,
                "price": all_cities.get(cheapest_city) if cheapest_city else None,
            },
            "best_station_recommendation": {
                "name": best_station.get("name"),
                "address": best_station.get("address"),
                "open_now": best_station.get("is_open", best_station.get("open_now")),
                "open_at_arrival": best_station.get("open_at_arrival"),
                "rating": best_station.get("rating"),
                "lat": best_station.get("lat"),
                "lon": best_station.get("lon"),
                "fuel_price": best_station.get("fuel_price"),
            },
            "stops_by_km": stops_by_km,
            "all_analyzed_cities": all_cities,
            "stations_found": len(all_enriched),
            "places": all_enriched[:16],
        }


class ContextAwarePOIPlanner:
    """
    Macro-Tool: Anlamsal (semantic) mekan araması yapar, hava durumunu kontrol eder
    ve hava durumuna göre (rain_factor) filtrelenmiş bir rota çizer.
    """

    def __init__(self, mcp_client):
        self.mcp = mcp_client

    async def _safe_call(self, service: str, tool_name: str, args: dict) -> dict:
        try:
            res = await self.mcp.mcp_rpc_call(service, "tools/call", {"name": tool_name, "arguments": args})
            if isinstance(res, dict):
                return res
            if isinstance(res, str):
                try:
                    return json.loads(res)
                except Exception:
                    return {"status": "success", "data": res}
            return {"status": "error", "message": f"Beklenmeyen tür: {type(res)}"}
        except Exception as e:
            log.error(f"API Error in {tool_name}: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def evaluate(
        self,
        current_lat: float,
        current_lon: float,
        semantic_query: str,
        location_name: Optional[str] = None,
        search_radius: float = 5000,
        username: str = "test_pilot",
    ) -> Dict[str, Any]:
        log.info(
            f"🧠 [Macro-Tool] ContextAwarePOIPlanner: '{semantic_query}' @ "
            f"{location_name or (str(current_lat) + ',' + str(current_lon))}"
        )

        # 1. Koordinat Çözümleme
        search_lat, search_lon = current_lat, current_lon
        if location_name:
            geo_res = await self._safe_call(
                "city",
                "search_hybrid_places",
                {"query": location_name, "location_name": location_name, "limit": 1},
            )
            places_data = geo_res.get("places", []) if isinstance(geo_res, dict) else []
            if places_data:
                search_lat = float(places_data[0]["lat"])
                search_lon = float(places_data[0]["lon"])

        # 2. Hava Durumu Analizi
        weather_res = await self._safe_call("city", "get_weather", {"lat": search_lat, "lon": search_lon})
        weather_condition = "Açık"
        is_bad_weather = False

        weather_temp = "?"
        rain_prob_pct = 0
        if "error" not in weather_res:
            weather_data = weather_res.get("data", weather_res)
            if isinstance(weather_data, dict) and "ANLIK_DURUM" in weather_data:
                condition_raw = weather_data["ANLIK_DURUM"].get("durum", "").lower()
                weather_condition = condition_raw
                weather_temp = weather_data["ANLIK_DURUM"].get("sicaklik", "?")
                hourly = weather_data.get("ONUMUZDEKI_SAATLER", [])
                if hourly:
                    try:
                        rain_prob_pct = int(float(hourly[0].get("pop", 0)) * 100)
                    except (TypeError, ValueError):
                        rain_prob_pct = 0
                if any(w in condition_raw for w in ["rain", "drizzle", "thunderstorm", "yağmur", "kar", "snow", "fırtına"]):
                    is_bad_weather = True
                    if "kapalı" not in semantic_query.lower() and "iç" not in semantic_query.lower():
                        semantic_query += " kapalı mekan"
                    search_radius = 2000

        # 3. HİBRİT ARAMA (Google + OSM + RAG)
        poi_res = await self._safe_call(
            "city",
            "search_hybrid_places",
            {
                "query": semantic_query,
                "lat": search_lat,
                "lon": search_lon,
                "location_name": location_name,
                "category": "commercial",
            },
        )

        places = poi_res.get("places", []) if isinstance(poi_res, dict) else []

        if not places:
            return {
                "status": "error",
                "message": f"Kritere uygun mekan bulunamadı. ({semantic_query})",
                "weather_context": weather_condition,
                "suggestion": "Daha genel bir arama yapmayı deneyin veya farklı bir bölge seçin.",
            }

        # 4. En uygun olanı seç ve rota çiz
        best_poi = places[0]
        route_res = await self._safe_call(
            "city",
            "get_route_data",
            {
                "origin": f"{current_lat},{current_lon}",
                "destination": f"{best_poi['lat']},{best_poi['lon']}",
            },
        )

        markers = [
            {
                "name": p.get("name"),
                "lat": p.get("lat"),
                "lon": p.get("lon"),
                "description": p.get("address", p.get("fusion_status", "")),
                "type": "poi",
            }
            for p in places[:5]
        ]

        return {
            "status": "success",
            "intent_analyzed": semantic_query,
            "weather_analysis": {
                "condition": weather_condition,
                "temperature": weather_temp,
                "rain_probability_pct": rain_prob_pct,
                "is_bad_weather": is_bad_weather,
                "impact": "Kapalı mekanlar önceliklendirildi." if is_bad_weather else "Normal arama.",
            },
            "recommendation": {
                "name": best_poi.get("name"),
                "address": best_poi.get("address"),
                "rating": best_poi.get("rating"),
                "fusion_status": best_poi.get("fusion_status"),
            },
            "map": {
                "markers": markers,
                "polyline": route_res.get("polyline") if isinstance(route_res, dict) else "",
            },
            "route_summary": {
                "distance": route_res.get("mesafe_km"),
                "duration": route_res.get("sure_dk"),
            },
            "alternatives": [p.get("name") for p in places[1:4]],
        }


class EnvironmentalAnalyst:
    """Satellite data aggregator for environmental health and imagery."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    async def _safe_call(self, service, tool, params):
        try:
            return await self.orchestrator.call_tool(service, tool, params)
        except Exception as e:
            log.error(f"Satellite Macro Error: {str(e)}")
            return {"status": "error", "message": str(e)}

    async def evaluate(self, lat: float, lon: float, analyze_vegetation: bool = True) -> Dict[str, Any]:
        log.info(f"🛰️ [Macro-Tool] EnvironmentalAnalyst: {lat},{lon}")

        offset = 0.01
        bbox = {
            "min_lon": lon - offset,
            "min_lat": lat - offset,
            "max_lon": lon + offset,
            "max_lat": lat + offset,
        }

        results = {}

        if analyze_vegetation:
            veg_res = await self._safe_call("satellite", "get_vegetation_report", bbox)
            results["vegetation"] = veg_res

        img_res = await self._safe_call("satellite", "search_satellite_imagery", bbox)
        results["imagery"] = img_res

        return {
            "status": "success",
            "location": f"{lat},{lon}",
            "analysis": results,
            "summary": "Uydu verileri üzerinden çevresel analiz tamamlandı.",
        }
