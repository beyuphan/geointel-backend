import json
import asyncio
from tools.geometry import calculate_distance_meters
import uvicorn
from fastmcp import FastMCP
from loguru import logger
from typing import List, Union, Optional

# --- MODELLER VE HANDLERLAR ---
from tools.models import StandardPlace, RouteResponse, WeatherResponse, ErrorResponse
from tools.osm import search_infrastructure_osm_handler
from tools.google import search_places_google_handler
from tools.here import get_route_data_handler
from tools.weather import get_weather_handler, analyze_route_weather_handler
from tools.db import save_location_handler, search_spatial_rag, save_poi_with_embedding
from tools.toll import get_toll_prices_handler, get_toll_for_route_handler
from tools.wfs import fetch_wfs_as_geojson, fetch_ibb_dataset_geojson, list_wfs_datasets
from tools.radar import get_radars_on_route_handler
from tools.cache import redis_store
from tools.route_summary import build_route_summary_handler
from tools.ev_charging import get_ev_charging_handler
from tools.traffic import get_route_traffic_handler
from safe_tools import safe_tool

# --- MCP SUNUCU KURULUMU ---
# v2.0: Veri bütünlüğü ve koordinat güvenliği odaklı mimari
mcp = FastMCP(name="City Agent", version="2.0.0")

def handle_critical_error(e: Exception, tool_name: str) -> str:
    """Hataları merkezi bir formatta loglar ve döner."""
    logger.error(f"🔥 [Tool: {tool_name}] Kritik Hata: {str(e)}")
    return ErrorResponse(message=f"{tool_name} işleminde hata oluştu: {str(e)}").model_dump_json()

# --- 1. OSM ALTYAPI ARAMA ---
@mcp.tool()
@safe_tool(fallback_message="OSM araması şu an yapılamıyor.")
async def search_infrastructure_osm(lat: float, lon: float, category: str, radius: int = 2000) -> str:
    """
    OSM ALTYAPI ARAMA: Belirtilen konumun çevresindeki kamusal (ticari olmayan) alanları bulur.
    Hastane, Okul, Park, Stadyum, Havalimanı gibi yerler için bunu kullan.
    
    Args:
        lat (float): Merkez enlem (-90, 90).
        lon (float): Merkez boylam (-180, 180).
        category (str): Aramak istediğin OSM etiketi (Örn: hospital, park, airport).
        radius (int): Arama yarıçapı (metre).
    """
    try:
        logger.info(f"🛠️ [Tool: OSM] İstek: {category} @ {lat},{lon}")
        raw_data = await search_infrastructure_osm_handler(lat, lon, category, radius)
        
        # Handler hata dönerse (List içinde dict olarak gelme durumu)
        if raw_data and isinstance(raw_data, list) and len(raw_data) > 0 and "error" in raw_data[0]:
            logger.warning(f"⚠️ [Tool: OSM] Handler Hatası: {raw_data[0]['error']}")
            return ErrorResponse(message=raw_data[0]["error"]).model_dump_json()

        # Veriyi StandardPlace modeline dökerek standardize ediyoruz
        standard_list = []
        for item in raw_data:
            # Eksik koordinat veya isim kontrolü
            if not item.get("lat") or not item.get("lon") or not item.get("isim"):
                continue
                
            place = StandardPlace(
                name=item.get("isim"),
                lat=item.get("lat"),
                lon=item.get("lon"),
                category=category,
                source="osm"
            )
            standard_list.append(place.model_dump())

        logger.success(f"✅ [Tool: OSM] {len(standard_list)} mekan doğrulandı ve dönüldü.")
        return json.dumps(standard_list, ensure_ascii=False)
        
    except Exception as e:
        return handle_critical_error(e, "search_infrastructure_osm")

