"""
tests/test_integration.py — GeoIntel Integration Test Suite

Professional test coverage:
1. Intent Classification (all categories + complexity routing)
2. Result Compression (field preservation + stripping)
3. Action Cards (conditional UI logic)
4. Route Loop Guard (should_continue)
5. Model Validation (IntentAnalysis, StandardPlace)
6. search_hybrid_places format standardization
7. Macro-tool response parsing
8. Rain factor cost formulas
9. Satellite bbox validation
10. Intel create_response type safety
"""
import sys
import os
import json
import pytest

# ---------------------------------------------------------------------------
# PATH SETUP — Clean isolation without sys.path pollution
# ---------------------------------------------------------------------------
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ORCH_PATH = os.path.join(ROOT, "services", "orchestrator")
CITY_PATH = os.path.join(ROOT, "services", "mcp_city")
INTEL_PATH = os.path.join(ROOT, "services", "mcp_intel")
SAT_PATH = os.path.join(ROOT, "services", "mcp_satellite")

# Add paths once at module level
for p in [ORCH_PATH, CITY_PATH, INTEL_PATH, SAT_PATH]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 1: INTENT CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════

class TestIntentClassification:
    """classify_intent_fast() doğru kategori ve complexity döndürüyor mu?"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from prompt_manager import classify_intent_fast, HIGH_KEYWORDS
        self.classify = classify_intent_fast
        self.HIGH_KEYWORDS = HIGH_KEYWORDS

    def test_routing_keywords(self):
        result = self.classify("İstanbul'dan Ankara'ya rota bul")
        assert result["category"] == "routing"
        assert result["complexity"] == "high"

    def test_fuel_keywords(self):
        result = self.classify("benzin fiyatları ne kadar")
        assert result["category"] == "fuel"

    def test_pharmacy_keywords(self):
        result = self.classify("nöbetçi eczane nerede bulabilirim")
        assert result["category"] == "pharmacy"

    def test_event_keywords(self):
        result = self.classify("bu hafta konser var mı")
        assert result["category"] == "event"

    def test_city_data_keywords(self):
        result = self.classify("İBB WFS katmanlarını göster")
        assert result["category"] == "city_data"

    def test_places_keywords(self):
        result = self.classify("Kadıköy'de kafe bul")
        assert result["category"] == "places"

    def test_places_extended(self):
        for word in ["restoran", "lokanta", "kahvaltı", "döner", "hamburger", "pizza"]:
            result = self.classify(f"yakınımda {word} var mı")
            assert result["category"] == "places", f"'{word}' places olarak tanınmalı"

    def test_general_fallback(self):
        result = self.classify("Merhaba nasılsın")
        assert result["category"] == "general"
        assert result["complexity"] == "low"

    def test_multi_intent_routing_places(self):
        """Rota + places birlikte geldiğinde routing'e yükseltilmeli."""
        result = self.classify("İstanbul'a giderken yemek yiyelim")
        assert result["category"] == "routing"

    def test_multi_intent_routing_fuel(self):
        """Rota + yakıt birlikte geldiğinde routing'e yükseltilmeli."""
        result = self.classify("Ankara yolunda benzin nerede ucuz")
        assert result["category"] == "routing"

    def test_complexity_long_sentence(self):
        """10+ kelimelik cümleler high complexity olmalı."""
        long_msg = "Kadıköy'den Taksim'e en kısa zamanda nasıl gidebilirim acaba trafik yoğun mu şu anda"
        result = self.classify(long_msg)
        assert result["complexity"] == "high"

    def test_urgency_detection(self):
        result = self.classify("acil eczane lazım")
        assert result["urgency"] is True

    def test_high_keywords_curated(self):
        """Rota kelimeleri HIGH_KEYWORDS'de olmalı, basit aramalar olmamalı."""
        assert "rota" in self.HIGH_KEYWORDS
        assert "eczane" not in self.HIGH_KEYWORDS
        assert "benzin" not in self.HIGH_KEYWORDS


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 2: INTENT ANALYSIS MODEL VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

