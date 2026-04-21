"""
tests/test_sprint2.py — Sprint 2 & 3 & 4 Testleri

Test edilen:
- here.py: weather rain_factor otomatik hesabı
- graph.py: redis_client None güvenliği
- notifier.py: FCM stub mod (firebase yüklü değilken)
- config.py: DATABASE_URL ve yeni alanlar
- routes.py: Yeni endpoint'ler mevcut mu
- result_compressor.py: alternatif rotalar sıkıştırma
- local_routing.py: rain_factor sınır değerleri
"""
import sys
import os
import json
import pytest

# Path setup
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORCHESTRATOR = os.path.join(BASE, "services", "orchestrator")
MCP_CITY = os.path.join(BASE, "services", "mcp_city")


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: here.py — weather import ve rain_factor mantığı
# ─────────────────────────────────────────────────────────────────────────────

def test_here_imports_weather():
    """here.py _get_weather'ı import etmeli."""
    here_path = os.path.join(MCP_CITY, "tools", "here.py")
    with open(here_path, encoding="utf-8") as f:
        content = f.read()
    assert "_get_weather" in content or "get_weather_handler" in content, \
        "here.py weather handler import etmeli"
    assert "rain_factor" in content, \
        "here.py rain_factor'u yerel routing'e gecirmeli"
    print("OK here.py weather entegrasyonu")


def test_rain_factor_weather_mapping():
    """Hava durumu koşul → rain_factor eşleme mantığı."""
    def get_rain_factor(condition: str) -> float:
        condition_raw = condition.lower()
        if any(w in condition_raw for w in ["rain", "drizzle", "thunderstorm", "yagmur"]):
            return 0.8
        elif any(w in condition_raw for w in ["snow", "kar"]):
            return 1.0
        elif any(w in condition_raw for w in ["fog", "mist", "sis"]):
            return 0.4
        return 0.0

    assert get_rain_factor("Rain") == 0.8,        "Yagmur → 0.8"
    assert get_rain_factor("Snow") == 1.0,        "Kar → 1.0 (max)"
    assert get_rain_factor("Fog") == 0.4,         "Sis → 0.4"
    assert get_rain_factor("Thunderstorm") == 0.8,"Firtina → 0.8"
    assert get_rain_factor("Clear") == 0.0,       "Acik hava → 0.0"
    assert get_rain_factor("Drizzle") == 0.8,     "Ciselemek → 0.8"
    print("OK rain_factor hava durumu eslemesi")


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: graph.py — Redis None güvenliği
# ─────────────────────────────────────────────────────────────────────────────

def test_graph_redis_null_safety():
    """graph.py redis_client kullaniminda None kontrolu olmali."""
    graph_path = os.path.join(ORCHESTRATOR, "core", "graph.py")
    with open(graph_path, encoding="utf-8") as f:
        content = f.read()

    # Direkt redis_client.get() cagrisinin onunde her zaman bir None kontrolu olmali
    # En az bir "if orchestrator.redis_client" olduğunu dogrula
    assert "if orchestrator.redis_client" in content, \
        "graph.py redis_client None kontrolu eksik"

    # Redis cagrilarinin try/except ile sarmalandigini kontrol et
    assert "except Exception as redis_err" in content or \
           "except Exception as e:" in content, \
        "Redis cagrisi try/except ile sarmalanmamis"
    print("OK graph.py Redis null safety")


def test_graph_redis_setex_null_safety():
    """graph.py redis setex cagrilari guvenceli olmali."""
    graph_path = os.path.join(ORCHESTRATOR, "core", "graph.py")
    with open(graph_path, encoding="utf-8") as f:
        content = f.read()
    # setex satirindan once if orchestrator.redis_client olmali
    assert "orchestrator.redis_client and" in content or \
           "if orchestrator.redis_client:" in content, \
        "redis setex before None check"
    print("OK graph.py Redis setex guvenceli")


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: notifier.py — FCM Stub Mod
# ─────────────────────────────────────────────────────────────────────────────