# --- 2. GOOGLE PLACES ROTA/KONUM ARAMA ---
@mcp.tool()
@safe_tool(fallback_message="Google Maps araması şu an yapılamıyor.")
async def search_places_google(query: str, lat: float = 0.0, lon: float = 0.0, route_polyline: str = None) -> str:
    """
    ⚠️ DEPRECATED — BU ARACI DOĞRUDAN ÇAĞIRMA! Bunun yerine 'search_hybrid_places' kullan.
    Bu araç sadece dahili sistemler (macro-tools) tarafından kullanılır.
    """
    try:
        logger.info(f"🛠️ [Tool: Google] Sorgu: '{query}' (Rota: {'Aktif' if route_polyline else 'Pasif'})")
        raw_data = await search_places_google_handler(query, lat, lon, route_polyline)

        if "error" in raw_data:
            return ErrorResponse(message=raw_data["error"]).model_dump_json()

        # Yol üstü ve sapma listelerini birleştirip Pydantic ile doğruluyoruz
        all_places = raw_data.get("strict_route_places", []) + raw_data.get("relaxed_route_places", [])
        
        standard_list = []
        for item in all_places:
            try:
                # Koordinat parse & Null Island (0,0) koruması
                if "coords" in item and "," in item["coords"]:
                    coords_split = item["coords"].split(",")
                    p_lat, p_lon = float(coords_split[0]), float(coords_split[1])
                    if abs(p_lat) < 0.0001 and abs(p_lon) < 0.0001: continue
                else: continue

                place = StandardPlace(
                    name=item.get("name"),
                    address=item.get("address"),
                    lat=p_lat,
                    lon=p_lon,
                    rating=item.get("rating", 0.0),
                    review_count=item.get("review_count", 0),
                    is_open=str(item.get("open_now", "Bilinmiyor")),
                    source="google",
                    metadata={
                        "durum": item.get("konum_durumu", "Bilinmiyor"),
                        "deviation_meters": item.get("deviation_meters", 0)
                    }
                )
                standard_list.append(place.model_dump())
            except Exception as parse_error:
                logger.warning(f"⚠️ [Google] Mekan parse edilemedi: {parse_error}")
                continue

        logger.success(f"✅ [Tool: Google] {len(standard_list)} mekan işlendi.")
        return json.dumps(standard_list, ensure_ascii=False)

    except Exception as e:
        return handle_critical_error(e, "search_places_google")

# --- 3. HERE ROTA OLUŞTURMA ---
@mcp.tool()
@safe_tool(fallback_message="HERE Maps rota oluşturamadı.")
async def get_route_data(origin: str, destination: str) -> str:
    """
    AKILLI ROTA MOTORU: İki nokta arasındaki trafik durumunu, süreyi ve mesafeyi hesaplar.
    İstanbul içi için İBB Canlı Verisi, şehirler arası için HERE Maps kullanır.
    """
    try:
        logger.info(f"🛠️ [Tool: Rota] Hesapla: {origin} -> {destination}")
        raw_data = await get_route_data_handler(origin, destination)

        if "error" in raw_data:
            return ErrorResponse(message=raw_data["error"]).model_dump_json()

        # Rota polyline verisini belirle (Local veya HERE)
        poly_data = raw_data.get("polyline_encoded")
        final_polyline = poly_data if poly_data else "LOCAL_ROUTE"

        # Alternatif rota verilerini aktar (HERE API alternatifler döndürürse)
        alternatives = raw_data.get("alternatif_rotalar", [])

        response = RouteResponse(
            distance_km=raw_data.get("mesafe_km", 0),
            duration_min=raw_data.get("sure_dk", 0),
            polyline=final_polyline, 
            summary=f"{raw_data.get('mesafe_km')} km, {raw_data.get('sure_dk')} dakika",
            checkpoints=raw_data.get("analiz_noktalari", {}),
            source_system=raw_data.get("source", "Bilinmiyor"),
            alternatives=alternatives
        )
        
        logger.success(f"✅ [Tool: Rota] {response.source_system} üzerinden rota hazır. {len(alternatives)} alternatif.")
        return response.model_dump_json()

    except Exception as e:
        return handle_critical_error(e, "get_route_data")

