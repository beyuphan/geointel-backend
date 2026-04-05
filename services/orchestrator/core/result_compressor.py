"""
Tool Result Compressor — V2.5
Her araç sonucu LLM'e gitmeden önce bu modülden geçer.
Gereksiz koordinat dizileri, tekrarlı alan adları ve büyük JSON payloadları budanır.
"""

import json
from typing import Any

# Her araç için maksimum sonuç sayısı ve korunacak alanlar
COMPRESSION_RULES = {
    "get_route_data": {
        "keep_fields": [
            "mesafe_km", "sure_dk", "origin", "destination",
            "traffic_summary", "traffic_status", "traffic_color",
            "route_warnings", "summary", "source_system",
            "source", "delay_min", "avg_speed_kmh",
            "rain_factor_applied", "alternative_count"
        ],
        "max_alternatives": 3,
        "strip_fields": ["polyline", "polyline_encoded", "coordinates", "legs", "maneuvers"]
    },
    "search_places_google": {
        "max_results": 8,
        "keep_place_fields": ["name", "address", "rating", "types", "description", "coords", "open_now", "eta_from_route_min", "user_ratings_total"]
    },
    "search_hybrid_places": {
        "max_results": 8,
        "keep_place_fields": ["name", "address", "rating", "types", "description", "lat", "lon", "fusion_status", "is_open", "user_ratings_total"]
    },
    "get_fuel_prices": {
        "max_results": 10,
        "keep_fields": ["company", "gasoline", "diesel", "lpg", "district", "city"]
    },
    "get_pharmacies": {
        "max_results": 8,
        "keep_fields": ["name", "address", "phone", "district", "coordinates"]
    },
    "get_events": {
        "max_results": 10,
        "keep_fields": ["title", "venue", "date", "category", "link", "source"]
    },
    "get_sports_matches": {
        "max_results": 10,
        "keep_fields": ["match", "time", "stadium", "city", "warning"]
    }
}


def compress_result(tool_name: str, result: Any) -> Any:
    """
    Araç sonucunu LLM'e göndermeden önce sıkıştırır.
    Geri dönüş tipi orijinalle aynıdır (dict veya str).
    """
    if not isinstance(result, (dict, list, str)):
        return result

    # String olan sonuçları parse etmeyi dene
    parsed = result
    was_string = False
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            was_string = True
        except (json.JSONDecodeError, TypeError):
            return result  # Parse edilemiyorsa dokunma

    rules = COMPRESSION_RULES.get(tool_name)
    if not rules:
        # Bilinen bir araç değilse, basit boyut limiti uygula
        return _cap_size(result, was_string)

    compressed = _apply_rules(tool_name, parsed, rules)

    if was_string:
        return json.dumps(compressed, ensure_ascii=False)
    return compressed


def _apply_rules(tool_name: str, data: Any, rules: dict) -> Any:
    """Araç tipine göre kuralları uygular."""
    
    if tool_name == "get_route_data":
        return _compress_route(data, rules)
    
    if tool_name in ("search_places_google", "search_hybrid_places"):
        return _compress_places(data, rules)
    
    if tool_name in ("get_fuel_prices", "get_pharmacies", "get_events", "get_sports_matches"):
        return _compress_list_data(data, rules)
    
    return data


def _compress_route(data: dict, rules: dict) -> dict:
    """Rota sonucunu sıkıştır — sadece özet bilgileri tut."""
    if not isinstance(data, dict):
        return data
    
    result = {}
    for field in rules.get("keep_fields", []):
        if field in data:
            result[field] = data[field]
    
    # Alternatifleri sınırla ve polyline'ları kaldır
    if "alternatives" in data:
        alts = data["alternatives"][:rules.get("max_alternatives", 3)]
        stripped_alts = []
        for alt in alts:
            clean_alt = {k: v for k, v in alt.items() if k not in rules.get("strip_fields", [])}
            stripped_alts.append(clean_alt)
        result["alternatives"] = stripped_alts
        result["alternative_count"] = len(data.get("alternatives", []))
    
    # Polyline proxy mesajını koru ama ham veriyi kaldır
    for strip_field in rules.get("strip_fields", []):
        if strip_field in data and isinstance(data[strip_field], str) and len(data[strip_field]) < 50:
            result[strip_field] = data[strip_field]  # Kısa proxy string'leri koru
    
    return result


def _compress_places(data: Any, rules: dict) -> Any:
    """Mekan listesini sıkıştır."""
    max_results = rules.get("max_results", 5)
    keep_fields = rules.get("keep_place_fields", [])
    
    # Liste formatı
    if isinstance(data, list):
        places = data[:max_results]
        return [_pick_fields(p, keep_fields) for p in places if isinstance(p, dict)]
    
    # Dict formatı (status + data/places içinde)
    if isinstance(data, dict):
        result = {k: v for k, v in data.items() if k not in ("places", "strict_route_places", "relaxed_route_places", "data")}
        
        for key in ("places", "strict_route_places", "relaxed_route_places", "data"):
            if key in data and isinstance(data[key], list):
                result[key] = [_pick_fields(p, keep_fields) for p in data[key][:max_results] if isinstance(p, dict)]
        
        return result
    
    return data


def _compress_list_data(data: Any, rules: dict) -> Any:
    """Genel liste verilerini sıkıştır (fuel, pharmacy, events)."""
    max_results = rules.get("max_results", 5)
    keep_fields = rules.get("keep_fields", [])
    
    if isinstance(data, dict) and "data" in data:
        items = data["data"]
        if isinstance(items, list):
            data["data"] = [_pick_fields(item, keep_fields) for item in items[:max_results] if isinstance(item, dict)]
        return data
    
    if isinstance(data, list):
        return [_pick_fields(item, keep_fields) for item in data[:max_results] if isinstance(item, dict)]
    
    return data


def _pick_fields(obj: dict, fields: list) -> dict:
    """Sadece belirtilen alanları seç."""
    if not fields:
        return obj
    return {k: v for k, v in obj.items() if k in fields}


def _cap_size(result: Any, was_string: bool, max_chars: int = 4000) -> Any:
    """Bilinmeyen araçlar için basit boyut sınırı uygula."""
    if was_string and isinstance(result, str) and len(result) > max_chars:
        return result[:max_chars] + "... [KISALTILDI]"
    return result
