# services/mcp_city/tools/geometry.py
import flexpolyline
from shapely.geometry import Point, LineString
from logger import log

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