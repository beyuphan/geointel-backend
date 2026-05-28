import httpx
import asyncio
from datetime import datetime, timezone, timedelta
from .config import settings, http_client
from logger import log
from .geometry import sample_route_points


# 13. Tur — Lightweight reverse-geocode cache (lat/lon → (il, ilçe))
# Process-local cache; Nominatim rate-limit'i (1 req/sec) buffer'lar.
_GEOCODE_CACHE: dict[tuple[float, float], tuple[str, str] | None] = {}


async def _reverse_geocode_short(
    client, lat: float, lon: float,
) -> tuple[str, str] | None:
    """Nominatim → (il, ilçe) tuple. Cache hit'lerde anında döner.

    Hata/fail → None (caller km fallback'i kullanır).
    """
    key = (round(lat, 3), round(lon, 3))  # ~110m hassasiyet
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    try:
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            "format": "json",
            "lat": lat,
            "lon": lon,
            "zoom": 10,                # ilçe seviyesi
            "addressdetails": 1,
            "accept-language": "tr",
        }
        headers = {"User-Agent": "GeoIntel_Weather/1.0"}
        resp = await client.get(url, params=params, headers=headers, timeout=8.0)
        if resp.status_code != 200:
            _GEOCODE_CACHE[key] = None
            return None
        data = resp.json()
        addr = data.get("address", {}) or {}
        # İl: province / state ; İlçe: town / county / city_district / suburb
        il = addr.get("province") or addr.get("state") or ""
        ilce = (
            addr.get("town")
            or addr.get("county")
            or addr.get("city_district")
            or addr.get("suburb")
            or addr.get("city")
            or ""
        )
        if not il and not ilce:
            _GEOCODE_CACHE[key] = None
            return None
        result = (il.strip(), ilce.strip())
        _GEOCODE_CACHE[key] = result
        return result
    except Exception as e:
        log.warning(f"⚠️ [RevGeocodeShort] lat={lat:.3f} lon={lon:.3f}: {e}")
        _GEOCODE_CACHE[key] = None
        return None


async def _get_forecast_at_eta(client, lat: float, lon: float, eta_minutes: float) -> dict | None:
    """
    ETA dakikasına en yakın hava tahminini döner.
    OpenWeatherMap /forecast → 3 saatlik dilimler, 5 gün.
    """
    try:
        # /forecast endpoint'i (onecall değil)
        forecast_url = "https://api.openweathermap.org/data/2.5/forecast"
        params = {
            "lat": lat, "lon": lon,
            "appid": settings.OPENWEATHER_API_KEY,
            "units": "metric",
            "cnt": 16,   # max 48 saat (16 x 3h)
        }
        resp = await client.get(forecast_url, params=params)
        data = resp.json()

        forecast_list = data.get("list", [])
        if not forecast_list:
            return None

        # ETA'yı UTC timestamp'e çevir
        now_ts = datetime.now(timezone.utc).timestamp()
        target_ts = now_ts + (eta_minutes * 60)

        # En yakın forecast dilimini sec
        best = min(forecast_list, key=lambda f: abs(f["dt"] - target_ts))
        return best

    except Exception as e:
        log.warning(f"⚠️ [ForecastETA] lat={lat:.3f} lon={lon:.3f}: {e}")
        return None

