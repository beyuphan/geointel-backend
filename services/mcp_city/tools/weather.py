import httpx
import asyncio
from datetime import datetime, timezone, timedelta
from .config import settings, http_client
from logger import log
from .geometry import sample_route_points 


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

    # Rota uzunluğuna göre örnekleme aralığını dinamikleştir (Hız Optimizasyonu)
    # Rota > 400km ise 80km aralıklarla bak, değilse 40km.
    # Bu, 1000km'lik rotada API çağrı sayısını 25'ten 12'ye indirir.
    interval = 80 if len(polyline) > 1000 else 40 # Polyline uzunluğu yaklaşık bir göstergedir
    # Daha garanti bir mesafe tahmini için sample_route_points içinde mesafe kontrolü yapılır.
    
    checkpoints = sample_route_points(polyline, interval_km=interval)
    if not checkpoints:
        return {"error": "Rota geometrisi çözülemedi."}

    log.info(f"🛡️ [SHIELD] Hava Kalkanı Devrede: {len(checkpoints)} nokta taranıyor (Aralık: {interval}km)...")

    risks = []
    summary = []

    tasks = []
    for point in checkpoints:
        eta_min = departure_minutes_from_now + (point["km_point"] / avg_speed_kmh) * 60
        tasks.append(_get_forecast_at_eta(http_client, point["lat"], point["lon"], eta_min))
    results = await asyncio.gather(*tasks)

    for point, forecast in zip(checkpoints, results):
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

        if is_risky or km == 0 or point == checkpoints[-1]:
            summary.append({
                "km":            f"{km}. km",
                "tahmini_saat":  eta_str,   # Kullanicinin o noktada olacagi saat
                "durum":         f"{risk_emoji} {desc.title()}",
                "sicaklik":      f"{temp}°C" if temp is not None else "?",
                "ruzgar":        f"{wind} m/s",
                "riskli_mi":     is_risky,
            })
            if is_risky:
                risks.append(f"{km}. km (~{eta_str}'de) {risk_emoji} {desc} ({temp}°C)")

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