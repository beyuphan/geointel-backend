import httpx
from datetime import datetime, timezone, timedelta
from .config import settings

async def get_weather_handler(lat: float, lon: float) -> dict:
    """Anlık ve önümüzdeki saatlerin hava durumu analizi (Zaman Damgalı)."""
    try:
        params = {
            "lat": lat, "lon": lon, 
            "appid": settings.OPENWEATHER_API_KEY, 
            "units": "metric", 
            "exclude": "minutely,alerts" # Daily kalsın, belki yarına bakıyordur
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(settings.OPENWEATHER_URL, params=params)
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