# --- 4. HAVA DURUMU (KONUM BAZLI) ---
@mcp.tool()
@safe_tool(fallback_message="Anlık hava durumu çekilemiyor.")
async def get_weather(lat: float, lon: float) -> str:
    """Belirtilen koordinat için anlık ve saatlik hava durumu raporu sunar."""
    try:
        logger.info(f"🛠️ [Tool: Hava] Sorgu: {lat},{lon}")
        raw_data = await get_weather_handler(lat, lon)

        if "error" in raw_data:
            return ErrorResponse(message=raw_data["error"]).model_dump_json()

        current = raw_data.get("ANLIK_DURUM", {})
        
        response = WeatherResponse(
            location=raw_data.get("lokasyon_koordinat", f"{lat},{lon}"),
            current_temp=current.get("sicaklik", "N/A"),
            feels_like=current.get("hissedilen", "N/A"),
            condition=current.get("durum", "N/A"),
            forecast_hourly=raw_data.get("ONUMUZDEKI_SAATLER", []),
            warning=raw_data.get("uyari")
        )

        return response.model_dump_json()
    except Exception as e:
        return handle_critical_error(e, "get_weather")

# --- 5. ROTA HAVA DURUMU (WHEATHER SHIELD) ---
@mcp.tool()
@safe_tool(fallback_message="Rota kalkanı (hava durumu analizi) yapılamadı.")
async def analyze_route_weather(polyline: str) -> str:
    """
    WEATHER SHIELD: Rota boyunca (40km'lik dilimlerle) hava durumu risklerini (yağmur, kar, buzlanma) analiz eder.
    """
    try:
        logger.info("🛠️ [Tool: Weather Shield] Analiz başlatılıyor...")
        result = await analyze_route_weather_handler(polyline)
        
        if isinstance(result, dict) and "error" in result:
            return ErrorResponse(message=result["error"]).model_dump_json()

        logger.success("✅ [Tool: Weather Shield] Analiz tamamlandı.")
        
        # TOKEN OPTIMIZATION: LLM'e sadece özeti gönder, tüm koordinat dizisi gereksiz bağlam yaratır.
        summary_result = {
            "tarama_noktasi_sayisi": result.get("tarama_noktasi_sayisi", 0),
            "risk_durumu": result.get("risk_durumu", "BİLİNMİYOR"),
            "riskli_bolgeler": result.get("riskli_bolgeler", []),
            "tavsiye": result.get("tavsiye", "")
        }
        return json.dumps(summary_result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "analyze_route_weather")

# --- 6. KONUM KAYDETME (POSTGIS) ---
@mcp.tool()
async def save_location(name: str, lat: float, lon: float, category: str = "Genel", note: str = "") -> str:
    """Kullanıcının belirttiği konumu (favori, iş, ev vb.) veritabanına kalıcı olarak kaydeder."""
    try:
        logger.info(f"💾 [Tool: DB] Kayıt Deneniyor: {name}")
        result = await save_location_handler(name, lat, lon, category, note)
        return json.dumps({"status": "success", "message": result}, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "save_location")

# --- 7. OTOYOL VE KÖPRÜ ÜCRETLERİ ---
@mcp.tool()
async def get_toll_prices(filter_region: str = None) -> str:
    """Türkiye genelindeki güncel otoyol, köprü ve tünel geçiş ücretlerini listeler."""
    try:
        logger.info("🛠️ [Tool: Otoyol] Fiyat listesi çekiliyor...")
        text_result = await get_toll_prices_handler(filter_region)
        return json.dumps({"status": "success", "text": text_result}, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_toll_prices")

# --- 8. SPATIAL RAG (Hibrit Arama) ---
@mcp.tool()
async def search_hybrid_poi(query: str, lat: float, lon: float, radius: float = 5000, limit: int = 5) -> str:
    """
    SPATIAL RAG ARAMA: Kullanıcının metin sorgusuna anlamsal (semantic) olarak en çok 
    benzeyen yerleri (POI) bulur ve bunları belirtilen koordinata olan uzaklıklarına göre (spatial) daraltır.
    """
    try:
        logger.info(f"🛠️ [Tool: SpatialRAG] Sorgu: '{query}' @ {lat},{lon}")
        results = await search_spatial_rag(query, lat, lon, radius, limit)
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "search_hybrid_poi")