async def analyze_route_weather_handler(
    polyline: str,
    avg_speed_kmh: float = 80.0,
    departure_minutes_from_now: float = 0.0,
) -> dict:
    """
    WEATHER SHIELD v2 — ETA-Adjusted Forecast.

    Her waypoint için tahmini varış süresi hesaplanır ve o ana ait
    forecast verisi çekilir. "320. km'de yağmur var" = gerçekten
    o noktaya vardığındaki hava tahmindir, şu anki değil.

    Args:
        polyline: Rota polyline'i
        avg_speed_kmh: Ortalama hız (varsayılan 80 km/h)
        departure_minutes_from_now: Kaç dakika sonra yola çıkılacak
    """
    if not polyline:
        return {"error": "Rota verisi (polyline) eksik."}

    # Latency v2 — Strategist akışında Nominatim çağrısı zaten yoktur,
    # weather için sample aralığını gevşet: kısa rotalarda 60km, uzun rotalarda 120km.
    # 1000km rotada 16→9, 400km rotada 10→7 çağrıya indirir.
    interval = 60 if len(polyline) > 1000 else 40
    # Daha garanti bir mesafe tahmini için sample_route_points içinde mesafe kontrolü yapılır.
    
    checkpoints = sample_route_points(polyline, interval_km=interval)
    if not checkpoints:
        return {"error": "Rota geometrisi çözülemedi."}

    log.info(f"🛡️ [SHIELD] Hava Kalkanı Devrede: {len(checkpoints)} nokta taranıyor (Aralık: {interval}km)...")

    risks = []
    summary = []

    # 13. Tur — forecast + reverse-geocode paralel
    forecast_tasks = []
    geo_tasks = []
    for point in checkpoints:
        eta_min = departure_minutes_from_now + (point["km_point"] / avg_speed_kmh) * 60
        forecast_tasks.append(
            _get_forecast_at_eta(http_client, point["lat"], point["lon"], eta_min)
        )
        geo_tasks.append(
            _reverse_geocode_short(http_client, point["lat"], point["lon"])
        )
    results = await asyncio.gather(*forecast_tasks)
    geo_results = await asyncio.gather(*geo_tasks)

    for point, forecast, geo in zip(checkpoints, results, geo_results):
        if not forecast:
            continue

        km = point["km_point"]
        eta_min = departure_minutes_from_now + (km / avg_speed_kmh) * 60
        eta_dt  = datetime.now(timezone.utc) + timedelta(minutes=eta_min)
        eta_str = eta_dt.strftime("%H:%M")

        main_weather = forecast.get("weather", [{}])[0]
        condition    = main_weather.get("main", "")
        desc         = main_weather.get("description", "")
        temp         = forecast.get("main", {}).get("temp")
        wind         = forecast.get("wind", {}).get("speed", 0)
        pop_raw      = forecast.get("pop", 0)
        rain_prob    = int((pop_raw or 0) * 100)

        is_risky   = False
        risk_emoji = "🌤️"

        if condition in ["Rain", "Drizzle", "Thunderstorm"]:
            is_risky, risk_emoji = True, "🌧️"
        elif condition == "Snow":
            is_risky, risk_emoji = True, "❄️"
        elif condition in ["Fog", "Mist"]:
            is_risky, risk_emoji = True, "🌫️"
        elif temp is not None and temp < 2:
            is_risky, risk_emoji = True, "🧈"

        # 12. Tur — Severity scale (UI renk kodu + intensity bar için)
        # condition + rain_prob + wind_speed + temp baz alınarak 4 seviye
        severity = "acik"
        if condition == "Thunderstorm" or rain_prob >= 70:
            severity = "siddetli"
        elif condition == "Snow":
            severity = "siddetli" if rain_prob >= 50 else "orta"
        elif condition == "Rain":
            severity = "orta" if rain_prob >= 40 else "hafif"
        elif condition == "Drizzle" or (condition in ("Clouds",) and rain_prob >= 30):
            severity = "hafif"
        elif condition in ("Fog", "Mist"):
            severity = "orta"
        elif temp is not None and temp < 2:
            severity = "orta"
        elif wind and wind >= 12:
            # Şiddetli rüzgar (43 km/s+) → uyarı
            severity = max(severity, "hafif", key=["acik", "hafif", "orta", "siddetli"].index)

        # Intensity yüzdesi (UI bar için 0-100)
        intensity_pct = max(rain_prob, {
            "acik": 5, "hafif": 30, "orta": 60, "siddetli": 90,
        }[severity])
        intensity_pct = min(100, intensity_pct)

        # 13. Tur — Konum etiketi (il/ilçe), reverse-geocode fail ise km fallback
        il = geo[0] if geo else None
        ilce = geo[1] if geo else None
        if ilce and il:
            location_label = f"{ilce}, {il}"
        elif il:
            location_label = il
        elif ilce:
            location_label = ilce
        else:
            location_label = f"{km}. km"

        summary.append({
            "km":            f"{km}. km",
            "km_int":        km,                                # 12. Tur — int km (UI timeline için)
            "tahmini_saat":  eta_str,
            "durum":         f"{risk_emoji} {desc.title()}",
            "sicaklik":      f"{temp}°C" if temp is not None else "?",
            "yagis_olasiligi": f"%{rain_prob}",
            "yagis_pct":     rain_prob,                          # 12. Tur — int (kalk yağış)
            "ruzgar":        f"{wind} m/s",
            "ruzgar_ms":     float(wind or 0),                   # 12. Tur — float (rüzgar)
            "riskli_mi":     is_risky,
            "severity":      severity,                           # 12. Tur — acik/hafif/orta/siddetli
            "intensity_pct": intensity_pct,                      # 12. Tur — UI bar 0-100
            "emoji":         risk_emoji,                         # 12. Tur — emoji ayrı (kolay render)
            "il":            il,                                  # 13. Tur — Çorum vs.
            "ilce":          ilce,                                # 13. Tur — Sungurlu vs.
            "location_label": location_label,                     # 13. Tur — "Sungurlu, Çorum"
            "lat":           point["lat"],                        # 14. Tur — mini map marker için
            "lon":           point["lon"],                        # 14. Tur
        })
        if is_risky:
                risks.append({
                    "km": f"{km}. km",
                    "km_int": km,
                    "saat": eta_str,
                    "durum": f"{risk_emoji} {desc.title()}",
                    "sicaklik": f"{temp}°C" if temp is not None else "?",
                    "yagis_olasiligi": f"%{rain_prob}",
                    "yagis_pct": rain_prob,
                    "ruzgar_ms": float(wind or 0),
                    "severity": severity,
                    "intensity_pct": intensity_pct,
                    "emoji": risk_emoji,
                    "il": il,
                    "ilce": ilce,
                    "location_label": location_label,
                })

    return {
        "tarama_noktasi_sayisi": len(checkpoints),
        "ortalama_hiz_kmh":      avg_speed_kmh,
        "risk_durumu":           "YÜKSEK" if risks else "TEMİZ",
        "not": (
            "Hava uyarıları o noktaya tahmini varış saatine göre hesaplanmıştır "
            "(şu anki değil, o saatteki tahmin)."
        ),
        "riskli_bolgeler": risks,
        "detayli_ozet":    summary,
        "tavsiye": (
            "Güzergah temiz görünüyor, iyi yolculuklar."
            if not risks else
            "Dikkat! Rotada kritik hava değişimleri var."
        ),
    }


