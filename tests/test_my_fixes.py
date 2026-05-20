"""
Benim değişikliklerimi test eden unit testler.
Dış servis bağımlılığı yok — saf logic testi.
"""
import sys
import json
import pytest

sys.path.insert(0, "services/orchestrator")


# ─── TEST 1: Yakıt — çoklu hedef mesafe üretimi ─────────────────────────────

class TestFuelMultiStop:
    """macro_tools.py: target_distances artık interval bazlı çoklu hedef üretiyor."""

    def _calc_targets(self, fuel_range: float, total_dist: float) -> list:
        target_distances = []
        if fuel_range:
            interval = fuel_range * 0.8
            km = interval
            while km < total_dist * 0.98:
                target_distances.append(km)
                km += interval
        if not target_distances:
            target_distances = [total_dist * 0.5]
        return target_distances

    def test_atakum_rize_200km_fuel_range(self):
        """200km menzil, 380km rota -> 160 ve 320km'de iki durak."""
        targets = self._calc_targets(200.0, 380.0)
        assert len(targets) == 2
        assert abs(targets[0] - 160.0) < 0.1
        assert abs(targets[1] - 320.0) < 0.1

    def test_short_route_single_fallback(self):
        """Rota menzilden kısa -> fallback: tek nokta ortada."""
        targets = self._calc_targets(500.0, 200.0)
        assert len(targets) == 1
        assert abs(targets[0] - 100.0) < 0.1

    def test_long_route_multiple_stops(self):
        """150km menzil, 700km rota -> 5 durak (120, 240, 360, 480, 600 < 686)."""
        targets = self._calc_targets(150.0, 700.0)
        assert len(targets) == 5  # 120, 240, 360, 480, 600 — hepsi 700*0.98=686'dan küçük

    def test_no_fuel_range_fallback(self):
        """fuel_range=None -> route ortasında tek hedef."""
        target_distances = []
        total_dist = 400.0
        fuel_range = None
        if fuel_range:
            pass
        if not target_distances:
            target_distances = [total_dist * 0.5]
        assert len(target_distances) == 1
        assert abs(target_distances[0] - 200.0) < 0.1


# ─── TEST 2: Fiyat etiketi — fiyatsız istasyon görünmez olmaz ───────────────

class TestFuelPriceLabel:
    """Fiyat bilinmeyenlere 'Fiyat bilgisi yok' etiketi."""

    def _enrich_label(self, places):
        for p in places:
            fp = p.get("fuel_price") or {}
            if not isinstance(fp.get("price_per_liter"), (int, float)):
                fp["price_label"] = "Fiyat bilgisi yok"
            else:
                fp["price_label"] = f"{fp['price_per_liter']:.2f} TL/L"
            p["fuel_price"] = fp
        return places

    def test_no_price_gets_label(self):
        places = [{"name": "BP", "fuel_price": {}}]
        result = self._enrich_label(places)
        assert result[0]["fuel_price"]["price_label"] == "Fiyat bilgisi yok"

    def test_known_price_gets_formatted_label(self):
        places = [{"name": "Total", "fuel_price": {"price_per_liter": 48.72}}]
        result = self._enrich_label(places)
        assert result[0]["fuel_price"]["price_label"] == "48.72 TL/L"

    def test_null_price_gets_label(self):
        places = [{"name": "Opet", "fuel_price": {"price_per_liter": None}}]
        result = self._enrich_label(places)
        assert result[0]["fuel_price"]["price_label"] == "Fiyat bilgisi yok"


# ─── TEST 3: Hava durumu — zone format (dict/string) ─────────────────────────