@mcp.tool()
async def save_poi_to_db(name: str, description: str, lat: float, lon: float, category: str = "Tavsiye") -> str:
    """
    Kullanıcının önerdiği veya beğendiği bir konumu açıklamasıyla birlikte vektörel (embedding)
    olarak veritabanına kaydeder. RAG aramalarında bu veriler listelenecektir.
    """
    try:
        logger.info(f"💾 [Tool: SpatialRAG Kayıt] {name}")
        result = await save_poi_with_embedding(name, description, lat, lon, category)
        return json.dumps({"status": "success", "message": result}, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "save_poi_to_db")

@mcp.tool()
async def fetch_wfs_layer(
    base_url: str,
    type_name: str,
    bbox: Optional[str] = None,
    src_epsg: Optional[int] = 5254,
    max_features: Optional[int] = 200,
) -> str:
    """
    WFS katmanı çeker ve GeoJSON FeatureCollection döndürür.

    Args:
        base_url: WFS endpoint (örn: 'https://.../ows' veya '?service=WFS' almayan kök URL)
        type_name: Katman adı (WFS typeNames) (örn: 'ibb:afettoplanma')
        bbox: 'minx,miny,maxx,maxy' formatında bbox. Varsayılan CRS: EPSG:5254.
        src_epsg: Kaynak koordinat sistemi (rapordaki senaryo için genelde 5254).
        max_features: Maksimum feature sayısı.
    """
    try:
        bbox_tuple = None
        if bbox:
            parts = [p.strip() for p in bbox.split(",")]
            if len(parts) >= 4:
                bbox_tuple = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))

        data = await fetch_wfs_as_geojson(
            base_url=base_url,
            type_name=type_name,
            bbox=bbox_tuple,
            src_epsg=src_epsg,
            dst_epsg=4326,
            max_features=max_features,
        )

        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "fetch_wfs_layer")

@mcp.tool()
async def fetch_ibb_dataset(
    dataset_id: str,
    bbox: Optional[str] = None,
    max_features: Optional[int] = 200,
) -> str:
    """
    İBB WFS preset dataset'lerinden GeoJSON çeker ve standardize edilmiş FeatureCollection döndürür.

    Not: Endpoint env ile yönetilir: IBB_WFS_BASE_URL
    """
    try:
        bbox_tuple = None
        if bbox:
            parts = [p.strip() for p in bbox.split(",")]
            if len(parts) >= 4:
                bbox_tuple = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))

        data = await fetch_ibb_dataset_geojson(
            dataset_id=dataset_id,
            bbox=bbox_tuple,
            max_features=max_features,
        )
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "fetch_ibb_dataset")

@mcp.tool()
async def list_ibb_datasets() -> str:
    """İBB WFS preset dataset listesini döndürür."""
    try:
        return json.dumps(list_wfs_datasets(), ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "list_ibb_datasets")


from tools.here import get_route_data_handler, _resolve_coordinates