async def get_weather_handler(lat: float, lon: float) -> dict:
    """Anlık ve önümüzdeki saatlerin hava durumu analizi (Zaman Damgalı)."""
    try:
        params = {
            "lat": lat, "lon": lon, 
            "appid": settings.OPENWEATHER_API_KEY, 
            "units": "metric", 
            "exclude": "minutely,alerts" # Daily kalsın, belki yarına bakıyordur
        }
        
        resp = await http_client.get(settings.OPENWEATHER_URL, params=params)
        data = resp.json()
        
        if resp.status_code != 200:
            return {"error": f"Hava durumu alınamadı: {data.get('message')}"}

        # ZAMAN AYARI (UTC+3 Türkiye Saati varsayımıyla veya timezone offset ile)
        # OpenWeather 'timezone_offset' saniye cinsinden verir.
        offset = data.get("timezone_offset", 0)
        tz = timezone(timedelta(seconds=offset))

        # ŞU ANKİ DURUM
        current = data.get("current", {})
        current_time = datetime.fromtimestamp(current.get("dt"), tz).strftime("%H:%M")
        
        current_obj = {
            "saat": f"ŞU AN ({current_time})", # LLM bunu görünce anlar
            "sicaklik": f"{current.get('temp')}°C",
            "hissedilen": f"{current.get('feels_like')}°C",
            "durum": current.get("weather", [{}])[0].get("description"),
            "ruzgar": f"{current.get('wind_speed')} m/s"
        }

        # SAATLİK TAHMİN (Önümüzdeki 5 saat)
        hourly_summary = []
        for h in data.get("hourly", [])[:5]:
            dt_str = datetime.fromtimestamp(h.get("dt"), tz).strftime("%H:%M")
            hourly_summary.append({
                "saat": dt_str,
                "tahmin": f"{h.get('temp')}°C (Hissedilen: {h.get('feels_like')}), {h.get('weather', [{}])[0].get('description')}"
            })

        # GÜNLÜK TAHMİN (Yarın için ipucu)
        # Eğer kullanıcı "Yarın nasıl?" derse buraya bakmalı
        daily_summary = []
        for d in data.get("daily", [])[:2]: # Bugün ve Yarın
            day_name = datetime.fromtimestamp(d.get("dt"), tz).strftime("%A (Günlük)")
            daily_summary.append({
                "gun": day_name,
                "gunduz_max": f"{d.get('temp', {}).get('day')}°C",
                "gece_min": f"{d.get('temp', {}).get('night')}°C",
                "aciklama": d.get("weather", [{}])[0].get("description")
            })

        return {
            "lokasyon_koordinat": f"{lat},{lon}",
            "bolge_saat_dilimi": data.get("timezone"),
            "ANLIK_DURUM": current_obj,        # Büyük harfle dikkat çekiyoruz
            "ONUMUZDEKI_SAATLER": hourly_summary,
            "GENEL_GUNLUK_RAPOR": daily_summary,
            "uyari": "Verilerdeki 'saat' bilgisini dikkate al. Gündüz sıcaklığı ile geceyi karıştırma."
        }

    except Exception as e:
        return {"error": str(e)}