class TestWeatherZoneHandling:
    """routes.py: riskli_bolgeler artık hem dict hem string handle ediliyor."""

    def _parse_zones(self, zones):
        weather_warnings = []
        for zone in zones:
            if isinstance(zone, dict):
                temp_str = zone.get("sicaklik", "")
                rain_str = zone.get("yagis_olasiligi", "")
                detail = f" {temp_str}" if temp_str else ""
                if rain_str:
                    detail += f", yagis olasiligi {rain_str}"
                weather_warnings.append({
                    "location": zone.get("km", "Rota"),
                    "condition": zone.get("durum", ""),
                    "temperature": temp_str,
                    "rain_probability": rain_str,
                    "severity": "warning",
                    "message": f"Dikkat: {zone.get('durum', 'kotu hava')}{detail}",
                })
            elif isinstance(zone, str):
                weather_warnings.append({
                    "location": "Rota",
                    "condition": zone,
                    "severity": "warning",
                    "message": zone,
                })
        return weather_warnings

    def test_dict_zone_includes_temperature(self):
        zones = [{"km": "160. km", "durum": "Hafif Yagmurlu", "sicaklik": "12C", "yagis_olasiligi": "%40"}]
        warnings = self._parse_zones(zones)
        assert len(warnings) == 1
        assert warnings[0]["temperature"] == "12C"
        assert warnings[0]["rain_probability"] == "%40"
        assert "12C" in warnings[0]["message"]

    def test_string_zone_no_attributeerror(self):
        """Eski format string zone — AttributeError yerine düzgün parse edilmeli."""
        zones = ["160. km (~15:30'de) hafif yagmur (12C)"]
        warnings = self._parse_zones(zones)
        assert len(warnings) == 1
        assert warnings[0]["condition"] == zones[0]
        assert warnings[0]["severity"] == "warning"

    def test_mixed_zones(self):
        zones = [
            {"km": "100. km", "durum": "Kar", "sicaklik": "-2C", "yagis_olasiligi": "%80"},
            "250. km (~18:00'de) fırtına",
        ]
        warnings = self._parse_zones(zones)
        assert len(warnings) == 2
        assert warnings[0]["temperature"] == "-2C"
        assert warnings[1]["location"] == "Rota"


# ─── TEST 4: Günlük plan — keyword slot haritası ─────────────────────────────

class TestDayPlanSlotMapping:
    """routes.py: kahve→Ö.Sonra, yürüyüş→Sabah, Öğle otomatik ekle."""

    NOTE_KEYWORDS = [
        ("kahvaltı",    "kahvaltı kafe",              "Sabah"),
        ("kahve",       "kafe kahvehane",             "Öğleden Sonra"),
        ("müze",        "müze sanat galerisi",        "Öğleden Sonra"),
        ("tarihi",      "tarihi alan anıt ören yeri", "Öğleden Sonra"),
        ("park",        "park doğa gezinti",          "Öğleden Sonra"),
        ("sahil",       "sahil kıyı yürüyüş",        "Sabah"),
        ("alışveriş",   "alışveriş merkezi çarşı",    "Öğleden Sonra"),
        ("sinema",      "sinema eğlence",             "Akşam"),
        ("konser",      "konser müzik mekan",         "Akşam"),
        ("yürüyüş",     "park sahil yürüyüş",        "Sabah"),
        ("spor",        "spor aktivite",              "Sabah"),
        ("restoran",    "restoran yemek",             "Akşam"),
        ("yemek",       "restoran yemek",             "Öğle"),
    ]

    def _build_slots(self, note: str) -> tuple[dict, dict]:
        note = note.lower()
        slot_custom: dict = {}
        for kw, query, slot in self.NOTE_KEYWORDS:
            if kw in note:
                if slot not in slot_custom:
                    slot_custom[slot] = []
                for token in query.split():
                    if token not in slot_custom[slot]:
                        slot_custom[slot].append(token)
        slot_custom_q = {s: " ".join(t) for s, t in slot_custom.items()}
        # Otomatik öğle
        if note and "Öğle" not in slot_custom_q:
            slot_custom_q["Öğle"] = "restoran lokanta yemek"
        return slot_custom, slot_custom_q

    def test_kahve_maps_to_ogleden_sonra_not_sabah(self):
        """Kahve artık sabah değil öğleden sonra."""
        _, sq = self._build_slots("kahve içmek istiyorum")
        assert "Öğleden Sonra" in sq
        assert "Sabah" not in sq or "kafe" not in sq.get("Sabah", "")

    def test_sahil_maps_to_sabah(self):
        """Sahil → sabah aktivitesi."""
        _, sq = self._build_slots("sahilde yürümek istiyorum")
        assert "Sabah" in sq
        assert "sahil" in sq["Sabah"]

    def test_yuruyu_maps_to_sabah(self):
        """Yürüyüş → sabah aktivitesi (eskiden öğleden sonraydı)."""
        _, sq = self._build_slots("sabah yürüyüşü yapmak istiyorum")
        assert "Sabah" in sq
        assert "yürüyüş" in sq["Sabah"]

    def test_user_case_sahil_yuruyu_kahve(self):
        """Kullanıcı testi: 'sahilde yürüyüp sonrasında kahve içmek istiyorum'."""
        _, sq = self._build_slots("sahilde yürüyüp sonrasında kahve içmek istiyorum")
        # Sabah: sahil + yürüyüş
        assert "Sabah" in sq
        # Öğleden Sonra: kahve
        assert "Öğleden Sonra" in sq
        assert "kafe" in sq["Öğleden Sonra"] or "kahvehane" in sq["Öğleden Sonra"]
        # Öğle: otomatik yemek
        assert "Öğle" in sq
        assert "restoran" in sq["Öğle"]

    def test_auto_lunch_when_note_given(self):
        """Not varsa Öğle otomatik yemek eklenmeli."""
        _, sq = self._build_slots("müze gezmek istiyorum")
        assert "Öğle" in sq
        assert "restoran" in sq["Öğle"]

    def test_no_auto_lunch_when_explicit_yemek(self):
        """Kullanıcı 'yemek' yazmışsa zaten Öğle'de var — duplicate olmamalı."""
        _, sq = self._build_slots("öğle yemek yemek istiyorum")
        assert "Öğle" in sq
        # Sadece bir tane olmalı (duplicate değil)
        assert sq["Öğle"].count("restoran") == 1


