"""
core/graph.py — v7.0 (Temiz Yeniden Yazım)

LangGraph iş akışı. 3 Node:
  1. classifier  → intent belirle
  2. agent       → LLM + tool çağrısı
  3. tools       → MCP araç çalıştırma

Faz sistemi KALDIRILDI. Action card mantığı buraya taşınmadı — routes.py'de.
LLM'in tek görevi: doğru tool'u çağır, 1-2 cümle açıklama yaz.
"""
import json
import asyncio
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END
from logger import log
from prompt_manager import get_dynamic_system_prompt, classify_intent_fast
from core.models import AgentState
from core.mcp_client import orchestrator
from core.result_compressor import compress_result
from profile_manager import ProfileManager


# ─────────────────────────────────────────────────────────────────────────────
# 1. CLASSIFIER NODE
# ─────────────────────────────────────────────────────────────────────────────

async def intent_node(state: AgentState):
    """Intent sınıflandırma — sadece keyword tabanlı, hızlı."""
    # Her yeni istekte görsel veriyi sıfırla
    state["visual_data"] = {"markers": [], "polyline": None, "geojson_layers": []}
    state["route_polyline"] = None

    messages = state["messages"]
    last_msg = messages[-1].content if messages else ""

    intent = classify_intent_fast(last_msg)

    log.success(
        f"🎯 [Intent] {intent['category'].upper()} | "
        f"complexity={intent['complexity']} | urgency={intent['urgency']}"
    )
    return {"intent": intent}


# ─────────────────────────────────────────────────────────────────────────────
# 2. AGENT NODE
# ─────────────────────────────────────────────────────────────────────────────

