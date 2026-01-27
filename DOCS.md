# 🌍 GeoIntel Backend Documentation

Bu proje, coğrafi sorguları hibrit bir mimari (OSM + Google + HERE) ile işleyen akıllı bir ajan sistemidir.

## 🏗️ Mimari
- **Orchestrator:** LangGraph tabanlı karar mekanizması.
- **City Agent:** FastMCP tabanlı, modüler araç seti.
- **DB:** PostGIS (Coğrafi Veri Tabanı).

## 🛠️ Araçlar (Tools)

| Araç Adı | Açıklama | Kaynak | Maliyet |
|----------|----------|--------|---------|
| `search_infrastructure_osm` | Kamusal alanları (Havalimanı, Park vb.) bulur. | OpenStreetMap | 🆓 Bedava |
| `search_places_google` | Ticari işletmeleri (Restoran, Kafe) ve puanlarını bulur. | Google Maps | 💰 Ücretli |
| `get_route_data` | İki nokta arasındaki mesafe ve süreyi hesaplar. | HERE Maps | 🆓/💰 Freemium |
| `get_weather` | Koordinat bazlı hava durumu getirir. | OpenWeather | 🆓 Freemium |
| `save_location` | Lokasyonu veritabanına kaydeder. | PostGIS | 🆓 Bedava |

## 🚀 Kurulum

1. `.env` dosyasını oluşturun.
2. `docker-compose up -d --build` komutuyla başlatın.
3. Orchestrator `http://localhost:8001/docs` adresinde çalışır.

## 🧪 Testler
Testleri çalıştırmak için:
```bash
pytest tests/