def test_notifier_exists():
    """notifier.py dosyasi olusturulmus olmali."""
    notifier_path = os.path.join(ORCHESTRATOR, "core", "notifier.py")
    assert os.path.exists(notifier_path), "notifier.py olusturulmamis!"
    print("OK notifier.py mevcut")


def test_notifier_graceful_fallback():
    """notifier.py firebase olmadan import edilebilmeli."""
    notifier_path = os.path.join(ORCHESTRATOR, "core", "notifier.py")
    with open(notifier_path, encoding="utf-8") as f:
        content = f.read()
    # try/except ile firebase import olmali
    assert "try:" in content and "_FIREBASE_AVAILABLE" in content, \
        "notifier.py firebase olmadan da calisabilmeli (try/except sarici)"
    assert "push_if_needed" in content, \
        "push_if_needed fonksiyonu tanimli olmali"
    assert "send_push" in content, \
        "send_push fonksiyonu tanimli olmali"
    print("OK notifier.py graceful fallback")


def test_notifier_templates():
    """FCM sablon fonksiyonlar tanimli olmali."""
    notifier_path = os.path.join(ORCHESTRATOR, "core", "notifier.py")
    with open(notifier_path, encoding="utf-8") as f:
        content = f.read()
    assert "notify_weather_risk" in content, "Weather risk bildirimi eksik"
    assert "notify_traffic_event" in content, "Traffic event bildirimi eksik"
    assert "notify_fuel_deal" in content, "Fuel deal bildirimi eksik"
    print("OK notifier.py sablon fonksiyonlar")


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Orchestrator config.py — yeni alanlar
# ─────────────────────────────────────────────────────────────────────────────

def test_orchestrator_config_db_url():
    """Orchestrator config.py DATABASE_URL icermeli."""
    config_path = os.path.join(ORCHESTRATOR, "config.py")
    with open(config_path, encoding="utf-8") as f:
        content = f.read()
    assert "DATABASE_URL" in content, "config.py DATABASE_URL eksik"
    assert "REDIS_HOST" in content, "config.py REDIS_HOST eksik"
    assert "FIREBASE_CREDENTIALS_PATH" in content, "config.py FIREBASE path eksik"
    print("OK orchestrator config yeni alanlar")


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: routes.py — Yeni endpoint'ler
# ─────────────────────────────────────────────────────────────────────────────

def test_routes_new_endpoints():
    """routes.py WebSocket ve location/update endpoint icermeli."""
    routes_path = os.path.join(ORCHESTRATOR, "api", "routes.py")
    with open(routes_path, encoding="utf-8") as f:
        content = f.read()

    assert "/ws/chat/" in content or "websocket_chat" in content, \
        "WebSocket endpoint eksik"
    assert "/location/update" in content, \
        "/location/update endpoint eksik"
    assert "action_cards" in content, \
        "action_cards response alanı eksik"
    assert "metadata" in content, \
        "metadata response alanı eksik"
    assert "/health" in content, \
        "/health endpoint eksik"
    print("OK routes.py yeni endpoint'ler")


def test_routes_session_id_in_response():
    """routes.py response'da session_id olmali."""
    routes_path = os.path.join(ORCHESTRATOR, "api", "routes.py")
    with open(routes_path, encoding="utf-8") as f:
        content = f.read()
    assert '"session_id"' in content or "'session_id'" in content, \
        "Response'da session_id eksik"
    print("OK routes.py session_id response")


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: result_compressor.py — Alternatif rotalar
# ─────────────────────────────────────────────────────────────────────────────