async def agent_node(state: AgentState):
    """LLM çağrısı. Claude primary, Gemini fallback."""
    session_id = state.get("session_id", "default_session")

    # Aktif rota varsa bunu LLM'e bildir
    has_active_route = (
        orchestrator.redis_client
        and orchestrator.redis_client.exists(f"route:{session_id}")
    )

    # Trip context — AKTİF ROTA VARSA her zaman inject et (places/fuel/break/scenic
    # sorgularında bile). Kullanıcı plan_trip yaptıktan sonra "yemek öner" derse,
    # bu sorgu o rotaya bağlamlı olmalı (rota üstünde, geçilen şehirlerde).
    # Sadece eczane/general gibi konum-bağımsız sorgularda devre dışı.
    _CTX_INJECT_CATEGORIES = {"routing", "places", "fuel", "event", "city_data"}
    trip_context = None
    if orchestrator.redis_client and (
        has_active_route
        or state.get("intent", {}).get("category") in _CTX_INJECT_CATEGORIES
    ):
        raw_tc = orchestrator.redis_client.get(f"trip_ctx:{session_id}")
        if raw_tc:
            import json as _json
            try:
                trip_context = _json.loads(
                    raw_tc if isinstance(raw_tc, str) else raw_tc.decode("utf-8")
                )
            except Exception:
                pass

    # System prompt oluştur
    user_ctx = await ProfileManager.get_combined_context(session_id)
    sys_prompt = get_dynamic_system_prompt(user_ctx, state["intent"], trip_context=trip_context)
    if has_active_route:
        sys_prompt += (
            "\n\n[SİSTEM NOTU: Kullanıcının aktif bir rotası var. "
            "Rota gerektiren tool çağrılarında polyline='LATEST' kullan, "
            "sistem otomatik Redis'ten gerçek polyline'ı enjekte edecek.]"
        )

    messages = [SystemMessage(content=sys_prompt)] + state["messages"]

    # Model seçimi: routing/day_plan → Claude, diğerleri → Gemini (hız)
    intent = state.get("intent", {})
    use_claude = intent.get("complexity") == "high"

    primary_model  = orchestrator.llm_claude if use_claude else orchestrator.llm_gemini
    fallback_model = orchestrator.llm_gemini if use_claude else orchestrator.llm_claude

    # Tool-call zorunlu olduğu durumları tespit eden son-kullanıcı mesajı kontrolü
    last_user_msg = ""
    for m in reversed(state["messages"]):
        try:
            if getattr(m, "type", "") == "human":
                last_user_msg = (m.content or "").lower() if isinstance(m.content, str) else ""
                break
        except Exception:
            continue
    _ADD_STOP_KEYWORDS = (
        "ekle", "uğrayalım", "uğra", "durup", "durak ekle", "yolda dur",
        "rotaya ekle", "molamızı ekle", "molamı ekle", "şuna da uğrayalım",
    )
    is_waypoint_add = has_active_route and any(k in last_user_msg for k in _ADD_STOP_KEYWORDS)

    # X'ten Y'ye yakıt sorgusu — evaluate_route_strategy ZORUNLU
    _FUEL_ROUTE_FUEL_KWS = ("mazot", "motorin", "benzin", "yakıt", "lpg", "akaryakıt")
    _FUEL_ROUTE_INDICATORS = (
        "den ", "dan ", "ten ", "tan ",
        "konumdan", "konumumdan", "konumundan", "buradan", "giderken",
    )
    is_fuel_route = (
        any(k in last_user_msg for k in _FUEL_ROUTE_FUEL_KWS)
        and any(k in last_user_msg for k in _FUEL_ROUTE_INDICATORS)
    )

    must_call_tool = is_waypoint_add or is_fuel_route

    def _resp_has_tool_calls(resp) -> bool:
        tc = getattr(resp, "tool_calls", None)
        return bool(tc)

    last_response = None
    for attempt in range(3):
        model = primary_model if attempt == 0 else fallback_model
        try:
            log.info(f"🧠 [Agent] Model={getattr(model, 'model', '?')} deneme={attempt+1}")
            response = await model.bind_tools(orchestrator.runtime_tools).ainvoke(messages)
            last_response = response

            # Tool çağrısı zorunlu ama LLM atladıysa: 1 kez daha tetikle
            if must_call_tool and not _resp_has_tool_calls(response) and attempt == 0:
                if is_fuel_route:
                    log.warning(
                        f"⚠️ [Agent] Yakıt-rota niyeti var ama tool çağrılmamış — "
                        f"force-retry. text: {str(response.content)[:120]!r}"
                    )
                    force_msg = SystemMessage(content=(
                        "⚠️ ZORUNLU: Kullanıcı güzergah üstü EN UCUZ yakıt soruyor. "
                        "ASLA düz metin yazma — ŞİMDİ `evaluate_route_strategy` tool çağrısı yap. "
                        "Parametreler: origin=başlangıç şehri, destination=hedef şehir, fuel_type=yakıt tipi. "
                        "`get_fuel_prices` KULLANMA — o sadece tek bir nokta için çalışır, "
                        "güzergah analizi yapmaz. evaluate_route_strategy rotayı KENDİ HESAPLAR."
                    ))
                else:
                    log.warning(
                        f"⚠️ [Agent] 'ekle/durak' niyeti var ama tool çağrılmamış — "
                        f"force-retry. text preview: {str(response.content)[:120]!r}"
                    )
                    force_msg = SystemMessage(content=(
                        "⚠️ ZORUNLU: Kullanıcı rotaya durak/mola eklemek istiyor. "
                        "ASLA tool çağırmadan cevap yazma. ŞİMDİ önce "
                        "`search_hybrid_places(query='...', route_polyline='LATEST')` "
                        "ile yeni durağın koordinatını bul, sonra "
                        "`get_route_data(origin='CURRENT_LOCATION', destination='<MEVCUT_HEDEF>', "
                        "waypoints='<ESKİ_WP>|<YENİ_LAT,LON>')` ile rotayı güncelle. "
                        "Bu iki tool çağrısını YAP, sonra cevap yaz."
                    ))
                messages = [SystemMessage(content=sys_prompt), force_msg] + state["messages"]
                continue  # bir sonraki attempt'te yeniden çağır

            return {"messages": [response], "retry_count": 0}
        except Exception as exc:
            exc_str = str(exc).lower()
            is_quota = any(k in exc_str for k in ["429", "quota", "resource_exhausted", "403"])
            if is_quota or attempt < 2:
                delay = 0 if is_quota else [0.2, 0.5][attempt]
                if delay:
                    await asyncio.sleep(delay)
                log.warning(f"⚡ [Agent] Fallback: {exc_str[:60]}")
                continue
            log.error(f"🔥 [Agent] Başarısız: {exc_str[:150]}")
            raise

    # Force-retry sonrası bile tool yoksa, mevcut response'u döndür (kullanıcı en
    # azından LLM metnini görür — yine de log'da neden uyarısı var)
    if last_response is not None:
        return {"messages": [last_response], "retry_count": 0}

    raise RuntimeError("[agent_node] Tüm denemeler başarısız")


# ─────────────────────────────────────────────────────────────────────────────
# 3. TOOL NODE
# ─────────────────────────────────────────────────────────────────────────────

