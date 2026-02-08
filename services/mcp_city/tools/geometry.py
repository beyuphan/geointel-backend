import flexpolyline
import math
from shapely.geometry import Point, LineString
from shapely.ops import transform
import pyproj
from loguru import logger as log

# --- PROJEKSİYON AYARLARI ---
# WGS84 (GPS - Lat/Lon) -> Web Mercator (Metre)
# Bu dönüşüm, mesafeleri "derece" yerine "metre" olarak hesaplamak için şarttır.
project_to_meters = pyproj.Transformer.from_proj(
    pyproj.Proj('epsg:4326'), # Kaynak: GPS
    pyproj.Proj('epsg:3857'), # Hedef: Metre (Web Mercator)
    always_xy=True
).transform

# Web Mercator (Metre) -> WGS84 (GPS)
project_to_gps = pyproj.Transformer.from_proj(
    pyproj.Proj('epsg:3857'), 
    pyproj.Proj('epsg:4326'), 
    always_xy=True
).transform

def _get_line_coords(encoded_polyline: str = None, geojson_geometry: dict = None) -> list:
    """
    Yardımcı Fonksiyon: Hem HERE Polyline hem de PostGIS GeoJSON formatını
    Shapely'nin anlayacağı [(lon, lat), (lon, lat)...] listesine çevirir.
    """
    line_coords = []
    
    # 1. DURUM: GeoJSON Varsa (Yerel DB'den geldiyse)
    if geojson_geometry and "coordinates" in geojson_geometry:
        # GeoJSON zaten [Lon, Lat] formatındadır.
        # Shapely de (x, y) yani (Lon, Lat) ister.
        raw_coords = geojson_geometry["coordinates"]
        # Eğer MultiLineString gelirse (bazen olabilir), ilk parçayı al
        if geojson_geometry["type"] == "MultiLineString":
            for part in raw_coords:
                line_coords.extend([tuple(c) for c in part])
        else:
            line_coords = [tuple(c) for c in raw_coords]

    # 2. DURUM: Polyline String Varsa (HERE API'den geldiyse)
    elif encoded_polyline and len(encoded_polyline) > 5:
        try:
            # flexpolyline decode -> [(lat, lon)] döner.
            decoded = flexpolyline.decode(encoded_polyline)
            # Shapely (lon, lat) ister. Ters çeviriyoruz.
            line_coords = [(lon, lat) for lat, lon in decoded]
        except Exception as e:
            log.error(f"Polyline decode hatası: {e}")
            return []
            
    return line_coords

def sample_route_points(encoded_polyline: str = None, geojson_geometry: dict = None, interval_km: int = 40) -> list:
    """
    Rotayı analiz eder ve her 'interval_km' mesafede bir koordinat örnekler.
    Hava durumu analizi için kullanılır.
    """
    if not encoded_polyline and not geojson_geometry:
        return []

    try:
        # Koordinatları al
        line_coords = _get_line_coords(encoded_polyline, geojson_geometry)
        
        if not line_coords or len(line_coords) < 2: 
            return []
        
        # Geometriyi oluştur (GPS Koordinatlarında)
        route_line = LineString(line_coords)
        
        # Metre cinsine çevir (Doğru hesaplama için şart)
        route_line_m = transform(project_to_meters, route_line)
        total_length_m = route_line_m.length
        
        if total_length_m <= 0 or math.isnan(total_length_m):
            return []

        interval_m = interval_km * 1000
        sampled_points = []
        current_dist = 0
        
        # Yol boyunca belirli aralıklarla nokta al
        while current_dist <= total_length_m:
            # Noktayı bul (Metre uzayında)
            point_m = route_line_m.interpolate(current_dist)
            # GPS'e geri çevir
            point_gps = transform(project_to_gps, point_m)
            
            sampled_points.append({
                "lat": point_gps.y,
                "lon": point_gps.x,
                "km_point": int(current_dist / 1000)
            })
            current_dist += interval_m
            
        # Bitiş noktasını da ekle (Eğer son nokta çok yakın değilse)
        if (total_length_m - (current_dist - interval_m)) > 5000: # Son noktaya 5km'den fazla varsa
            end_point_m = route_line_m.interpolate(total_length_m)
            end_point_gps = transform(project_to_gps, end_point_m)
            sampled_points.append({
                "lat": end_point_gps.y,
                "lon": end_point_gps.x,
                "km_point": int(total_length_m / 1000)
            })

        log.info(f"📏 [GEO] Rota {int(total_length_m/1000)} km, {len(sampled_points)} analiz noktasına bölündü.")
        return sampled_points

    except Exception as e:
        log.error(f"❌ Geometri Hatası (sample_route_points): {e}")
        return []

def filter_places_by_polyline(places: list, encoded_polyline: str = None, geojson_geometry: dict = None) -> list:
    """
    Mekanları rotaya olan uzaklığına göre etiketler.
    StandardPlace listesi alır, 'konum_durumu' ekleyip geri döner.
    """
    if not places: return []
    
    # Rota verisi yoksa filtreleme yapmadan dön
    if not encoded_polyline and not geojson_geometry:
        return places

    try:
        # Koordinatları al
        line_coords = _get_line_coords(encoded_polyline, geojson_geometry)
        
        if len(line_coords) < 2:
            return places

        route_line = LineString(line_coords)
        
        processed_places = []
        
        # Limitler (Derece cinsinden yaklaşık değerler)
        # 1 derece ~ 111km -> 1km ~ 0.009 derece
        # Bu yöntem 'project_to_meters' kullanmaktan daha hızlıdır (binlerce mekan için)
        STRICT_LIMIT = 0.0045   # ~500 metre
        FLEXIBLE_LIMIT = 0.027  # ~3 km

        for place in places:
            p_lat = place.get("lat")
            p_lon = place.get("lon")
            
            if not p_lat or not p_lon: continue
                
            try:
                point = Point(p_lon, p_lat)
                # Distance, derece cinsinden döner
                distance_deg = route_line.distance(point)
                
                if math.isinf(distance_deg) or math.isnan(distance_deg):
                    continue

                # Metreye çevir (Yaklaşık)
                distance_meters = int(distance_deg * 111000)
                
                # --- FİLTRELEME MANTIĞI ---
                if distance_deg <= FLEXIBLE_LIMIT:
                    # Durumu belirle
                    if distance_deg <= STRICT_LIMIT:
                        place["konum_durumu"] = "✅ YOL ÜSTÜ"
                    else:
                        place["konum_durumu"] = "⚠️ SAPMA GEREKTİRİR"
                    
                    # Kullanıcı dostu mesafe stringi
                    if distance_meters < 1000:
                        place["sapma_mesafesi"] = f"{distance_meters} metre"
                    else:
                        place["sapma_mesafesi"] = f"{round(distance_meters/1000, 1)} km"
                    
                    # Sıralama için ham mesafe
                    place["mesafe_raw"] = distance_meters
                    processed_places.append(place)
            
            except Exception:
                continue # Tekil bir mekan hatası tüm döngüyü kırmasın

        # En yakından en uzağa sırala
        processed_places.sort(key=lambda x: x.get("mesafe_raw", 999999))
        
        log.success(f"✅ Akıllı Filtre: {len(places)} mekandan {len(processed_places)} tanesi rotaya uygun.")
        return processed_places

    except Exception as e:
        log.error(f"Geometri Hatası (filter_places_by_polyline): {e}")
        return places # Hata durumunda filtreleme yapmadan ham listeyi dön