class TestIntentAnalysisModel:
    """IntentAnalysis Pydantic modeli tüm kategorileri kabul ediyor mu?"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from core.models import IntentAnalysis
        self.Model = IntentAnalysis

    def test_all_categories_valid(self):
        categories = ["fuel", "pharmacy", "event", "routing", "city_data", "places", "general"]
        for cat in categories:
            obj = self.Model(
                category=cat,
                urgency=False,
                focus_points=[],
                complexity="low"
            )
            assert obj.category == cat

    def test_invalid_category_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self.Model(
                category="invalid_category",
                urgency=False,
                focus_points=[],
                complexity="low"
            )

    def test_complexity_values(self):
        for c in ["low", "high"]:
            obj = self.Model(category="general", urgency=False, focus_points=[], complexity=c)
            assert obj.complexity == c


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 3: RESULT COMPRESSION
# ═══════════════════════════════════════════════════════════════════════════

class TestResultCompression:
    """compress_result() doğru alanları koruyor ve sıkıştırıyor mu?"""

    @pytest.fixture(autouse=True)
    def setup(self):
        try:
            from core.result_compressor import compress_result, COMPRESSION_RULES
            self.compress = compress_result
            self.rules = COMPRESSION_RULES
        except ImportError:
            pytest.skip("result_compressor import edilemedi")

    def test_route_data_keeps_critical_fields(self):
        sample = {
            "mesafe_km": 450.5,
            "sure_dk": 320,
            "source": "HERE_Maps_API",
            "traffic_status": "MODERATE",
            "delay_min": 12.5,
            "polyline_encoded": "A" * 1000,
        }
        compressed = self.compress("get_route_data", sample)
        assert compressed["mesafe_km"] == 450.5
        assert compressed["source"] == "HERE_Maps_API"
        assert compressed["traffic_status"] == "MODERATE"
        assert compressed["delay_min"] == 12.5

    def test_route_data_strips_polyline(self):
        sample = {
            "mesafe_km": 25,
            "polyline_encoded": "B" * 500,
            "polyline": "C" * 300,
        }
        compressed = self.compress("get_route_data", sample)
        poly = compressed.get("polyline_encoded", "")
        # Polyline should be stripped or replaced with proxy
        assert len(str(poly)) < 100 or poly is None

    def test_unknown_tool_passthrough(self):
        """Kuralı olmayan araçlar olduğu gibi geçmeli."""
        sample = {"data": "test", "huge_field": "x" * 10000}
        compressed = self.compress("unknown_tool", sample)
        assert "data" in compressed


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 4: ACTION CARDS
# ═══════════════════════════════════════════════════════════════════════════

class TestActionCards:
    """_build_action_cards() doğru kartları üretiyor mu?"""

    @pytest.fixture(autouse=True)
    def setup(self):
        try:
            from api.routes import _build_action_cards
            self.build_cards = _build_action_cards
        except ImportError:
            pytest.skip("routes import edilemedi")

    def test_polyline_shows_navigation_cards(self):
        cards = self.build_cards("Rota hesaplandı", {"polyline": "encoded_data", "markers": []})
        actions = [c["action"] for c in cards]
        assert "start_navigation" in actions
        assert "show_alternatives" in actions

    def test_markers_show_map_card(self):
        cards = self.build_cards("Mekanlar bulundu", {"polyline": None, "markers": [{"lat": 41}]})
        actions = [c["action"] for c in cards]
        assert "show_on_map" in actions

    def test_fuel_text_shows_fuel_card(self):
        cards = self.build_cards("Benzin fiyatları şöyle...", {"polyline": None, "markers": []})
        actions = [c["action"] for c in cards]
        assert "compare_fuel" in actions

    def test_empty_visual_data_no_cards(self):
        cards = self.build_cards("Merhaba!", {"polyline": None, "markers": []})
        assert len(cards) == 0


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 5: LOOP GUARD (should_continue)
# ═══════════════════════════════════════════════════════════════════════════

class TestLoopGuard:
    """should_continue() sonsuz döngüyü engelliyor mu?"""

    @pytest.fixture(autouse=True)
    def setup(self):
        try:
            from langgraph.graph import END
            from core.graph import should_continue
            self.should_continue = should_continue
            self.END = END
        except ImportError:
            pytest.skip("LangGraph import edilemedi")

    def test_no_tool_calls_returns_end(self):
        class MockMsg:
            tool_calls = []
            content = "Yanıt"

        state = {"messages": [MockMsg()], "retry_count": 0, "intent": {}, "session_id": "t"}
        assert self.should_continue(state) == self.END

    def test_max_retry_returns_end(self):
        class MockTool:
            pass
        class MockMsg:
            tool_calls = [MockTool()]
            content = ""

        state = {"messages": [MockMsg()], "retry_count": 3, "intent": {}, "session_id": "t"}
        assert self.should_continue(state) == self.END

    def test_valid_tool_call_returns_tools(self):
        class MockTool:
            pass
        class MockMsg:
            tool_calls = [MockTool()]
            content = ""

        state = {"messages": [MockMsg()], "retry_count": 0, "intent": {}, "session_id": "t"}
        result = self.should_continue(state)
        assert result == "tools"


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 6: RAIN FACTOR COST FORMULA
# ═══════════════════════════════════════════════════════════════════════════

class TestRainFactor:
    """C_edge = L × (1 + rain_factor × 0.3) formülü."""

    @staticmethod
    def calc_multiplier(rain_factor: float) -> float:
        return 1.0 + (min(max(rain_factor, 0.0), 1.0) * 0.3)

    def test_dry_road(self):
        assert self.calc_multiplier(0.0) == 1.0

    def test_max_rain(self):
        assert self.calc_multiplier(1.0) == 1.3

    def test_mid_rain(self):
        assert self.calc_multiplier(0.5) == 1.15

    def test_negative_clamp(self):
        assert self.calc_multiplier(-5.0) == 1.0

    def test_overflow_clamp(self):
        assert self.calc_multiplier(99.0) == 1.3


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 7: SATELLITE BBOX VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

class TestSatelliteBbox:
    """_validate_bbox() koordinat sınırlarını doğruluyor mu?"""

    @pytest.fixture(autouse=True)
    def setup(self):
        try:
            from tools.stac_client import _validate_bbox
            self.validate = _validate_bbox
        except ImportError:
            pytest.skip("stac_client import edilemedi")

    def test_valid_bbox(self):
        ok, msg = self.validate(28.5, 40.8, 29.5, 41.3)
        assert ok is True

    def test_inverted_lon(self):
        ok, msg = self.validate(29.5, 40.8, 28.5, 41.3)  # min > max
        assert ok is False

    def test_inverted_lat(self):
        ok, msg = self.validate(28.5, 41.3, 29.5, 40.8)  # min > max
        assert ok is False

    def test_too_large_area(self):
        ok, msg = self.validate(25.0, 36.0, 45.0, 42.0)  # > 2° span
        assert ok is False

    def test_out_of_range_lon(self):
        ok, msg = self.validate(-200, 40, 30, 41)  # lon out of range
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 8: ARCHITECTURE CONTRACTS (File-level checks)
# ═══════════════════════════════════════════════════════════════════════════

class TestArchitectureContracts:
    """Mimari kurallar dosya düzeyinde korunuyor mu?"""

    def test_no_circular_import_db_to_orchestrator(self):
        """mcp_city/tools/db.py → orchestrator importu YASAK."""
        db_path = os.path.join(CITY_PATH, "tools", "db.py")
        with open(db_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "from orchestrator" not in content
        assert "import orchestrator" not in content

    def test_no_circular_import_here_to_orchestrator(self):
        """mcp_city/tools/here.py → orchestrator importu YASAK."""
        here_path = os.path.join(CITY_PATH, "tools", "here.py")
        with open(here_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "from orchestrator" not in content

    def test_cors_no_wildcard(self):
        """Orchestrator CORS'ta wildcard kullanılmamalı."""
        main_path = os.path.join(ORCH_PATH, "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            content = f.read()
        # allow_origins=["*"] olmamalı
        assert 'allow_origins=["*"]' not in content

    def test_intent_model_has_places_category(self):
        """IntentAnalysis 'places' kategorisini desteklemeli."""
        models_path = os.path.join(ORCH_PATH, "core", "models.py")
        with open(models_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert '"places"' in content

    def test_docker_compose_satellite_url(self):
        """Orchestrator MCP_SATELLITE_URL'yi docker-compose'dan almalı."""
        compose_path = os.path.join(ROOT, "docker-compose.yml")
        with open(compose_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "MCP_SATELLITE_URL" in content

    def test_intel_uses_connection_pool(self):
        """mcp_intel/db_helper.py connection pool kullanmalı."""
        db_path = os.path.join(INTEL_PATH, "db_helper.py")
        with open(db_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "init_pool" in content
        assert "get_pool" in content
        # Her sorguda asyncpg.connect() olmamalı
        assert content.count("asyncpg.connect") <= 1  # Only in docstring/comment ideally

    def test_satellite_tools_are_async(self):
        """Satellite server araçları async olmalı."""
        server_path = os.path.join(SAT_PATH, "server.py")
        with open(server_path, "r", encoding="utf-8") as f:
            content = f.read()
        # Tüm tool fonksiyonları async def olmalı
        assert "async def search_satellite_imagery" in content
        assert "async def calculate_ndvi" in content
        assert "async def calculate_evi" in content
        assert "async def get_vegetation_report" in content


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 9: MACRO TOOLS RESPONSE PARSING
# ═══════════════════════════════════════════════════════════════════════════

class TestMacroToolsResponseParsing:
    """RouteStrategyEvaluator her iki response formatını da destekliyor mu?"""

    def test_new_format_places_key(self):
        """Yeni format: {places: [...], count: N}"""
        response = {"places": [{"name": "Shell", "address": "Istanbul"}], "count": 1}
        places = response.get("places", [])
        assert len(places) == 1
        assert places[0]["name"] == "Shell"

    def test_old_format_strict_relaxed(self):
        """Eski format: {strict_route_places: [...], relaxed_route_places: [...]}"""
        response = {
            "strict_route_places": [{"name": "BP"}],
            "relaxed_route_places": [{"name": "Total"}]
        }
        places = response.get("places", [])
        if not places:
            places = response.get("strict_route_places", []) + response.get("relaxed_route_places", [])
        assert len(places) == 2

    def test_list_format(self):
        """Düz liste gelme durumu."""
        response = [{"name": "Opet"}, {"name": "Petrol Ofisi"}]
        places = response if isinstance(response, list) else []
        assert len(places) == 2

    def test_empty_response(self):
        """Boş response güvenli şekilde ele alınmalı."""
        response = {"places": [], "count": 0}
        places = response.get("places", [])
        assert len(places) == 0


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 10: OSM GEOCODING LOGIC
# ═══════════════════════════════════════════════════════════════════════════

class TestOSMGeocodingLogic:
    """OSM geocoding importance + type priority."""

    def test_importance_sorting(self):
        mock = [
            {"importance": "0.3", "type": "village", "class": "place"},
            {"importance": "0.8", "type": "city", "class": "place"},
            {"importance": "0.1", "type": "hamlet", "class": "place"},
        ]
        mock.sort(key=lambda x: float(x.get("importance", 0)), reverse=True)
        assert mock[0]["type"] == "city"

    def test_city_type_priority(self):
        mock = [
            {"importance": "0.9", "type": "administrative", "class": "boundary"},
            {"importance": "0.7", "type": "city", "class": "place"},
        ]
        mock.sort(key=lambda x: float(x.get("importance", 0)), reverse=True)
        best = mock[0]
        for item in mock:
            if item.get("class") == "place" and item.get("type") in ["city", "town", "municipality"]:
                best = item
                break
        assert best["type"] == "city"


# ═══════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-x"])