async def custom_tool_node(state: AgentState):
    """MCP araçlarını paralel çalıştırır, görsel veriyi yakalar."""
    last_msg  = state["messages"][-1]
    session_id = state.get("session_id", "default_session")
    route_key  = f"route:{session_id}"
    visual_data = state.get(
        "visual_data", {"markers": [], "polyline": None, "geojson_layers": []}
    )

    tool_calls = getattr(last_msg, "tool_calls", [])
    if not tool_calls:
        return {"messages": [], "retry_count": state.get("retry_count", 0)}

    async def _run_tool(tc):
        t_name = tc["name"]
        args   = dict(tc.get("args", {}))
        args["session_id"] = session_id

        # LATEST polyline substitution
        if orchestrator.redis_client:
            for k, v in list(args.items()):
                if isinstance(v, str) and v.upper() in ("LATEST", "[HARITAYA ÇİZİLDİ]"):
                    raw = orchestrator.redis_client.get(route_key)
                    if raw:
                        args[k] = raw if isinstance(raw, str) else raw.decode("utf-8")
                        log.info(f"🔄 [PolySubst] {k} → Redis polyline enjekte edildi")
                    else:
                        log.warning(f"⚠️ [PolySubst] {k}=LATEST ama Redis boş ({t_name})")

        # ★ AUTO-WAYPOINT INJECTION — LLM get_route_data çağırıyor ama
        # waypoints parametresini unutmuşsa, trip_ctx'teki mevcut waypoint
        # listesini Redis'ten çek ve enjekte et. Bu sayede "Boztepe ekle"
        # diyen kullanıcının waypoint'i kaybolmaz.
        if t_name == "get_route_data" and orchestrator.redis_client:
            _has_wp = bool((args.get("waypoints") or "").strip())
            if not _has_wp:
                try:
                    _raw_tc = orchestrator.redis_client.get(f"trip_ctx:{session_id}")
                    if _raw_tc:
                        import json as _json_g
                        _tc = _json_g.loads(
                            _raw_tc if isinstance(_raw_tc, str) else _raw_tc.decode("utf-8")
                        )
                        _wps = _tc.get("waypoints") or []
                        if _wps:
                            # Lat,lon koordinatları olanları al; şehir adlarını bırak
                            import re as _re_wp_inj
                            _coord_wps = [
                                str(w).strip() for w in _wps
                                if isinstance(w, str) and _re_wp_inj.match(
                                    r"^-?\d+\.?\d*,-?\d+\.?\d*$", w.strip()
                                )
                            ]
                            if _coord_wps:
                                args["waypoints"] = "|".join(_coord_wps)
                                log.info(
                                    f"🔧 [AutoWP] get_route_data waypoints boştu → "
                                    f"trip_ctx'ten {len(_coord_wps)} koordinat enjekte: "
                                    f"{args['waypoints'][:80]}"
                                )
                except Exception as _wpe:
                    log.warning(f"⚠️ [AutoWP] hata: {_wpe}")

        log.info(f"🛠️ [Tool] Başlatıldı: {t_name}")
        tool = orchestrator.get_tool_by_name(t_name)
        if not tool:
            return (
                ToolMessage(content=f"Hata: {t_name} bulunamadı", tool_call_id=tc["id"]),
                None,
            )

        try:
            res = await asyncio.wait_for(tool.ainvoke(args), timeout=300.0)
        except asyncio.TimeoutError:
            res = {"status": "error", "message": f"{t_name} 5 dakikada yanıt vermedi"}
            log.error(f"⏳ [Timeout] {t_name}")
        except Exception as e:
            res = {"status": "error", "message": str(e)}
            log.error(f"🔥 [Tool Error] {t_name}: {e}")

        # JSON parse
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except Exception:
                pass

        local_visual = {"markers": [], "polyline": None}

        if isinstance(res, dict):
            if res.get("status") == "error":
                return (
                    ToolMessage(
                        content=json.dumps(res, ensure_ascii=False),
                        tool_call_id=tc["id"],
                    ),
                    None,
                )

            # ── Polyline yakalama ─────────────────────────────────────────
            poly = res.get("polyline") or res.get("polyline_encoded")
            if poly and len(str(poly)) > 50:
                is_proxy = any(
                    k in str(poly).upper()
                    for k in ["LATEST", "GİZLENDİ", "HARİTAYA", "[HARITA"]
                )
                if not is_proxy:
                    local_visual["polyline"] = poly
                    if orchestrator.redis_client:
                        try:
                            orchestrator.redis_client.setex(route_key, 3600, poly)
                        except Exception:
                            pass

            # Polyline gizle (LLM'e token tasarrufu)
            for pk in ("polyline", "polyline_encoded"):
                if pk in res:
                    res[pk] = "[HARİTAYA ÇİZİLDİ]"

            # ── Marker yakalama (sadece POI araçlarından) ─────────────────
            POI_TOOLS = {
                "search_hybrid_places", "get_pharmacies", "get_events",
                "search_web_intel", "get_sports_matches", "find_ev_charging",
                "plan_weather_aware_route", "evaluate_route_strategy"
            }
            if t_name in POI_TOOLS:
                # mcp_intel get_pharmacies sadece adres + telefon döner,
                # koordinat YOK. Google Places Text Search ile lat/lon enrich et.
                if t_name == "get_pharmacies" and isinstance(res, dict):
                    pharms = res.get("data") or res.get("pharmacies") or []
                    if isinstance(pharms, list) and pharms:
                        await _geocode_pharmacies(pharms)

                # Tek POI (res seviyesinde lat/lon)
                if "lat" in res and "lon" in res:
                    local_visual["markers"].append(_build_marker(res))

                # Listeler — birden çok olası key adı (places / pharmacies / data / results)
                all_places: list = []
                for key in (
                    "strict_route_places", "relaxed_route_places",
                    "places", "pharmacies", "results", "data", "items",
                ):
                    v = res.get(key)
                    if isinstance(v, list):
                        all_places.extend(v)

                def _normalize_coords(p: dict) -> bool:
                    """lat/lon yoksa latitude/longitude veya coords alanından doldur."""
                    if "lat" in p and "lon" in p:
                        return True
                    if "latitude" in p and "longitude" in p:
                        try:
                            p["lat"] = float(p["latitude"])
                            p["lon"] = float(p["longitude"])
                            return True
                        except Exception:
                            return False
                    coords = p.get("coords") or p.get("location") or p.get("position")
                    if isinstance(coords, str) and "," in coords:
                        try:
                            cl, cln = coords.split(",", 1)
                            p["lat"] = float(cl.strip())
                            p["lon"] = float(cln.strip())
                            return True
                        except Exception:
                            return False
                    if isinstance(coords, dict):
                        lat = coords.get("lat") or coords.get("latitude")
                        lon = coords.get("lon") or coords.get("lng") or coords.get("longitude")
                        if lat is not None and lon is not None:
                            try:
                                p["lat"] = float(lat)
                                p["lon"] = float(lon)
                                return True
                            except Exception:
                                return False
                    return False

                for p in all_places:
                    if not isinstance(p, dict):
                        continue
                    if not _normalize_coords(p):
                        continue
                    m = _build_marker(p)
                    if t_name == "get_pharmacies":
                        m["type"] = "pharmacy"
                    elif _is_fuel_place(p, res):
                        m["type"] = "fuel_station"
                    local_visual["markers"].append(m)

            # Waypoint marker'ları (rota güncellendiğinde)
            if t_name == "get_route_data":
                local_visual["distance_km"] = res.get("distance_km")
                local_visual["duration_min"] = res.get("duration_min")
                if args.get("waypoints"):
                    for i, wp in enumerate(str(args["waypoints"]).split("|")):
                        wp = wp.strip()
                        if "," in wp:
                            try:
                                lat, lon = map(float, wp.split(","))
                                local_visual["markers"].append({
                                    "name": f"Durak {i+1}",
                                    "lat": lat, "lon": lon,
                                    "type": "waypoint",
                                    "snippet": "Ara Durak",
                                })
                            except Exception:
                                pass

        output = compress_result(t_name, res)
        return (
            ToolMessage(
                content=json.dumps(output, ensure_ascii=False),
                tool_call_id=tc["id"],
            ),
            local_visual,
        )

    results = await asyncio.gather(*[_run_tool(tc) for tc in tool_calls])

    msgs = []
    for msg, lv in results:
        msgs.append(msg)
        if lv:
            if lv.get("polyline"):
                visual_data["polyline"] = lv["polyline"]
            if lv.get("distance_km"):
                visual_data["distance_km"] = lv["distance_km"]
            if lv.get("duration_min"):
                visual_data["duration_min"] = lv["duration_min"]
            visual_data["markers"].extend(lv["markers"])

    return {
        "messages": msgs,
        "visual_data": visual_data,
        "retry_count": state.get("retry_count", 0) + 1,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ROUTING LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def should_continue(state: AgentState) -> str:
    """Tool çağrısı varsa → tools, yoksa → END."""
    last_msg = state["messages"][-1]
    has_tool_calls = bool(getattr(last_msg, "tool_calls", None))

    if has_tool_calls:
        if state.get("retry_count", 0) >= 4:
            log.warning("⚠️ [MaxRetry] 4 tool döngüsü aşıldı, durduruluyor.")
            return END
        return "tools"

    return END


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_marker(p: dict) -> dict:
    return {
        "name":                    p.get("name", "Mekan"),
        "title":                   p.get("name", "Mekan"),
        "lat":                     p["lat"],
        "lon":                     p["lon"],
        "type":                    p.get("type", "poi"),
        "snippet":                 p.get("address", p.get("description", "")),
        "address":                 p.get("address", ""),
        "on_route_side":           p.get("on_route_side", "unknown"),
        "opening_hours":           p.get("opening_hours", []),
        "open_now":                p.get("open_now"),
        "eta":                     p.get("eta"),
        "deviation_meters":        p.get("deviation_meters", 0),
        "distance_along_route_km": p.get("distance_along_route_km"),
        "rating":                  p.get("rating"),
        "review_count":            p.get("review_count"),
        "price_level":             p.get("price_level"),
        "phone":                   p.get("phone"),
        # evaluate_route_strategy tarafından eklenen yakıt fiyatı.
        # _build_poi_overlay m.get("fuel_price") okur → aynı isimde geçir.
        # Flutter'ın beklediği field fuel_price_info; _build_poi_overlay oraya map'ler.
        "fuel_price":              p.get("fuel_price"),
    }


def _is_fuel_place(p: dict, res: dict) -> bool:
    fuel_brands = {"benzin", "shell", "opet", "bp", "petrol", "total", "akaryak", "motorin"}
    combined = (p.get("name", "") + str(res)).lower()
    return any(b in combined for b in fuel_brands)


async def _geocode_pharmacies(pharms: list) -> None:
    """
    mcp_intel'in get_pharmacies sonucu sadece adres + telefon döner — koordinat
    YOKTUR. Google Places Text Search API ile her eczanenin lat/lon'unu doldur.
    (GOOGLE_API_KEY Geocoding API'ye yetkili değil; GOOGLE_MAPS_API_KEY Places'e
    yetkili — onu kullanıyoruz.)
    """
    import os
    try:
        import httpx
    except Exception as e:
        log.warning(f"⚠️ [Pharmacy Geocode] Bağımlılık yok: {e}")
        return

    api_key = os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    if not api_key:
        log.warning("⚠️ [Pharmacy Geocode] API key yok, atlandı")
        return

    async def _geocode_one(client: httpx.AsyncClient, ph: dict) -> None:
        if "lat" in ph and "lon" in ph and ph["lat"] and ph["lon"]:
            return
        # Eczane ismi + ilçe + il/ülke → Text Search en güvenilir
        name = (ph.get("name") or "").strip()
        district = (ph.get("district") or "").strip()
        query_parts = [name, district, "Samsun", "Türkiye"]
        # Eğer district adres içinde geçiyorsa duplicate olmasın
        addr = ph.get("address") or ""
        if addr and addr not in query_parts:
            # Daha kısa tutmak için sadece sokak/mahalle kısmını al
            query_parts.insert(1, addr.split(",")[0].strip())
        query = " ".join([p for p in query_parts if p])
        if not query.strip():
            return
        try:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": query, "key": api_key, "language": "tr"},
                timeout=8.0,
            )
            data = resp.json()
            results = data.get("results") or []
            if results:
                loc = results[0].get("geometry", {}).get("location") or {}
                lat = loc.get("lat")
                lng = loc.get("lng")
                if lat is not None and lng is not None:
                    ph["lat"] = float(lat)
                    ph["lon"] = float(lng)
                    # Adres yoksa Google'dan da çek
                    if not ph.get("address"):
                        ph["address"] = results[0].get("formatted_address", "")
            elif data.get("status") not in ("OK", "ZERO_RESULTS"):
                log.warning(
                    f"⚠️ [Pharmacy Geocode] '{name}' API hatası: "
                    f"{data.get('status')} / {data.get('error_message','')[:80]}"
                )
        except Exception as exc:
            log.warning(f"⚠️ [Pharmacy Geocode] '{name}' başarısız: {exc}")

    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            *[_geocode_one(client, ph) for ph in pharms if isinstance(ph, dict)],
            return_exceptions=True,
        )
    enriched = sum(1 for ph in pharms if isinstance(ph, dict) and "lat" in ph)
    log.info(f"📍 [Pharmacy Geocode] {enriched}/{len(pharms)} eczane geocode edildi")
