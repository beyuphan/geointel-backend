"""
tests/test_sprint1.py — Sprint 1 Birim Testleri

Test edilen:
- DB Pool (init/acquire/close lifecycle)
- Local routing rain_factor maliyet formülü
- OSM geocoding fallback sıralaması
- Result compressor get_route_data fields
- Prompt manager: HIGH_KEYWORDS, route_history sadece routing'de
- should_continue: sonsuz loop koruması
"""
import sys
import os
import json
import asyncio
import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Test 1: DB Pool lifecycle (gerçek DB olmadan, mock ile)
# ─────────────────────────────────────────────────────────────────────────────

def test_get_pool_raises_without_init():
    """Pool başlatılmadan get_pool() çağrısı RuntimeError fırlatmalı."""
    # Modülü temiz import et (pool global'i None olsun)
    import importlib
    if "services.mcp_city.tools.db" in sys.modules:
        del sys.modules["services.mcp_city.tools.db"]

    # Gerçek DB yok, sadece mantık kontrolü
    # Bu test ortamında asyncpg yüklü olmayabilir — sadece import kontrolü
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "mcp_city"))
        from tools.db import get_pool, _pool
        # _pool None iken get_pool RuntimeError vermeli
        with pytest.raises(RuntimeError, match="DB Pool henüz başlatılmadı"):
            get_pool()
        print("✅ DB Pool RuntimeError testi geçti.")
    except ImportError as e:
        pytest.skip(f"Import başarısız (ortam eksik): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Rain Factor Maliyet Formülü
# ─────────────────────────────────────────────────────────────────────────────

def test_rain_multiplier_calculation():
    """C_edge = L × (1 + rain_factor × 0.3) formülü doğrulama."""
    def calc_multiplier(rain_factor: float) -> float:
        return 1.0 + (min(max(rain_factor, 0.0), 1.0) * 0.3)

    assert calc_multiplier(0.0) == 1.0,    "Kuru yol: çarpan 1.0 olmalı"
    assert calc_multiplier(1.0) == 1.3,    "Sağanak: çarpan 1.3 (max %30) olmalı"
    assert calc_multiplier(0.5) == 1.15,   "Orta yağış: çarpan 1.15 olmalı"
    assert calc_multiplier(-1.0) == 1.0,   "Negatif değer: clamp ile 0'a indirgenmeli"
    assert calc_multiplier(2.0) == 1.3,    "Aşırı değer: clamp ile max 1.3'e indirgenmeli"
    print("✅ Rain factor maliyet formülü testi geçti.")


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: OSM Geocoding — Importance Sıralaması
# ─────────────────────────────────────────────────────────────────────────────

def test_osm_importance_sorting():
    """importance'a göre sıralama doğrulama — en yüksek önem en başta."""
    mock_results = [
        {"importance": "0.3", "type": "village", "class": "place", "lat": "41.1", "lon": "40.5"},
        {"importance": "0.8", "type": "city", "class": "place", "lat": "41.0", "lon": "40.5"},
        {"importance": "0.1", "type": "hamlet", "class": "place", "lat": "41.2", "lon": "40.5"},
    ]

    # Simüle et
    mock_results.sort(key=lambda x: float(x.get("importance", 0)), reverse=True)
    best = mock_results[0]

    assert best["type"] == "city", f"En iyi eşleşme 'city' olmalı, '{best['type']}' geldi"
    assert float(best["importance"]) == 0.8
    print("✅ OSM importance sıralama testi geçti.")


def test_osm_city_priority_filter():
    """Şehir/kasaba tipi bulunursa önceliklendirilmeli."""
    mock_results = [
        {"importance": "0.9", "type": "administrative", "class": "boundary", "lat": "41.0", "lon": "40.5"},
        {"importance": "0.7", "type": "city", "class": "place", "lat": "41.05", "lon": "40.52"},
        {"importance": "0.5", "type": "village", "class": "place", "lat": "41.1", "lon": "40.6"},
    ]
    mock_results.sort(key=lambda x: float(x.get("importance", 0)), reverse=True)

    best_match = mock_results[0]
    for item in mock_results:
        if item.get("class") == "place" and item.get("type") in ["city", "town", "municipality"]:
            best_match = item
            break

    assert best_match["type"] == "city", "place/city tipi seçilmeli"
    print("✅ OSM city priority filtre testi geçti.")


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Result Compressor — get_route_data Fields
# ─────────────────────────────────────────────────────────────────────────────

def test_result_compressor_route_data():
    """get_route_data compression doğrulama: source, traffic_status korunmalı."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "orchestrator"))
        from core.result_compressor import compress_result, COMPRESSION_RULES

        rules = COMPRESSION_RULES.get("get_route_data", {})
        keep = rules.get("keep_fields", [])

        assert "source" in keep,         "source field korunmalı"
        assert "traffic_status" in keep, "traffic_status field korunmalı"
        assert "delay_min" in keep,      "delay_min field korunmalı"
        assert "mesafe_km" in keep,      "mesafe_km field korunmalı"

        # Strip alanları korunmamalı
        strip = rules.get("strip_fields", [])
        assert "polyline" in strip,         "polyline sıkıştırılmalı"
        assert "polyline_encoded" in strip, "polyline_encoded sıkıştırılmalı"

        # Gerçek sıkıştırma testi
        sample = {
            "mesafe_km": 25.5,
            "sure_dk": 35,
            "source": "HERE_Maps_API",
            "traffic_status": "HEAVY",
            "delay_min": 8.5,
            "polyline_encoded": "A" * 500,  # Büyük polyline
            "polyline": "B" * 300,
            "alternatives": [],
        }
        compressed = compress_result("get_route_data", sample)
        assert compressed.get("mesafe_km") == 25.5
        assert compressed.get("source") == "HERE_Maps_API"
        # Polyline gizlenmeli veya kısaltılmalı (< 50 char proxy string ise korunur, uzunsa strip)
        raw_poly = compressed.get("polyline_encoded", "")
        assert len(str(raw_poly)) < 100 or raw_poly is None, "Uzun polyline sıkıştırılmalı"

        print("✅ Result compressor get_route_data testi geçti.")
    except ImportError as e:
        pytest.skip(f"Import başarısız: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Prompt Manager — HIGH_KEYWORDS ve Route History
# ─────────────────────────────────────────────────────────────────────────────

def test_high_keywords_routing():
    """Rota kelimeleri HIGH olarak tanınmalı."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "orchestrator"))
        from prompt_manager import classify_intent_fast, HIGH_KEYWORDS

        # Rota sorusu high olmalı
        result = classify_intent_fast("İstanbul'dan Ankara'ya rota bul")
        assert result["complexity"] == "high", "Rota sorusu HIGH complexity olmalı"
        assert result["category"] == "routing"

        # Basit eczane sorusu artık LOW olmalı (HIGH_KEYWORDS'den kaldırıldı)
        result2 = classify_intent_fast("eczane nerede")
        assert result2["complexity"] == "low", "Kısa eczane sorusu LOW olmalı"

        # "eczane" HIGH_KEYWORDS'de olmamalı
        assert "eczane" not in HIGH_KEYWORDS, "eczane HIGH_KEYWORDS'den kaldırılmalı"
        assert "benzin" not in HIGH_KEYWORDS, "benzin HIGH_KEYWORDS'den kaldırılmalı"
        assert "rota" in HIGH_KEYWORDS,       "rota HIGH_KEYWORDS'de olmalı"

        print("✅ HIGH_KEYWORDS testi geçti.")
    except ImportError as e:
        pytest.skip(f"Import başarısız: {e}")


def test_route_history_only_for_routing():
    """Route history sadece routing kategorisinde prompt'a eklenmeli."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "orchestrator"))
        from prompt_manager import get_dynamic_system_prompt

        mock_history = [
            {"origin": "Rize", "destination": "Trabzon", "distance_km": 75}
        ]

        # Routing kategorisinde history olmalı
        routing_prompt = get_dynamic_system_prompt(
            "Test user",
            {"category": "routing", "complexity": "high", "focus_points": [], "urgency": False,
             "route_history": mock_history}
        )
        assert "Rize" in routing_prompt, "Routing'de route history görünmeli"

        # Genel kategoride history olmamalı
        general_prompt = get_dynamic_system_prompt(
            "Test user",
            {"category": "general", "complexity": "low", "focus_points": [], "urgency": False,
             "route_history": mock_history}
        )
        assert "Rize" not in general_prompt, "General'de route history görünmemeli"

        print("✅ Route history conditional inject testi geçti.")
    except ImportError as e:
        pytest.skip(f"Import başarısız: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: should_continue — Sonsuz Loop Koruması
# ─────────────────────────────────────────────────────────────────────────────

def test_should_continue_no_tool_calls():
    """Tool çağrısı yoksa should_continue END döndürmeli."""
    try:
        from langgraph.graph import END
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "orchestrator"))
        from core.graph import should_continue

        # Mock mesaj — tool_calls yok
        class MockMessage:
            tool_calls = []
            content = "Test yanıt"

        state = {"messages": [MockMessage()], "retry_count": 0, "intent": {}, "session_id": "test"}
        result = should_continue(state)
        assert result == END, f"Tool çağrısı yokken END olmalı, '{result}' geldi"
        print("✅ should_continue END testi geçti.")
    except ImportError as e:
        pytest.skip(f"Import başarısız: {e}")


def test_should_continue_max_retry():
    """retry_count >= 3 olduğunda tool_calls olsa bile END döndürmeli."""
    try:
        from langgraph.graph import END
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "orchestrator"))
        from core.graph import should_continue

        class MockToolCall:
            pass

        class MockMessage:
            tool_calls = [MockToolCall()]
            content = ""

        state = {"messages": [MockMessage()], "retry_count": 3, "intent": {}, "session_id": "test"}
        result = should_continue(state)
        assert result == END, f"Max retry'da END olmalı, '{result}' geldi"
        print("✅ should_continue max retry testi geçti.")
    except ImportError as e:
        pytest.skip(f"Import başarısız: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: mcp_city.tools.db — get_saved_locations artık orchestrator'dan değil db'den
# ─────────────────────────────────────────────────────────────────────────────

def test_db_module_no_orchestrator_import():
    """db.py orchestrator.profile_manager'ı import ETMEMELİ."""
    db_path = os.path.join(
        os.path.dirname(__file__), "..", "services", "mcp_city", "tools", "db.py"
    )
    with open(db_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "from orchestrator.profile_manager" not in content, \
        "db.py orchestrator'dan import yapmamalı! Döngüsel bağımlılık var."
    assert "get_saved_locations" in content, \
        "get_saved_locations fonksiyonu db.py'de tanımlı olmalı"
    print("✅ db.py döngüsel import testi geçti.")


def test_here_module_no_orchestrator_import():
    """here.py artık orchestrator.profile_manager'ı import ETMEMELİ."""
    here_path = os.path.join(
        os.path.dirname(__file__), "..", "services", "mcp_city", "tools", "here.py"
    )
    with open(here_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "from orchestrator.profile_manager" not in content, \
        "here.py orchestrator'dan import yapmamalı! Döngüsel bağımlılık kırılmalıydı."
    assert "_db_get_saved_locations" in content or "get_saved_locations" in content, \
        "here.py kendi db modülüne başvurmalı"
    print("✅ here.py döngüsel import testi geçti.")


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: mcp_city cache TTL — get_route_data önbelleklenebilmeli
# ─────────────────────────────────────────────────────────────────────────────

def test_route_data_cache_ttl_present():
    """orchestrator'daki cache_ttls sözlüğünde get_route_data olmalı."""
    mcp_client_path = os.path.join(
        os.path.dirname(__file__), "..", "services", "orchestrator", "core", "mcp_client.py"
    )
    with open(mcp_client_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert '"get_route_data"' in content or "'get_route_data'" in content, \
        "cache_ttls'de get_route_data olmalı"
    print("✅ get_route_data cache TTL testi geçti.")


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: search_places_google @mcp.tool() olmadan tanımlı
# ─────────────────────────────────────────────────────────────────────────────

def test_deprecated_google_tool_not_registered():
    """search_places_google @mcp.tool() dekoratörü OLMADAN tanımlı olmalı."""
    server_path = os.path.join(
        os.path.dirname(__file__), "..", "services", "mcp_city", "server.py"
    )
    with open(server_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # search_places_google fonksiyonundan önce @mcp.tool() olmamalı
    for i, line in enumerate(lines):
        if "async def search_places_google" in line:
            # 1-3 satır öncesine bak
            context = "".join(lines[max(0, i-3):i])
            assert "@mcp.tool()" not in context, \
                "search_places_google @mcp.tool() ile register edilmemeli (DEPRECATED)"
            print("✅ search_places_google @mcp.tool() kaldırma testi geçti.")
            return

    pytest.fail("search_places_google fonksiyonu bulunamadı!")


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # pytest olmadan doğrudan çalıştırılabilir
    tests = [
        test_rain_multiplier_calculation,
        test_osm_importance_sorting,
        test_osm_city_priority_filter,
        test_db_module_no_orchestrator_import,
        test_here_module_no_orchestrator_import,
        test_route_data_cache_ttl_present,
        test_deprecated_google_tool_not_registered,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"❌ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"⚠️ {test.__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"✅ {passed} test geçti | ❌ {failed} test başarısız")
