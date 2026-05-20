"""
tests/test_new_features.py — Yeni Özellik Testleri

Kapsam:
1. Günlük Plan intent sınıflandırması (yeni anahtar kelimeler)
2. Prompt'ta doğru MCP tool isimleri (event + day_plan kategorileri)
3. Koordinat regex filtresi (waypoint parsing)
4. Yakıt durağı rota filtresi (distance_along_route_km <= total_km)
5. Mola slotu hesaplama (±50km tolerans, aralık mantığı)
6. Hava analizi — temiz hava bildirimi (info severity eklenmesi)
7. Gün planı action card üretimi
8. Sınıflandırıcı öncelik sırası (day_plan routing'den önce)
"""
import sys
import os
import re
import pytest

ROOT      = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ORCH_PATH = os.path.join(ROOT, "services", "orchestrator")
for p in [ORCH_PATH]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 1: GÜNLÜK PLAN INTENT SINIFLANDIRMASI
# ═══════════════════════════════════════════════════════════════════════════

class TestDayPlanClassification:
    """Yeni day_plan anahtar kelimeleri doğru sınıflandırılıyor mu?"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from prompt_manager import classify_intent
        self.classify = classify_intent

    def test_gunluk_plan_keyword(self):
        result = self.classify("Samsun için günlük plan yap")
        assert result["category"] == "day_plan"

    def test_hafta_sonu_plan_keyword(self):
        result = self.classify("Bu hafta sonu plan yapmak istiyorum")
        assert result["category"] == "day_plan"

    def test_haftasonu_plan_no_space(self):
        result = self.classify("Haftasonu plan arıyorum")
        assert result["category"] == "day_plan"

    def test_icin_plan_keyword(self):
        result = self.classify("İstanbul için plan yap lütfen")
        assert result["category"] == "day_plan"

    def test_plan_yap_keyword(self):
        result = self.classify("Bana güzel bir plan yap")
        assert result["category"] == "day_plan"

    def test_bugün_ne_yapayim_keyword(self):
        result = self.classify("Bugün ne yapayım")
        assert result["category"] == "day_plan"

    def test_bana_bir_seyler_oner_keyword(self):
        result = self.classify("Bana bir şeyler öner, canım sıkılıyor")
        assert result["category"] == "day_plan"

    def test_gun_planla_keyword(self):
        result = self.classify("Günümü planla")
        assert result["category"] == "day_plan"

    def test_ne_yapabilirim_keyword(self):
        result = self.classify("Ankara'da ne yapabilirim bugün")
        assert result["category"] == "day_plan"

    def test_day_plan_is_high_complexity(self):
        """day_plan her zaman high complexity olmalı."""
        result = self.classify("Günlük plan")
        assert result["complexity"] == "high"

    def test_day_plan_before_routing(self):
        """'için plan' hem routing hem day_plan tetikleyebilir — day_plan kazanmalı."""
        result = self.classify("Samsun için plan yap, araba ile gideceğim")
        assert result["category"] == "day_plan"

    def test_routing_not_misclassified_as_day_plan(self):
        """Saf rota sorgusu day_plan olmamalı."""
        result = self.classify("İstanbul'dan Ankara'ya rota bul")
        assert result["category"] == "routing"


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 2: PROMPT'TA DOĞRU MCP TOOL İSİMLERİ
# ═══════════════════════════════════════════════════════════════════════════

class TestPromptToolNames:
    """INSTRUCTIONS sözlüğü doğru tool isimlerini içeriyor mu?"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from prompt_manager import INSTRUCTIONS
        self.instructions = INSTRUCTIONS

    def test_event_uses_get_city_events(self):
        """Etkinlik modu gerçek tool adını kullanmalı."""
        assert "get_city_events" in self.instructions["event"]

    def test_event_uses_get_sports_events(self):
        """Spor modu gerçek tool adını kullanmalı."""
        assert "get_sports_events" in self.instructions["event"]

    def test_event_no_old_get_events(self):
        """Eski yanlış isim `get_events()` kullanılmamalı."""
        assert "get_events()" not in self.instructions["event"]

    def test_event_no_old_get_sports_matches(self):
        """Eski yanlış isim `get_sports_matches()` kullanılmamalı."""
        assert "get_sports_matches" not in self.instructions["event"]

    def test_day_plan_uses_get_city_events(self):
        """Gün planlama modu etkinlik için gerçek tool adını kullanmalı."""
        assert "get_city_events" in self.instructions["day_plan"]

    def test_day_plan_uses_get_sports_events(self):
        """Gün planlama modu spor için gerçek tool adını kullanmalı."""
        assert "get_sports_events" in self.instructions["day_plan"]

    def test_day_plan_no_get_weather_tool(self):
        """`get_weather()` MCP'de mevcut değil — day_plan onu referans etmemeli."""
        assert "get_weather()" not in self.instructions["day_plan"]

    def test_day_plan_has_search_hybrid_places(self):
        """Mekan önerisi için search_hybrid_places kullanılmalı."""
        assert "search_hybrid_places" in self.instructions["day_plan"]

    def test_routing_uses_get_route_data(self):
        """Rota modu doğru tool adını kullanmalı."""
        assert "get_route_data" in self.instructions["routing"]

    def test_fuel_uses_get_fuel_prices(self):
        """Yakıt modu doğru tool adını kullanmalı."""
        assert "get_fuel_prices" in self.instructions["fuel"]

    def test_pharmacy_uses_get_pharmacies(self):
        """Eczane modu doğru tool adını kullanmalı."""
        assert "get_pharmacies" in self.instructions["pharmacy"]


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 3: KOORDİNAT REGEX FİLTRESİ
# ═══════════════════════════════════════════════════════════════════════════