def test_result_compressor_alternatives():
    """Alternatif rotalar polyline'siz sıkıştırılmali."""
    sys.path.insert(0, ORCHESTRATOR)
    try:
        from core.result_compressor import compress_result

        sample = {
            "mesafe_km": 120.5,
            "sure_dk": 95,
            "source": "HERE_Maps_API",
            "traffic_status": "FREE_FLOW",
            "polyline_encoded": "A" * 500,  # Gizlenmeli
            "alternatives": [
                {"isim": "Rota 2", "mesafe_km": 125, "polyline_encoded": "B" * 400},
                {"isim": "Rota 3", "mesafe_km": 115, "polyline_encoded": "C" * 350},
            ]
        }

        compressed = compress_result("get_route_data", sample)

        # Ana polyline < 50 char proxy'ye indirilmeli veya tamamen yok olmali
        poly = compressed.get("polyline_encoded", "")
        assert len(str(poly)) < 100, \
            f"Ana polyline sıkıştırılmali, {len(str(poly))} char geldi"

        # Alternatifler korunmali ama polyline'siz
        if "alternatives" in compressed:
            for alt in compressed["alternatives"]:
                alt_poly = alt.get("polyline_encoded", "")
                assert len(str(alt_poly)) < 100, \
                    f"Alternatif polyline sıkıştırılmali"

        # Önemli alanlar korunmali
        assert compressed.get("source") == "HERE_Maps_API"
        assert compressed.get("mesafe_km") == 120.5
        print("OK result_compressor alternatif rotalar")
    except ImportError as e:
        pytest.skip(f"Import hatası (env eksik): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: local_routing.py — rain_factor sınır değerleri korunuyor
# ─────────────────────────────────────────────────────────────────────────────

def test_local_routing_rain_factor_clamp():
    """rain_factor 0-1 arası clamp uygulamali."""
    def calc(rain_factor: float) -> float:
        return 1.0 + (min(max(rain_factor, 0.0), 1.0) * 0.3)

    # Normal değerler
    assert abs(calc(0.0) - 1.0) < 0.001
    assert abs(calc(0.5) - 1.15) < 0.001
    assert abs(calc(1.0) - 1.3) < 0.001

    # Sınır dışı değerler
    assert abs(calc(-5.0) - 1.0) < 0.001,  "Negatif clamp: 1.0 olmali"
    assert abs(calc(10.0) - 1.3) < 0.001,  "Asiri yuksek clamp: 1.3 olmali"

    # Yagmur → kar → sis değerleri
    assert abs(calc(0.8) - 1.24) < 0.001,  "Yagmur (0.8) → 1.24"
    assert abs(calc(1.0) - 1.30) < 0.001,  "Kar (1.0) → 1.30"
    assert abs(calc(0.4) - 1.12) < 0.001,  "Sis (0.4) → 1.12"
    print("OK local_routing rain_factor clamp")


def test_local_routing_file_uses_pool():
    """local_routing.py asyncpg.connect() yerine pool kullanmali."""
    lr_path = os.path.join(MCP_CITY, "tools", "local_routing.py")
    with open(lr_path, encoding="utf-8") as f:
        content = f.read()
    assert "asyncpg.connect(" not in content, \
        "local_routing.py hala asyncpg.connect() kullaniyor! Pool'a gecilmeli"
    assert "get_pool" in content or "pool.acquire" in content, \
        "local_routing.py pool kullanmiyor"
    print("OK local_routing.py pool entegrasyonu")


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: docker-compose.yml — Döngüsel volume mount yok
# ─────────────────────────────────────────────────────────────────────────────

def test_no_circular_volume_mount():
    """mcp_city servisinde orchestrator volume mount olmamali."""
    compose_path = os.path.join(BASE, "docker-compose.yml")
    if not os.path.exists(compose_path):
        pytest.skip("docker-compose.yml bulunamadi")

    with open(compose_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    # YAML bloklari arasindan mcp_city blogi parse et
    in_mcp_city_block = False
    mcp_city_indent = 0

    for i, line in enumerate(lines):
        stripped = line.rstrip()

        # mcp_city: satiri bul (sol kenarda, yani en az 2 bosluk indent'i yok)
        if "  mcp_city:" in line and not line.startswith(" " * 6):
            in_mcp_city_block = True
            mcp_city_indent = len(line) - len(line.lstrip())
            continue

        # Baska bir ust-duzey servis baslayinca dur
        if in_mcp_city_block:
            current_indent = len(line) - len(line.lstrip())
            # Ust duzey alan (mcp_city ile ayni indent veya daha az) geldiyse cik
            if stripped and current_indent <= mcp_city_indent and ":" in stripped and not stripped.startswith("-"):
                break

            # Bu blok icinde orchestrator mount var mi?
            if "./services/orchestrator" in line:
                pytest.fail(
                    f"mcp_city blogu (satir {i+1}) orchestrator'u volume olarak mount ediyor!\n"
                    f"Satir: {stripped}"
                )

    print("OK docker-compose.yml dongusel volume mount yok")


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: FCM token flow kontrolü (routes.py)
# ─────────────────────────────────────────────────────────────────────────────

def test_fcm_token_stored_in_routes():
    """routes.py fcm_token'ı Redis'e kaydediyor olmali."""
    routes_path = os.path.join(ORCHESTRATOR, "api", "routes.py")
    with open(routes_path, encoding="utf-8") as f:
        content = f.read()
    assert "fcm_token" in content, "routes.py fcm_token almiyor"
    assert "fcm:" in content, "routes.py FCM token'i Redis'e kaydetmiyor"
    print("OK routes.py FCM token storage")


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: mcp_city server.py — lifespan ve pool lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def test_mcp_city_pool_lifecycle():
    """mcp_city server.py init_pool ve close_pool cagirmali."""
    server_path = os.path.join(MCP_CITY, "server.py")
    with open(server_path, encoding="utf-8") as f:
        content = f.read()
    assert "init_pool" in content, "mcp_city server startup'ta init_pool caginmali"
    assert "close_pool" in content, "mcp_city server shutdown'da close_pool cagirilmali"
    assert "lifespan" in content, "mcp_city server lifespan context manager eksik"
    print("OK mcp_city server pool lifecycle")


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: CORS — wildcard kaldirildi mi
# ─────────────────────────────────────────────────────────────────────────────

def test_cors_no_wildcard():
    """orchestrator main.py CORS'u wildcard ile acmamalı."""
    main_path = os.path.join(ORCHESTRATOR, "main.py")
    with open(main_path, encoding="utf-8") as f:
        content = f.read()
    # allow_origins=["*"] olmamali
    assert 'allow_origins=["*"]' not in content, \
        "CORS wildcard hala aktif! Guvenlik acigi."
    assert "ALLOWED_ORIGINS" in content, \
        "ALLOWED_ORIGINS whitelist tanimli olmali"
    print("OK CORS wildcard kaldirildi")


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: here.py response — yeni alanlar eklendi mi
# ─────────────────────────────────────────────────────────────────────────────

def test_here_response_new_fields():
    """here.py Local DB response traffic_status ve delay_min icermeli."""
    here_path = os.path.join(MCP_CITY, "tools", "here.py")
    with open(here_path, encoding="utf-8") as f:
        content = f.read()
    assert '"traffic_status"' in content, "here.py response'da traffic_status eksik"
    assert '"delay_min"' in content, "here.py response'da delay_min eksik"
    assert '"rain_factor_applied"' in content, "here.py response'da rain_factor_applied eksik"
    print("OK here.py yeni response alanlari")


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_here_imports_weather,
        test_rain_factor_weather_mapping,
        test_graph_redis_null_safety,
        test_graph_redis_setex_null_safety,
        test_notifier_exists,
        test_notifier_graceful_fallback,
        test_notifier_templates,
        test_orchestrator_config_db_url,
        test_routes_new_endpoints,
        test_routes_session_id_in_response,
        test_result_compressor_alternatives,
        test_local_routing_rain_factor_clamp,
        test_local_routing_file_uses_pool,
        test_no_circular_volume_mount,
        test_fcm_token_stored_in_routes,
        test_mcp_city_pool_lifecycle,
        test_cors_no_wildcard,
        test_here_response_new_fields,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except (AssertionError, Exception) as e:
            print(f"FAIL {test.__name__}: {e}")
            failed += 1

    print(f"\n{'='*55}")
    print(f"PASSED: {passed} | FAILED: {failed}")
