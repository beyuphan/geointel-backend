# services/orchestrator/tools.py

MANUAL_TOOLS = [
    # ==========================================
    # 🧠 LOCAL ORCHESTRATOR ARAÇLARI
    # ==========================================
    {
        "name": "remember_info",
        "description": "Kullanıcının tercihini (takım, yakıt tipi, ev adresi) hafızaya kaydeder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "'team', 'fuel_type', 'home_location'"},
                "value": {"type": "string", "description": "Kaydedilecek değer"}
            },
            "required": ["category", "value"]
        }
    },
    {
        "name": "evaluate_route_strategy",
        "description": "Kullanıcı uzun yola gideceğini ve yakıt alacağını söylediğinde TEK SEFERDE rotayı çizer, istasyonları bulur ve en ucuz yakıt fiyatlarını analiz eder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Başlangıç noktası (Örn: Rize)"},
                "destination": {"type": "string", "description": "Varış noktası (Örn: İstanbul)"},
                "fuel_type": {"type": "string", "description": "Yakıt tipi, varsayılan: benzin"}
            },
            "required": ["origin", "destination"]
        }
    }
]