# --- 11. ROTA RADARLARI (GERÇEK ZAMANLI - HERE MAPS) ---
@mcp.tool()
async def get_route_radars(route_polyline: str) -> str:
    """
    GERÇEK ZAMANLI RADAR TARAMA (HERE Maps Traffic API): Rota boyunca aktif hız kameraları,
    radar noktaları ve trafik tehlikelerini HERE Maps'ten canlı olarak çeker.
    Rota hesaplandıktan sonra her zaman çağır. route_polyline için 'LATEST' kullanabilirsin.
    """
    try:
        logger.info("🚨 [Tool: Radar] Rota üzerinde kamera taraması yapılıyor...")
        result = await get_radars_on_route_handler(route_polyline)
        if "error" in result:
            return ErrorResponse(message=result["error"]).model_dump_json()
        logger.success(f"✅ [Tool: Radar] {result['total_count']} kamera bulundu.")
        
        # TOKEN OPTIMIZATION: LLM'e tüm radar koordinatlarını dönme. 
        # Sadece sayı durumu ve tehlike bilgisini gönder. 
        summary_result = {
            "total_count": result.get("total_count", 0),
            "summary": result.get("summary", ""),
            "warning": result.get("warning")
        }
        return json.dumps(summary_result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_route_radars")


# --- 12. ROTA GEÇİŞ ÜCRETLERİ (GERÇEK ZAMANLI - HERE MAPS) ---
@mcp.tool()
@safe_tool(fallback_message="Geçiş ücretleri sorgulanamadı.")
async def get_toll_for_route(route_polyline: str) -> str:
    """
    GERÇEK ZAMANLI GEÇİŞ ÜCRETİ (HERE Maps Routing API): Rota üzerindeki köprü, tünel ve
    ücretli otoyolların HGS/OGS maliyetini HERE Maps'ten canlı olarak hesaplar.
    Rota hesaplandıktan sonra her zaman çağır. route_polyline için 'LATEST' kullanabilirsin.
    """
    try:
        logger.info("💰 [Tool: Toll] Rota ücreti hesaplanıyor...")
        result = await get_toll_for_route_handler(route_polyline)
        if "error" in result:
            return ErrorResponse(message=result["error"]).model_dump_json()
        logger.success(f"✅ [Tool: Toll] {result['toll_count']} geçiş tespit edildi. Toplam: {result['total_toll_cost_tl']} TL")
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_toll_for_route")

@mcp.tool()
@safe_tool(fallback_message="Hibrit mekan arama başarısız oldu.")
async def search_hybrid_places(query: str, location_name: Optional[str] = None, lat: Optional[float] = None, lon: Optional[float] = None, category: Optional[str] = "commercial", route_polyline: Optional[str] = None) -> str:
    """
    HİBRİT MEKAN ARAMA (Google + OSM Füzyonu): Kullanıcının aradığı mekanları bulur ve fiziksel doğruluğunu artırır.
    Eğer elinde kesin 'lat' ve 'lon' yoksa, bunları BOŞ BIRAK ve sadece 'location_name' (Örn: 'Kadıköy', 'Rize') ile 'query' değerlerini ver. 
    Lütfen koordinatları TAHMİN ETME!
    Eğer kullanıcı bir rota üzerindeyse route_polyline parametresini geç veya LATEST kullan; bu sayede ETA bazlı mekan filtreleme aktif olur.
    """
    logger.info(f"🧬 [Tool: Hybrid Fusion] Başlatıldı: '{query}' @ {location_name} | {lat},{lon} | Rota: {'Aktif' if route_polyline else 'Pasif'}")
    
    try:
        # 1. Koordinat Çözümleme (LLM halüsinasyonunu engeller)
        if not lat or not lon:
            if location_name:
                resolved = await _resolve_coordinates(location_name)
                if resolved:
                    lat, lon = map(float, resolved.split(","))
                else:
                    return json.dumps({"status": "error", "message": f"{location_name} için koordinat bulunamadı."})
            elif not route_polyline:
                 return json.dumps({"status": "error", "message": "Konum adı (location_name), koordinatlar (lat, lon) veya route_polyline parametrelerinden en az biri gerekli."})

        # 2. Google'dan Ticari Veriyi Çek (route_polyline geçiliyorsa ETA hesabı da aktif)
        # lat ve lon None olsa bile route_polyline varsa Google Places yine de sonuç bulabilir.
        google_raw = await search_places_google_handler(query, lat or 0.0, lon or 0.0, route_polyline)
        if "error" in google_raw:
            return json.dumps({"status": "error", "message": google_raw["error"]})
            
        google_places = google_raw.get("strict_route_places", []) + google_raw.get("relaxed_route_places", [])
        
        # Eğer kesin koordinat yoksa OSM fusion yapılamaz (Bounding box merkezsiz çizilemez), doğrudan dön!
        if not lat or not lon:
            logger.info("ℹ️ Kesin koordinat olmadığı için OSM füzyonu atlanıyor, Google Rota araması sonuçları dönülecek.")
            fused_results = []
            for g_place in google_places:
                try:
                    g_lat, g_lon = map(float, g_place.get("coords", "0,0").split(","))
                except:
                    continue
                fused_results.append({
                    "name": g_place.get("name"),
                    "address": g_place.get("address"),
                    "rating": g_place.get("rating"),
                    "is_open": str(g_place.get("open_now")),
                    "lat": g_lat,
                    "lon": g_lon,
                    "fusion_status": "Google Route-Only Match"
                })
            
            return json.dumps({
                "places": fused_results,
                "count": len(fused_results),
                "data_source": "Google Places API (Route Match)"
            }, ensure_ascii=False)

        # Kesin koordinat varsa normal OSM füzyonu devam eder...
        # 3. OSM'den Fiziksel (Bina/Altyapı) Verisini Çek
        osm_raw = await search_infrastructure_osm_handler(lat, lon, category)
        osm_places = osm_raw if isinstance(osm_raw, list) and len(osm_raw) > 0 and "error" not in osm_raw[0] else []

        fused_results = []
        
        # 4. Çakıştırma (Füzyon) Algoritması
        for g_place in google_places:
            try:
                g_lat, g_lon = map(float, g_place.get("coords", "0,0").split(","))
            except:
                continue

            fused_place = {
                "name": g_place.get("name"),
                "address": g_place.get("address"),
                "rating": g_place.get("rating"),
                "is_open": str(g_place.get("open_now")),
                "lat": g_lat, # Varsayılan Google geometrisi
                "lon": g_lon,
                "fusion_status": "Google Only" # Başlangıç durumu
            }

            # OSM ile 20 Metre (Sapma) Kontrolü
            for o_place in osm_places:
                o_lat, o_lon = float(o_place.get("lat", 0)), float(o_place.get("lon", 0))
                distance = calculate_distance_meters(g_lat, g_lon, o_lat, o_lon)
                
                if distance <= 20.0:
                    # EŞLEŞME BULUNDU! 
                    # Geometriyi OSM'in kesin fiziksel konumuna (bina ayak izine) kaydır
                    fused_place["lat"] = o_lat
                    fused_place["lon"] = o_lon
                    fused_place["fusion_status"] = "Fused (Google+OSM)"
                    fused_place["osm_distance_shift"] = f"{distance:.1f}m"
                    break # Bu mekan için eşleşme tamam, diğer OSM noktalarına bakmaya gerek yok
            
            fused_results.append(fused_place)

        logger.success(f"✅ [Tool: Hybrid Fusion] {len(fused_results)} nesne oluşturuldu.")
        return json.dumps(fused_results, ensure_ascii=False)

    except Exception as e:
        logger.error(f"🔥 [Tool: Hybrid Fusion] Kritik Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})


# --- 13. POI GERİ BİLDİRİM (KARA LİSTE) ---
@mcp.tool()
async def report_poi_feedback(place_name: str, reason: str, session_id: str = "default") -> str:
    """
    KULLANICI GERİ BİLDİRİMİ: Kullanıcı bir mekanı “kapalıydı” veya “beğenmedim” dediğinde çağır.
    Bu mekanı oturum kara listesine ekler, bir daha önerilmez.
    Kullanıcı herhangi bir mekan veya öneri hakkında olumsuz geıbildirim verdiğinde kullan.
    """
    try:
        blacklist_key = f"poi_blacklist:{session_id}"
        entry = json.dumps({"name": place_name, "reason": reason}, ensure_ascii=False)
        redis_store.client.rpush(blacklist_key, entry)
        redis_store.client.expire(blacklist_key, 86400 * 7)  # 7 gün
        logger.info(f"🚫 [Feedback] '{place_name}' kara listeye eklendi. Neden: {reason}")
        return json.dumps({
            "status": "ok",
            "message": f"'​{place_name}' kara listeye eklendi. Bir daha önerilmeyecek.",
            "session_id": session_id
        }, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "report_poi_feedback")


@mcp.tool()
async def get_poi_blacklist(session_id: str = "default") -> str:
    """
    Oturuma ait POI kara listesini getirir. Mekan önerisi yapmadan önce bu listeyi kontrol et.
    Kara listedeki mekanlar hiçbir koşulda tekrar önerilmemeli.
    """
    try:
        blacklist_key = f"poi_blacklist:{session_id}"
        items = redis_store.client.lrange(blacklist_key, 0, -1)
        blacklist = [json.loads(item) for item in items] if items else []
        return json.dumps({
            "blacklisted_places": blacklist,
            "count": len(blacklist)
        }, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_poi_blacklist")


# --- 14. ROTA ÖZET KARTI ---
@mcp.tool()
async def build_route_summary(
    route_data: str,
    radar_data: str = "",
    toll_data: str = "",
    weather_data: str = ""
) -> str:
    """
    ROTA ÖZET KARTI: Rota hesaplandıktan sonra mesafe, süre, radar sayısı,
    geçiş ücreti ve hava durumu bilgilerini birleştirip Markdown özet kart oluşturur.
    Rota araçları tamamlandıktan sonra mutlaka bu aracı çağır.
    """
    try:
        import json as _json
        rd = _json.loads(route_data) if route_data else {}
        rr = _json.loads(radar_data) if radar_data else None
        td = _json.loads(toll_data) if toll_data else None
        wd = _json.loads(weather_data) if weather_data else None
        result = build_route_summary_handler(rd, rr, td, wd)
        return _json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "build_route_summary")


# --- 15. EV ŞARJ İSTASYONLARI ---
@mcp.tool()
async def get_ev_charging_stations(route_polyline: str) -> str:
    """
    EV ŞARJ İSTASYONLARI: Rota boyunca elektrikli araç şarj noktalarını bulur (OSM).
    Kullanıcının aracı elektrikli ise veya şarj noktası soran kullanıcıya uygulanmalıdır.
    'LATEST' kullanılabilir.
    """
    try:
        result = await get_ev_charging_handler(route_polyline)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_ev_charging_stations")


# --- 16. CANLI TRAFİK DURUMU ---
@mcp.tool()
async def get_route_traffic(route_polyline: str) -> str:
    """
    CANLI TRAFİK: HERE Traffic Flow API'dan rota üzerindeki gerçek zamanlı trafik
    yoğunluğunu çeker. Sıkışıklık seviyesi, hız ve gecikme bilgisi döner.
    Rota hesaplandıktan sonra 'LATEST' ile otomatik çağrılabilir.
    """
    try:
        result = await get_route_traffic_handler(route_polyline)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "get_route_traffic")


# --- 17. ARAÇ PROFİLİ GÜNCELLEME ---
@mcp.tool()
async def update_vehicle_profile(
    brand: str,
    model: str,
    year: int,
    city_consumption: float,
    highway_consumption: float,
    fuel_type: str = "gasoline",
    username: str = "test_pilot"
) -> str:
    """
    ARAÇ PROFİLİ GÜNCELLEME: Kullanıcının araç bilgilerini (marka, model, yıl, tüketim) veritabanında günceller.
    Kullanıcı 'Aracımı şu marka yap' veya 'Tüketimi güncelle' dediğinde bunu kullan.
    """
    try:
        from orchestrator.profile_manager import ProfileManager
        result = await ProfileManager.update_vehicle_profile(
            brand, model, year, city_consumption, highway_consumption, fuel_type, username
        )
        return json.dumps({"status": "success", "message": result}, ensure_ascii=False)
    except Exception as e:
        return handle_critical_error(e, "update_vehicle_profile")


if __name__ == "__main__":
    logger.info("🚀 City Agent v2.0 (MCP) Port 8000 üzerinde başlatılıyor...")
    mcp.run(transport="sse", host="0.0.0.0", port=8000)