class TestCoordinateRegex:
    """Waypoint koordinat filtresi şehir isimlerini eliyor mu?"""

    COORD_RE = re.compile(r'^-?\d+\.?\d*,-?\d+\.?\d*$')

    def _is_coord(self, s: str) -> bool:
        return bool(self.COORD_RE.match(s.strip()))

    def test_valid_coordinate_passes(self):
        assert self._is_coord("41.0082,28.9784")

    def test_valid_coordinate_with_high_precision(self):
        assert self._is_coord("39.925533,32.866287")

    def test_valid_negative_lat(self):
        assert self._is_coord("-33.8688,151.2093")

    def test_valid_integer_coordinate(self):
        assert self._is_coord("41,28")

    def test_city_name_rejected(self):
        assert not self._is_coord("İstanbul")

    def test_city_name_with_country_rejected(self):
        assert not self._is_coord("Ankara, Türkiye")

    def test_partial_coordinate_rejected(self):
        assert not self._is_coord("41.0082")

    def test_address_rejected(self):
        assert not self._is_coord("Bağcılar, İstanbul")

    def test_coordinate_with_spaces_stripped(self):
        assert self._is_coord(" 41.0082,28.9784 ")

    def test_empty_string_rejected(self):
        assert not self._is_coord("")

    def test_filter_mixed_waypoints(self):
        """Şehir + koordinat karışık listeden sadece koordinatlar kalmalı."""
        waypoints = ["İstanbul", "41.0082,28.9784", "Ankara", "39.9334,32.8597"]
        filtered = [w for w in waypoints if self._is_coord(w)]
        assert filtered == ["41.0082,28.9784", "39.9334,32.8597"]


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 4: YAKIT DURAĞ FİLTRESİ
# ═══════════════════════════════════════════════════════════════════════════

class TestFuelStopFilter:
    """Rota uzunluğunu aşan yakıt durakları filtreleniyor mu?"""

    @staticmethod
    def _filter_fuel_stops(places: list, total_km: float) -> list:
        return [
            m for m in places
            if (m.get("distance_along_route_km") or 0) <= total_km
        ]

    def test_stop_within_range_passes(self):
        places = [{"name": "Shell", "distance_along_route_km": 300}]
        result = self._filter_fuel_stops(places, 677)
        assert len(result) == 1

    def test_stop_at_exact_total_km_passes(self):
        places = [{"name": "Opet", "distance_along_route_km": 677}]
        result = self._filter_fuel_stops(places, 677)
        assert len(result) == 1

    def test_stop_beyond_total_km_filtered(self):
        places = [{"name": "BP", "distance_along_route_km": 700}]
        result = self._filter_fuel_stops(places, 677)
        assert len(result) == 0

    def test_stop_with_none_distance_passes(self):
        """distance_along_route_km=None olan durak (0 kabul edilir) geçmeli."""
        places = [{"name": "Türk Petrol", "distance_along_route_km": None}]
        result = self._filter_fuel_stops(places, 677)
        assert len(result) == 1

    def test_mixed_stops(self):
        places = [
            {"name": "Shell", "distance_along_route_km": 200},
            {"name": "BP", "distance_along_route_km": 700},   # aşıyor
            {"name": "Opet", "distance_along_route_km": 677},
        ]
        result = self._filter_fuel_stops(places, 677)
        names = [p["name"] for p in result]
        assert "Shell" in names
        assert "Opet" in names
        assert "BP" not in names

    def test_empty_list(self):
        result = self._filter_fuel_stops([], 677)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 5: MOLA SLOTU HESAPLAMA
