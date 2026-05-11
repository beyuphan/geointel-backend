import flexpolyline
import math
from shapely.geometry import Point, LineString, mapping
from shapely.ops import transform
import pyproj
from typing import Optional
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

def get_distance_from_route(location: dict, polyline_str: str) -> float:
    """
    Mekan (Point) ile rota (Polyline) arasındaki en kısa dik mesafeyi (metre) hesaplar.
    Hata durumunda 999999 döner.
    """
    try:
        if not polyline_str or polyline_str == "LATEST" or len(polyline_str) < 5:
            return 0

        try:
            decoded = flexpolyline.decode(polyline_str)
        except Exception:
            try:
                import polyline  # optional dependency
                decoded = polyline.decode(polyline_str)
            except Exception:
                return 999999

        if not decoded or len(decoded) < 2:
            return 999999

        line_coords = [(lon, lat) for lat, lon in decoded]
        route_line = LineString(line_coords)
        place_point = Point(location["lng"], location["lat"])

        route_line_m = transform(project_to_meters, route_line)
        place_point_m = transform(project_to_meters, place_point)

        distance = route_line_m.distance(place_point_m)

        if math.isinf(distance) or math.isnan(distance):
            return 999999

        return float(distance)
    except Exception as e:
        log.warning(f"⚠️ Mesafe hesaplama hatası: {e}")
        return 999999

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
    


def get_route_midpoint(
    encoded_polyline: str = None,
    geojson_geometry: dict = None,
    fraction: float = 0.5
) -> Optional[dict]:
    """
    Rota polyline'ı üzerindeki belirtilen kesirsel noktanın koordinatını döner.
    fraction=0.5 → yolun tam ortası (midpoint)
    fraction=0.25 → yolun ilk çeyreği vb.
    
    Returns:
        {"lat": float, "lon": float, "distance_from_start_km": float} veya None (hata)
    """
    try:
        line_coords = _get_line_coords(encoded_polyline, geojson_geometry)
        if not line_coords or len(line_coords) < 2:
            return None

        route_line = LineString(line_coords)
        route_line_m = transform(project_to_meters, route_line)
        total_length_m = route_line_m.length

        if total_length_m <= 0 or math.isnan(total_length_m):
            return None

        target_dist_m = total_length_m * max(0.0, min(1.0, fraction))
        point_m = route_line_m.interpolate(target_dist_m)
        point_gps = transform(project_to_gps, point_m)

        log.info(
            f"📍 [GEO] Route midpoint @ fraction={fraction:.2f}: "
            f"lat={point_gps.y:.5f}, lon={point_gps.x:.5f} "
            f"(+{target_dist_m/1000:.1f} km)"
        )
        return {
            "lat": point_gps.y,
            "lon": point_gps.x,
            "distance_from_start_km": round(target_dist_m / 1000, 2)
        }
    except Exception as e:
        log.error(f"❌ Geometri Hatası (get_route_midpoint): {e}")
        return None


def get_route_buffer_polygon(
    encoded_polyline: str = None,
    geojson_geometry: dict = None,
    buffer_meters: float = 5000.0
) -> Optional[dict]:
    """
    Rota polyline'ı etrafında 'buffer_meters' yarıçaplı bir GeoJSON Polygon döner.
    OSM/Google sorgularını 'başlangıç noktası' yerine 'tüm rota boyunca' yapmak için
    bu poligonun bbox'ını veya WKT'sini kullanabilirsiniz.

    Returns:
        GeoJSON Polygon dict (type + coordinates) veya None (hata)
    """
    try:
        line_coords = _get_line_coords(encoded_polyline, geojson_geometry)
        if not line_coords or len(line_coords) < 2:
            return None

        route_line = LineString(line_coords)
        route_line_m = transform(project_to_meters, route_line)

        # Metre uzayında buffer oluştur
        buffer_m = route_line_m.buffer(buffer_meters)

        # GPS'e geri çevir
        buffer_gps = transform(project_to_gps, buffer_m)

        # Shapely Polygon → GeoJSON dict
        geojson = mapping(buffer_gps)
        log.info(
            f"🗺️ [GEO] Route buffer oluşturuldu: {buffer_meters}m — "
            f"bbox {buffer_gps.bounds}"
        )
        return geojson
    except Exception as e:
        log.error(f"❌ Geometri Hatası (get_route_buffer_polygon): {e}")
        return None


def calculate_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """İki GPS koordinatı arasındaki mesafeyi Haversine formülü ile metre cinsinden hesaplar."""
    R = 6371000  # Dünya yarıçapı (metre)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c


def get_place_route_side(p_lat: float, p_lon: float, encoded_polyline: str) -> str:
    """
    Bir mekanın seyahat yönüne göre sağ mı sol mu tarafta olduğunu belirler.
    Cross-product (çapraz çarpım) yöntemi kullanır.
    
    Dönüş değerleri:
        'right'   → Seyahat yönüne göre SAĞ taraf (ideal — U-dönüşü gerekmez)
        'left'    → Seyahat yönüne göre SOL taraf (yolun karşısı — tehlikeli)
        'unknown' → Hesaplanamadı
    """
    try:
        if not encoded_polyline or len(encoded_polyline) < 5:
            return 'unknown'
        
        # Polyline'ı decode et
        try:
            decoded = flexpolyline.decode(encoded_polyline)
            if len(decoded) < 2:
                return 'unknown'
            line_coords = [(lat, lon) for lat, lon in decoded]
        except Exception:
            return 'unknown'
        
        # Mekana en yakın rota segmentini bul
        min_dist = float('inf')
        closest_seg_start = None
        closest_seg_end = None
        
        for i in range(len(line_coords) - 1):
            a_lat, a_lon = line_coords[i]
            b_lat, b_lon = line_coords[i + 1]
            
            # Segmente olan mesafeyi hesapla (basit öklid)
            # Orta noktaya olan mesafeyi kullan
            mid_lat = (a_lat + b_lat) / 2
            mid_lon = (a_lon + b_lon) / 2
            dist = (p_lat - mid_lat) ** 2 + (p_lon - mid_lon) ** 2
            
            if dist < min_dist:
                min_dist = dist
                closest_seg_start = (a_lat, a_lon)
                closest_seg_end = (b_lat, b_lon)
        
        if not closest_seg_start or not closest_seg_end:
            return 'unknown'
        
        # Çapraz çarpım ile yön tespiti
        # Vektör AB: seyahat yönü
        # Vektör AP: segment başından mekana olan yön
        ax, ay = closest_seg_start[1], closest_seg_start[0]  # lon, lat
        bx, by = closest_seg_end[1], closest_seg_end[0]
        px, py = p_lon, p_lat
        
        # Cross product z-bileşeni
        cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        
        # Cross product > 0 → P, AB'nin solunda
        # Cross product < 0 → P, AB'nin sağında
        # (Kuzey-doğu koordinat sisteminde sağ taraf negatif cross product)
        if cross < 0:
            return 'right'
        elif cross > 0:
            return 'left'
        else:
            return 'right'  # Tam üzerinde → sağ kabul et
    except Exception as e:
        log.warning(f"⚠️ Yön tespiti hatası: {e}")
        return 'unknown'