import json
import uvicorn
from fastmcp import FastMCP
from loguru import logger
from tools.models import StandardPlace, RouteResponse, WeatherResponse

# --- HANDLER IMPORTS (Hepsi Bağlı) ---
from tools.osm import search_infrastructure_osm_handler
from tools.google import search_places_google_handler
from tools.here import get_route_data_handler # <-- HİBRİT ROUTING BURADA
from tools.weather import get_weather_handler, analyze_route_weather_handler
from tools.db import save_location_handler
from tools.toll import get_toll_prices_handler 

# --- MCP SUNUCU KURULUMU ---
mcp = FastMCP(name="City Agent")

# --- 1. OSM ALTYAPI ARAMA ---
@mcp.tool()
async def search_infrastructure_osm(lat: float, lon: float, category: str) -> str:
    """
    OSM ALTYAPI ARAMA: Belirtilen konumun çevresindeki kamusal alanları bulur.
    
    Ticari olmayan; Hastane, Okul, Park, Stadyum, Havalimanı gibi yerler için bunu kullan.
    Restoran veya kafe aramak için BUNU KULLANMA.
    
    Args:
        lat (float): Merkez enlem.
        lon (float): Merkez boylam.
        category (str): 'hospital', 'park', 'stadium', 'airport', 'parking'.
    """
    try:
        logger.info(f"🛠️ [Tool: OSM] İstek: {category} @ {lat},{lon}")
        raw_data = await search_infrastructure_osm_handler(lat, lon, category)
        
        # Handler hata dönerse (List içinde dict olarak)
        if raw_data and isinstance(raw_data, list) and len(raw_data) > 0 and "error" in raw_data[0]:
            logger.warning(f"⚠️ [Tool: OSM] Hata: {raw_data[0]['error']}")
            return json.dumps({"status": "error", "message": raw_data[0]["error"]})

        # Veriyi StandardPlace modeline döküyoruz
        standard_list = []
        for item in raw_data:
            place = StandardPlace(
                name=item.get("isim"),
                lat=item.get("lat"),
                lon=item.get("lon"),
                category=category,
                source="osm"
            )
            standard_list.append(place.model_dump())

        logger.success(f"✅ [Tool: OSM] {len(standard_list)} mekan bulundu.")
        return json.dumps(standard_list, ensure_ascii=False)
        
    except Exception as e:
        logger.error(f"🔥 [Tool: OSM] Kritik Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

# --- 2. GOOGLE TİCARİ ARAMA (ROTA FİLTRELİ) ---
@mcp.tool()
async def search_places_google(query: str, lat: float = None, lon: float = None, route_polyline: str = None) -> str:
    """
    GOOGLE MEKAN ARAMA: Restoran, Benzinlik, Tamirci, Kafe gibi ticari yerleri arar.
    
    Eğer kullanıcı bir rota üzerindeyse 'route_polyline' parametresi mutlaka dolu gelmelidir.
    
    Args:
        query (str): Aranan yer (Örn: 'En yakın köfteci', 'Lastikçi').
        lat (float): Aramanın yapılacağı merkez enlem.
        lon (float): Aramanın yapılacağı merkez boylam.
        route_polyline (str, optional): Eğer bir rota varsa, rota çizgisi (encoded polyline).
    """
    try:
        logger.info(f"🛠️ [Tool: Google] İstek: '{query}' (Rota Modu: {'Aktif' if route_polyline else 'Pasif'})")
        
        raw_data = await search_places_google_handler(query, lat, lon, route_polyline)

        if "error" in raw_data:
            logger.warning(f"⚠️ [Tool: Google] Servis hatası: {raw_data['error']}")
            return json.dumps({"status": "error", "message": raw_data["error"]})

        # Strict (Yol üstü) ve Relaxed (Sapma) listelerini birleştir
        strict_places = raw_data.get("strict_route_places", [])
        relaxed_places = raw_data.get("relaxed_route_places", [])
        all_places = strict_places + relaxed_places
        
        standard_list = []
        for item in all_places:
            # Koordinatları güvenli parse et
            try:
                if "coords" in item and "," in item["coords"]:
                    lat_str, lon_str = item["coords"].split(",")
                    p_lat, p_lon = float(lat_str), float(lon_str)
                else:
                    p_lat, p_lon = 0.0, 0.0
            except Exception:
                p_lat, p_lon = 0.0, 0.0

            place = StandardPlace(
                name=item.get("name"),
                address=item.get("address"),
                lat=p_lat,
                lon=p_lon,
                rating=item.get("rating"),
                is_open=str(item.get("open_now")), 
                source="google",
                # Ekstra metadata (LLM için faydalı)
                metadata={
                    "durum": item.get("konum_durumu", "Bilinmiyor"),
                    "sapma": item.get("sapma_mesafesi", "0m")
                }
            )
            standard_list.append(place.model_dump())

        logger.success(f"✅ [Tool: Google] {len(standard_list)} mekan işlendi.")
        return json.dumps(standard_list, ensure_ascii=False)

    except Exception as e:
        logger.error(f"🔥 [Tool: Google] Kritik Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

# --- 3. AKILLI ROTA HESAPLAMA (HİBRİT) ---
@mcp.tool()
async def get_route_data(origin: str, destination: str) -> str:
    """
    AKILLI ROTA MOTORU: İki nokta arasındaki trafik durumunu, süreyi ve mesafeyi hesaplar.
    
    Bu araç, hem şehir içi (İstanbul İBB verisi) hem de şehirler arası (HERE Maps) 
    rota hesaplamaları için TEK YETKİLİ araçtır.
    
    Args:
        origin (str): Başlangıç noktası (Örn: 'Rize', 'Kadikoy evlendirme dairesi').
        destination (str): Varış noktası (Örn: 'Trabzon', 'Taksim meydani').
    """
    try:
        logger.info(f"🛠️ [Tool: Rota] Hesapla: {origin} -> {destination}")
        
        # Hibrit Handler'ı çağır
        raw_data = await get_route_data_handler(origin, destination)

        if "error" in raw_data:
            logger.error(f"❌ [Tool: Rota] Başarısız: {raw_data['error']}")
            return json.dumps({"status": "error", "message": raw_data["error"]})

        # Pydantic Response Modelini doldur
        # DÜZELTME BURADA YAPILDI 👇
        poly_data = raw_data.get("polyline_encoded")
        final_polyline = poly_data if poly_data else "LOCAL_ROUTE"

        response = RouteResponse(
            distance_km=raw_data.get("mesafe_km", 0),
            duration_min=raw_data.get("sure_dk", 0),
            polyline=final_polyline, 
            summary=f"{raw_data.get('mesafe_km')} km, {raw_data.get('sure_dk')} dakika ({raw_data.get('source', 'Bilinmiyor')})",
            checkpoints=raw_data.get("analiz_noktalari", {}),
            extras={
                "geometry": raw_data.get("geometry"),
                "source_system": raw_data.get("source")
            }
        )
        
        logger.success(f"✅ [Tool: Rota] Rota Hazır: {response.summary}")
        return response.model_dump_json()

    except Exception as e:
        logger.error(f"🔥 [Tool: Rota] Kritik Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

# --- 4. HAVA DURUMU ---
@mcp.tool()
async def get_weather(lat: float, lon: float) -> str:
    """
    Belirtilen koordinat için anlık hava durumunu verir.
    
    Args:
        lat (float): Enlem.
        lon (float): Boylam.
    """
    try:
        logger.info(f"🛠️ [Tool: Hava] Sorgu: {lat},{lon}")
        raw_data = await get_weather_handler(lat, lon)

        if "error" in raw_data:
            return json.dumps({"status": "error", "message": raw_data["error"]})

        current = raw_data.get("ANLIK_DURUM", {})
        
        response = WeatherResponse(
            location=raw_data.get("lokasyon_koordinat", ""),
            current_temp=current.get("sicaklik", ""),
            feels_like=current.get("hissedilen", ""),
            condition=current.get("durum", ""),
            forecast_hourly=raw_data.get("ONUMUZDEKI_SAATLER", []),
            warning=raw_data.get("uyari")
        )

        return response.model_dump_json()
    except Exception as e:
        logger.error(f"🔥 [Tool: Hava] Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

# --- 5. ROTA HAVA DURUMU ANALİZİ ---
@mcp.tool()
async def analyze_route_weather(polyline: str) -> str:
    """
    WEATHER SHIELD: Uzun yolculuklarda rota üzerindeki hava durumu risklerini analiz eder.
    
    Kullanıcı 'yolculukta yağmur var mı?', 'yolda hava nasıl?' diye sorarsa bunu kullan.
    
    Args:
        polyline (str): Rota verisi (Encoded Polyline string).
    """
    try:
        logger.info("🛠️ [Tool: Rota Hava] Analiz başlatılıyor...")
        # Not: Yerel rotalarda polyline yerine GeoJSON kullanılması gerekebilir.
        # Handler içinde bu dönüşüm yapılacak.
        result = await analyze_route_weather_handler(polyline)
        
        logger.success("✅ [Tool: Rota Hava] Analiz tamamlandı.")
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        logger.error(f"🔥 [Tool: Rota Hava] Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

# --- 6. KONUM KAYDETME (DB) ---
@mcp.tool()
async def save_location(name: str, lat: float, lon: float, category: str = "Genel", note: str = "") -> str:
    """
    Kullanıcının bir konumu veritabanına kaydetmesini sağlar.
    
    Args:
        name (str): Konumun adı (Örn: 'Mehmetin evi').
        lat (float): Enlem.
        lon (float): Boylam.
        category (str): Kategori (ev, is, favori).
        note (str): Kullanıcı notu.
    """
    try:
        logger.info(f"💾 [Tool: DB] Kayıt: {name}")
        result = await save_location_handler(name, lat, lon, category, note)
        return json.dumps({"status": "success", "message": result}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"🔥 [Tool: DB] Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

# --- 7. OTOYOL ÜCRETLERİ ---
@mcp.tool()
async def get_toll_prices(filter_region: str = None) -> str:
    """
    Köprü, tünel ve otoyol geçiş ücretlerini listeler.
    
    Args:
        filter_region (str): Filtrelemek için şehir adı (Örn: 'İstanbul'). Hepsi için boş bırak.
    """
    try:
        logger.info("🛠️ [Tool: Otoyol] Fiyatlar çekiliyor...")
        text_result = await get_toll_prices_handler(filter_region)
        return json.dumps({"status": "success", "text": text_result}, ensure_ascii=False)
    except Exception as e:
        logger.error(f"🔥 [Tool: Otoyol] Hata: {e}")
        return json.dumps({"status": "error", "message": str(e)})

if __name__ == "__main__":
    logger.info("🚀 City Agent (MCP) Başlatılıyor... [Port: 8000]")
    # Docker içinde host 0.0.0.0 olmalı ki dışarıdan erişilebilsin
    mcp.run(transport="sse", host="0.0.0.0", port=8000)