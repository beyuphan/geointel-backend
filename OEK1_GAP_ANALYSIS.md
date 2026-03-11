# OEK1 (Rapor) → Repo Gap Analizi

Bu doküman, `c:\Users\eyuph\Desktop\OEK1.pdf` raporunda tarif edilen hedef sistem ile mevcut `geointel-backend` kod tabanının durumunu karşılaştırır. Amaç: **eksikleri netleştirmek, uygulanabilir iş paketlerine bölmek ve geliştirmeyi buradan başlatmak**.

## Mevcut durum (repo içinde görülenler)

- **Host / Orchestrator (FastAPI + LangGraph)**: `services/orchestrator/main.py`
- **MCP Servers (FastMCP + JSON-RPC)**:
  - City: `services/mcp_city/server.py`
  - Intel: `services/mcp_intel/server.py`
  - Satellite: `services/mcp_satellite/server.py`
- **Pilot bölge yerel rotalama (pgRouting)**: `services/mcp_city/tools/local_routing.py`
- **Hibrit POI (OSM + Google)**: `services/mcp_city/tools/osm.py`, `services/mcp_city/tools/google.py`
- **Meteoroloji (OWM)**: `services/mcp_city/tools/weather.py`
- **STAC keşif + COG tabanlı NDVI fonksiyonu (kod seviyesi)**: `services/mcp_satellite/tools/stac_client.py`
- **PostGIS + pgRouting altyapısı**: `infra/postgres/init.sql`, `docker-compose.yml`

## Rapor gereksinimleri checklist’i

Aşağıdaki tabloda:
- **Durum**: ✅ Var / 🟡 Kısmi / ❌ Yok
- **Kanıt**: repo içi dosya referansı
- **Aksiyon**: yapılacak iş

| Gereksinim | Durum | Kanıt | Aksiyon |
|---|---:|---|---|
| Orkestrasyon katmanı (FastAPI, asenkron) | ✅ | `services/orchestrator/main.py` | API sözleşmesini sabitle (OpenAPI), auth/rate-limit ekle |
| MCP standardı (tools/resources/prompts) | 🟡 | `services/mcp_*/server.py` | “resources/prompts” primitive’lerini de ekle (en azından tool’lar için şema) |
| Hibrit MCP transport (stdio + SSE) | ❌ | (repo genelinde `stdio` kullanım izi yok) | Yerel/low-latency MCP server’ları stdio ile koştur, ağır analizleri SSE’de bırak |
| PostGIS + GiST indeksleme | ✅ | `infra/postgres/init.sql` | Şema/migration stratejisi ekle (alembic ya da SQL migration dizini) |
| pgRouting ile dinamik rotalama | 🟡 | `services/mcp_city/tools/local_routing.py` | Rapordaki dinamik maliyet (hava/eğim) + “forecast horizon” maliyetlerini uygula |
| HERE + OWM entegrasyonu | ✅ | `services/mcp_city/tools/here.py`, `weather.py` | Timeout/retry standardize et; maliyet/limit korumaları ekle |
| WFS (İBB) → GeoJSON dönüşümü | 🟡 | `services/mcp_city/tools/wfs.py`, `services/mcp_city/server.py` (`fetch_wfs_layer`) | İBB gerçek endpoint/typeNames ile doğrula; şema normalizasyonu ekle |
| ITRF96 (EPSG:5254) → WGS84 (EPSG:4326) dönüşümü | 🟡 | `services/mcp_city/tools/wfs.py` | GML geometry çeşitlerini genişlet (Multi*), bbox CRS senaryolarını netleştir |
| Cloud-native uydu: STAC keşif | ✅ | `services/mcp_satellite/tools/stac_client.py` | Sonuçları standart bir JSON şemaya bağla |
| COG streaming ile NDVI analizi | 🟡 | `SatelliteClient.calculate_ndvi()` var | MCP tool olarak dışa aç ve orchestrator’dan çağrılabilir yap |
| Spatial RAG (Sparse+Dense) | ❌ | (pgvector yok) | `pgvector` extension + embedding tablosu + retrieval pipeline ekle |
| pgvector kullanımı | ❌ | `infra/postgres/init.sql` | `CREATE EXTENSION vector;` + vektör tablo/indeks tasarımı |
| İzole scraping server (headless browser) | 🟡 | Intel tarafında Playwright var | Scraping’i ayrı servis/sınırlarla izole et (timeout, queue, sandbox) |

## Önceliklendirilmiş iş paketleri (öneri)

- **P0 (geliştirmeye başlamak için en gerekli)**
  - **WFS dönüşüm hattı** (raporda ana veri kaynağı): WFS→GeoJSON + EPSG dönüşümü
  - **NDVI tool’u**: uydu sunucusunda NDVI’yi MCP tool olarak expose etmek
  - **pgvector altyapısı**: extension + minimal embedding storage (RAG için zemin)
  - **Güvenlik/operasyon**: `/chat` auth + rate limit + CORS daraltma (public giriş noktası)

- **P1 (rapor “özgün değer” kısımları)**
  - **Dinamik maliyet fonksiyonu** (hava + forecast horizon) ve rota skoru
  - **Spatial RAG pipeline** (sparse PostGIS filtre + dense pgvector benzerlik + rerank)

- **P2 (ürünleştirme)**
  - Şema/migration düzeni, gözlemlenebilirlik (metrics/tracing), daha güçlü testler

## “Done” tanımı (rapora göre)

Minimum hedef:
- Orchestrator, MCP üzerinden **WFS veri çekebilir**, **NDVI hesaplayabilir**, **pilot bölgede dinamik maliyetli rota** üretebilir ve **Spatial RAG ile POI önerisi** verebilir.

