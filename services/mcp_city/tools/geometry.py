# services/mcp_city/tools/geometry.py
import flexpolyline
from shapely.geometry import Point, LineString
from logger import log
from shapely.ops import transform
import pyproj

# WGS84 (GPS) -> Web Mercator (Metre) Dönüşümcüleri
project_to_meters = pyproj.Transformer.from_proj(
    pyproj.Proj('epsg:4326'), # GPS
    pyproj.Proj('epsg:3857'), # Metre
    always_xy=True
).transform

project_to_gps = pyproj.Transformer.from_proj(
    pyproj.Proj('epsg:3857'), # Metre
    pyproj.Proj('epsg:4326'), # GPS
    always_xy=True
).transform

def sample_route_points(encoded_polyline: str, interval_km: int = 40) -> list:
    """
    Rotayı analiz eder ve her 'interval_km' mesafede bir koordinat örnekler.
    Örn: 400km yol için ~10 nokta döndürür.
    """
    if not encoded_polyline or encoded_polyline == "LATEST":
        return []

    try:
        # 1. Polyline çöz (Decode)
        coords = flexpolyline.decode(encoded_polyline) # [(lat, lon), ...]
        # Shapely (lon, lat) ister, flexpolyline (lat, lon) verir. Ters çevir:
        line_coords = [(lon, lat) for lat, lon in coords]
        
        if not line_coords: return []
        
        # 2. Geometriyi oluştur ve Metreye çevir
        route_line = LineString(line_coords)
        route_line_m = transform(project_to_meters, route_line)
        
        total_length_m = route_line_m.length
        interval_m = interval_km * 1000
        
        # 3. Örnekleme (Sampling)
        sampled_points = []
        current_dist = 0
        
        while current_dist <= total_length_m:
            # Noktayı bul (Metre uzayında)
            point_m = route_line_m.interpolate(current_dist)
            # GPS'e geri çevir
            point_gps = transform(project_to_gps, point_m)
            
            sampled_points.append({
                "lat": point_gps.y,
                "lon": point_gps.x,
                "km_point": int(current_dist / 1000) # Kaçıncı km?
            })
            current_dist += interval_m
            
        log.info(f"📏 [GEO] Rota {int(total_length_m/1000)} km, {len(sampled_points)} noktaya bölündü.")
        return sampled_points

    except Exception as e:
        log.error(f"❌ Geometri Hatası: {e}")
        return []

def filter_places_by_polyline(places: list, encoded_polyline: str) -> list:
    """
    Mekanları rotaya olan uzaklığına göre etiketler.
    Kategoriler:
    - 0-500m: "Yol Üstü" (Rotayı uzatmaz)
    - 500m-3000m: "Ufak Sapma" (Değebilir)
    - >3000m: Elenir.
    """
    if not encoded_polyline:
        return places

    try:
        coords = flexpolyline.decode(encoded_polyline)
        line_coords = [(lon, lat) for lat, lon in coords]
        route_line = LineString(line_coords)
        
        processed_places = []
        
        # Limitler (Derece cinsinden yaklaşık)
        # 1 derece ~ 111km -> 1km ~ 0.009 derece
        STRICT_LIMIT = 500 / 111000.0   # 500 metre
        FLEXIBLE_LIMIT = 3000 / 111000.0 # 3 km (Bu kadar sapmaya izin veriyoruz)

        for place in places:
            p_lat, p_lon = place.get("lat"), place.get("lon")
            if not p_lat or not p_lon: continue
                
            point = Point(p_lon, p_lat)
            distance_deg = route_line.distance(point)
            distance_meters = int(distance_deg * 111000)
            
            # --- ETIKETLEME MANTIĞI ---
            if distance_deg <= FLEXIBLE_LIMIT:
                # Durumu belirle
                if distance_deg <= STRICT_LIMIT:
                    place["konum_durumu"] = "✅ YOL ÜSTÜ"
                    place["sapma_mesafesi"] = f"{distance_meters} metre"
                else:
                    place["konum_durumu"] = "⚠️ SAPMA GEREKTİRİR"
                    place["sapma_mesafesi"] = f"{round(distance_meters/1000, 1)} km"
                
                # Matematiksel veriyi de ekleyelim ki LLM kıyaslasın
                place["mesafe_raw"] = distance_meters
                processed_places.append(place)

        # En yakından en uzağa sırala
        processed_places.sort(key=lambda x: x["mesafe_raw"])
        
        log.success(f"✅ Akıllı Filtre: {len(processed_places)} mekan analiz edildi.")
        return processed_places

    except Exception as e:
        log.error(f"Geometri Hatası: {e}")
        return places