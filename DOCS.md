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

## 🗺️ WFS (İBB) Entegrasyonu

- **Genel WFS tool**: City Agent içinde `fetch_wfs_layer`
  - Parametreler: `base_url`, `type_name`, opsiyonel `bbox`, `src_epsg`
- **İBB preset tool**: City Agent içinde `fetch_ibb_dataset`
  - Önce `.env` içine `IBB_WFS_BASE_URL` ekleyin (örnek: `.env.example`)
  - Sonra `dataset_id` ile çağırın (örn: `ibb_afet_toplanma`)
- **Dataset listesi**: City Agent içinde `list_ibb_datasets` (hangi `dataset_id`’ler var görürsünüz)

## 🧪 Testler
Testleri çalıştırmak için:
```bash
pytest tests/
```

## 📌 OEK1 (Rapor) uyumluluk / eksik listesi

Rapor hedefleri ile mevcut kod tabanının fark analizi:
- `OEK1_GAP_ANALYSIS.md`