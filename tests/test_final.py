"""
tests/test_final.py — Son kontrol testleri

- satellite server: 4 tool tanimli mi
- satellite stac_client: bbox validasyon, NDVI yorum fonksiyonu
- docker-compose: mcp_intel DATABASE_URL, redis healthcheck
- profile_manager: 24h duplicate check importu
- mcp_client: redis try/except sarmalama
- .env.example: zorunlu keyler mevcut mu
"""
import os, sys
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_satellite_server_has_all_tools():
    path = os.path.join(BASE, "services", "mcp_satellite", "server.py")
    with open(path, encoding="utf-8") as f:
        c = f.read()
    for tool in ["search_satellite_imagery", "calculate_ndvi", "calculate_evi", "get_vegetation_report"]:
        assert f"def {tool}" in c, f"Eksik tool: {tool}"
    print("OK satellite 4 tool")

def test_satellite_bbox_validation():
    sys.path.insert(0, os.path.join(BASE, "services", "mcp_satellite"))
    try:
        from tools.stac_client import _validate_bbox
        ok, _ = _validate_bbox(28.0, 41.0, 29.0, 42.0)
        assert ok, "Gecerli bbox reddedildi"
        ok, msg = _validate_bbox(29.0, 41.0, 28.0, 42.0)
        assert not ok, "Gecersiz bbox kabul edildi"
        ok, msg = _validate_bbox(28.0, 41.0, 31.0, 44.0)   # 3x3 derece > 2x2 limiti
        assert not ok, "Cok buyuk alan kabul edildi"
        print("OK bbox validasyon")
    except ImportError as e:
        import pytest; pytest.skip(str(e))

def test_ndvi_interpretation():
    sys.path.insert(0, os.path.join(BASE, "services", "mcp_satellite"))
    try:
        from tools.stac_client import _interpret_ndvi
        assert _interpret_ndvi(0.7)["seviye"] == "COK YOGUN" or "YOGUN" in _interpret_ndvi(0.7)["seviye"]
        assert _interpret_ndvi(-0.1)["seviye"] == "BITKISIZ" or "BİTKİSİZ" in _interpret_ndvi(-0.1)["seviye"]
        print("OK NDVI yorum fonksiyonu")
    except ImportError as e:
        import pytest; pytest.skip(str(e))

def test_docker_compose_mcp_intel_db():
    path = os.path.join(BASE, "docker-compose.yml")
    with open(path, encoding="utf-8", errors="replace") as f:
        c = f.read()
    # mcp_intel blogu DATABASE_URL icermeli
    assert "DATABASE_URL" in c, "docker-compose mcp_intel DATABASE_URL eksik"
    # Redis healthcheck olmali
    assert "redis-cli" in c, "Redis healthcheck eksik"
    print("OK docker-compose mcp_intel + redis healthcheck")

def test_profile_manager_24h_check():
    path = os.path.join(BASE, "services", "orchestrator", "profile_manager.py")
    with open(path, encoding="utf-8") as f:
        c = f.read()
    assert "timedelta" in c, "profile_manager 24h kontrolu icin timedelta kullanmali"
    assert "cutoff" in c, "24h cutoff hesabi yok"
    assert "basit kontrol" not in c.lower(), "Eski TODO yorumu hala var"
    print("OK profile_manager 24h duplicate check")

def test_mcp_client_redis_try_except():
    path = os.path.join(BASE, "services", "orchestrator", "core", "mcp_client.py")
    with open(path, encoding="utf-8") as f:
        c = f.read()
    assert "except Exception as redis_err" in c, "mcp_client.py polyline proxy try/except eksik"
    print("OK mcp_client redis try/except")

def test_env_example_exists_and_complete():
    path = os.path.join(BASE, ".env.example")
    assert os.path.exists(path), ".env.example olusturulmamis"
    with open(path, encoding="utf-8", errors="replace") as f:
        c = f.read()
    for key in ["ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "POSTGRES_USER", "HERE_API_KEY", "OPENWEATHER_API_KEY"]:
        assert key in c, f".env.example icinde {key} eksik"
    print("OK .env.example tam")

if __name__ == "__main__":
    tests = [
        test_satellite_server_has_all_tools,
        test_satellite_bbox_validation,
        test_ndvi_interpretation,
        test_docker_compose_mcp_intel_db,
        test_profile_manager_24h_check,
        test_mcp_client_redis_try_except,
        test_env_example_exists_and_complete,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t(); passed += 1
        except Exception as e:
            print(f"FAIL {t.__name__}: {e}"); failed += 1
    print(f"\n{'='*50}\nPASSED: {passed} | FAILED: {failed}")