# ═══════════════════════════════════════════════════════════════════════════

class TestBreakSlotCalculation:
    """Mola slotları doğru aralıklarda, mevcut duraklardan ±50km uzakta üretiliyor mu?"""

    @staticmethod
    def _compute_break_slots(break_interval_km: float, total_km: float,
                              selected_kms: list) -> list:
        """routes.py add_stops_to_trip'deki aynı mantık."""
        break_slots = []
        curr = break_interval_km
        while curr < total_km - 30:
            if not any(abs(skm - curr) <= 50 for skm in selected_kms):
                break_slots.append(curr)
            curr += break_interval_km
        return break_slots

    def test_basic_slot_generation(self):
        """500km rotada 160km aralıkla slot'lar üretilmeli.
        Döngü: while curr < 500-30=470 → 160 ve 320 eklenir, 480 >= 470 durur."""
        slots = self._compute_break_slots(160, 500, [])
        assert slots == [160, 320]
        assert 480 not in slots

    def test_stop_at_500km_not_added(self):
        """470+ km'de olan slot total_km-30 kontrolünden geçmemeli."""
        slots = self._compute_break_slots(160, 500, [])
        assert 480 not in slots

    def test_slot_skipped_near_existing_stop(self):
        """Seçili durağa ±50km yakın slot atlanmalı."""
        # Kullanıcı 300km'de bir yemek yeri seçti; 320km slotu atlanmalı
        slots = self._compute_break_slots(160, 700, [300])
        assert 160 in slots      # 300'den 140km uzak — eklenmeli
        assert 320 not in slots  # 300'e sadece 20km — atlanmalı (|320-300|=20 <= 50)
        assert 480 in slots      # 300'den 180km uzak — eklenmeli

    def test_slot_not_skipped_when_far_enough(self):
        """Seçili durağa 51km+ uzak slot atlanmamalı."""
        slots = self._compute_break_slots(160, 700, [100])
        # |160-100| = 60 > 50, so 160 should be included
        assert 160 in slots

    def test_no_slots_when_route_too_short(self):
        """Kısa rotada (total < break_interval) mola slotu üretilmemeli."""
        slots = self._compute_break_slots(160, 100, [])
        assert slots == []

    def test_multiple_existing_stops_skip_multiple_slots(self):
        """Birden fazla mevcut durak, birden fazla yakın slotu atlıyor mu?"""
        slots = self._compute_break_slots(100, 600, [100, 300])
        # slot 100: |100-100|=0 <= 50 → skip
        # slot 200: |200-100|=100>50, |200-300|=100>50 → include
        # slot 300: |300-300|=0 <= 50 → skip
        # slot 400: |400-300|=100>50 → include
        # slot 500: 500 < 570 (600-30), |500-300|=200>50 → include
        assert 100 not in slots
        assert 200 in slots
        assert 300 not in slots
        assert 400 in slots
        assert 500 in slots


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 6: HAVA DURUMU TEMİZ HAVA BİLDİRİMİ
# ═══════════════════════════════════════════════════════════════════════════

