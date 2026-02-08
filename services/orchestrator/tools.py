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
    }
]