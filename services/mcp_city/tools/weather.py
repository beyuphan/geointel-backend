import httpx
import asyncio
from datetime import datetime, timezone, timedelta
from .config import settings, http_client
from logger import log
from .geometry import sample_route_points 


async def get_weather_simple(client, lat, lon):
    """Tekil nokta için hızlı sorgu (Batch işlemde kullanacağız)"""
    try:
        params = {
            "lat": lat, "lon": lon, 
            "appid": settings.OPENWEATHER_API_KEY, 
            "units": "metric",
            "exclude": "minutely,hourly,daily,alerts" # Sadece anlık yeterli
        }
        resp = await client.get(settings.OPENWEATHER_URL, params=params)
        return resp.json()
    except:
        return None

async def analyze_route_weather_handler(polyline: str) -> dict:
    """
    WEATHER SHIELD: Rota boyunca hava durumunu tarar ve risk raporu oluşturur.
    """
    if not polyline:
        return {"error": "Rota verisi (polyline) eksik."}

    # 1. Rotayı 40km'lik parçalara böl
    checkpoints = sample_route_points(polyline, interval_km=40)
    if not checkpoints:
        return {"error": "Rota geometrisi çözülemedi."}

    log.info(f"🛡️ [SHIELD] Hava Kalkanı Devrede: {len(checkpoints)} nokta taranıyor...")

    risks = []
    summary = []
    
    # 2. Paralel İstek At (Batch Request — V3: shared http_client)
    tasks = [get_weather_simple(http_client, p["lat"], p["lon"]) for p in checkpoints]
    results = await asyncio.gather(*tasks)

    # 3. Analiz ve Süzgeç
    for point, weather_data in zip(checkpoints, results):
        if not weather_data or "current" not in weather_data: continue

        current = weather_data["current"]
        temp = current.get("temp")
        condition = current.get("weather", [{}])[0].get("main", "") # Rain, Snow, Clear
        desc = current.get("weather", [{}])[0].get("description", "")
        
        # Risk Tespiti (LLM için bayraklar)
        is_risky = False
        risk_emoji = "🌤️"
        
        if condition in ["Rain", "Drizzle", "Thunderstorm"]:
            is_risky = True
            risk_emoji = "🌧️"
        elif condition in ["Snow"]:
            is_risky = True
            risk_emoji = "❄️"
        elif condition in ["Fog", "Mist"]:
            is_risky = True
            risk_emoji = "🌫️"
        elif temp < 2: # Buzlanma riski
            is_risky = True
            risk_emoji = "🧊"
        
        # Sadece riskli durumları veya başlangıç/bitiş noktalarını rapora ekle
        # (Nokta sayısı 0 ise Başlangıç, -1 ise Bitiş)
        if is_risky or point["km_point"] == 0 or point == checkpoints[-1]:
            summary.append({
                "km": f"{point['km_point']}. km",
                "durum": f"{risk_emoji} {desc.title()}",
                "sicaklik": f"{temp}°C",
                "riskli_mi": is_risky
            })
            
            if is_risky:
                risks.append(f"{point['km_point']}. km civarında {desc} ({temp}°C)")

    # 4. Final Rapor
    shield_report = {
        "tarama_noktasi_sayisi": len(checkpoints),
        "risk_durumu": "YÜKSEK" if len(risks) > 0 else "TEMİZ",
        "riskli_bolgeler": risks,
        "detayli_ozet": summary,
        "tavsiye": "Güzergah temiz görünüyor, iyi yolculuklar." if not risks else "Dikkat! Rotada kritik hava değişimleri var."
    }
    
    return shield_report



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