class TestWeatherClearEntry:
    """Hava analizi tamamlandığında, risk yoksa 'info' severity ekleniyor mu?"""

    @staticmethod
    def _apply_weather_clear_logic(weather_analyzed: bool, weather_warnings: list) -> list:
        """routes.py'deki aynı mantık."""
        if weather_analyzed and not weather_warnings:
            weather_warnings.append({
                "location": "Tüm Rota",
                "condition": "Açık",
                "severity": "info",
                "message": "☀️ Rota boyunca hava açık, güvenli yolculuklar!",
            })
        return weather_warnings

    def test_clear_weather_entry_added_when_no_warnings(self):
        result = self._apply_weather_clear_logic(True, [])
        assert len(result) == 1
        assert result[0]["severity"] == "info"
        assert result[0]["condition"] == "Açık"

    def test_clear_weather_entry_has_correct_location(self):
        result = self._apply_weather_clear_logic(True, [])
        assert result[0]["location"] == "Tüm Rota"

    def test_no_clear_entry_when_not_analyzed(self):
        """Hava analizi yapılmadıysa ek girdi eklenmemeli."""
        result = self._apply_weather_clear_logic(False, [])
        assert result == []

    def test_no_clear_entry_when_warnings_exist(self):
        """Uyarı zaten varsa temiz hava girdisi eklenmemeli."""
        existing = [{"severity": "warning", "location": "Bolu", "condition": "kar"}]
        result = self._apply_weather_clear_logic(True, existing)
        assert len(result) == 1  # Mevcut uyarı kaldı, yeni eklenmedi
        assert result[0]["severity"] == "warning"

    def test_clear_entry_message_contains_sun_emoji(self):
        result = self._apply_weather_clear_logic(True, [])
        assert "☀️" in result[0]["message"]

    def test_severe_weather_not_cleared(self):
        """Kritik uyarı varken temiz hava girdisi eklenmemeli."""
        existing = [{"severity": "critical", "location": "Ankara", "condition": "fırtına"}]
        result = self._apply_weather_clear_logic(True, existing)
        info_entries = [w for w in result if w.get("severity") == "info"]
        assert len(info_entries) == 0


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 7: GÜN PLANI ACTION CARD'LARI
# ═══════════════════════════════════════════════════════════════════════════

class TestDayPlanActionCards:
    """day_plan kategorisi doğru action card'ları üretiyor mu?"""

    @pytest.fixture(autouse=True)
    def setup(self):
        try:
            from api.routes import _build_action_cards
            self.build_cards = _build_action_cards
        except ImportError:
            pytest.skip("routes import edilemedi")

    def test_day_plan_generates_route_card(self):
        intent = {"category": "day_plan"}
        visual  = {"polyline": None, "markers": []}
        cards = self.build_cards(intent, visual)
        actions = [c["action"] for c in cards]
        assert "Planladığım yerler için optimum rota oluştur" in actions

    def test_day_plan_generates_more_suggestions_card(self):
        intent = {"category": "day_plan"}
        visual  = {"polyline": None, "markers": []}
        cards = self.build_cards(intent, visual)
        actions = [c["action"] for c in cards]
        assert "Başka aktiviteler de öner" in actions

    def test_day_plan_no_navigation_card_without_polyline(self):
        """Polyline yoksa navigasyon card'ı olmamalı."""
        intent = {"category": "day_plan"}
        visual  = {"polyline": None, "markers": []}
        cards = self.build_cards(intent, visual)
        actions = [c["action"] for c in cards]
        assert "ui:start_navigation" not in actions

    def test_routing_with_poly_overrides_day_plan_cards(self):
        """Rota çizilmişse navigasyon card'ı öne çıkmalı."""
        intent = {"category": "routing"}
        visual  = {"polyline": "some_encoded_polyline", "markers": []}
        cards = self.build_cards(intent, visual)
        actions = [c["action"] for c in cards]
        assert "ui:start_navigation" in actions


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 8: SINIFLANDIRICI ÖNCELİK SIRASI
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifierPriority:
    """Sınıflandırıcı öncelik sırası doğru mu?"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from prompt_manager import classify_intent
        self.classify = classify_intent

    def test_day_plan_before_routing_priority(self):
        """day_plan anahtar kelimesi routing içerikli cümlede bile kazanmalı."""
        result = self.classify("Ankara için günlük plan yap, mesafe önemli değil")
        assert result["category"] == "day_plan"

    def test_event_before_places(self):
        """Etkinlik sorusu places'a düşmemeli."""
        result = self.classify("Bu hafta konser var mı, Ankara'da kafe de bulalım")
        assert result["category"] == "event"

    def test_routing_before_places(self):
        """Rota + mekan → routing kazanmalı."""
        result = self.classify("İstanbul'a giderken yemek yeri ara")
        assert result["category"] == "routing"

    def test_routing_before_fuel(self):
        """Rota + yakıt → routing kazanmalı."""
        result = self.classify("Yolculuk sırasında benzin nerede ucuz")
        assert result["category"] == "routing"

    def test_urgency_pharmacy(self):
        """Acil eczane hem urgency hem pharmacy olmalı."""
        result = self.classify("Acil nöbetçi eczane lazım")
        assert result["category"] == "pharmacy"
        assert result["urgency"] is True

    def test_general_fallback_for_unknown(self):
        """Tanımsız kelimeler general'a düşmeli."""
        result = self.classify("Merhaba nasılsın bugün ne yedim")
        assert result["category"] == "general"


# ═══════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