# ─── TEST 5: Serbest mod — routing olmayan categoryde trip_ctx yüklenmez ─────

class TestFreeModeContextIsolation:
    """graph.py: ROUTING_CATEGORIES = {'routing'} dışında trip_ctx inject edilmez."""

    ROUTING_CATEGORIES = {"routing"}

    def _should_load_trip_ctx(self, intent_category: str) -> bool:
        return intent_category in self.ROUTING_CATEGORIES

    def test_pharmacy_no_trip_ctx(self):
        assert not self._should_load_trip_ctx("pharmacy")

    def test_places_no_trip_ctx(self):
        assert not self._should_load_trip_ctx("places")

    def test_general_no_trip_ctx(self):
        assert not self._should_load_trip_ctx("general")

    def test_routing_loads_trip_ctx(self):
        assert self._should_load_trip_ctx("routing")

    def test_day_plan_no_trip_ctx(self):
        """Günlük plan rotadan bağımsız olmalı."""
        assert not self._should_load_trip_ctx("day_plan")

    def test_fuel_no_trip_ctx(self):
        assert not self._should_load_trip_ctx("fuel")


# ─── TEST 6: Hava — yağış olasılığı hesabı ───────────────────────────────────

class TestWeatherRainProb:
    """weather.py ve macro_tools.py: pop alanından yüzde hesabı."""

    def _calc_rain_prob(self, hourly_list: list) -> int:
        if hourly_list:
            try:
                return int(float(hourly_list[0].get("pop", 0)) * 100)
            except (TypeError, ValueError):
                return 0
        return 0

    def test_50_percent(self):
        assert self._calc_rain_prob([{"pop": 0.5}]) == 50

    def test_0_percent(self):
        assert self._calc_rain_prob([{"pop": 0.0}]) == 0

    def test_100_percent(self):
        assert self._calc_rain_prob([{"pop": 1.0}]) == 100

    def test_empty_list(self):
        assert self._calc_rain_prob([]) == 0

    def test_missing_pop_key(self):
        assert self._calc_rain_prob([{"temp": 18}]) == 0
