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
    },
    {
        "name": "plan_weather_aware_route",
        "description": "Kullanıcı anlamsal bir mekan aradığında ('sessiz bir yer bul', 'manzaralı kafe') ve oraya rota istediğinde TEK SEFERDE mekanı bulur, hava durumunu kontrol eder ve hava şartlarına uygun rota çizer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "current_lat": {"type": "number", "description": "Kullanıcının anlık enlemi"},
                "current_lon": {"type": "number", "description": "Kullanıcının anlık boylamı"},
                "location_name": {"type": "string", "description": "Aranacak bölge adı (örn: 'Kadıköy'). Boşsa anlık konum kullanılır."},
                "semantic_query": {"type": "string", "description": "Kullanıcının aradığı mekan türü (örn: sessiz kütüphane)"},
                "search_radius": {"type": "number", "description": "Arama çapı metre cinsinden, varsayılan 5000"}
            },
            "required": ["current_lat", "current_lon", "semantic_query"]
        }
    },
    {
        "name": "get_environmental_analysis",
        "description": "Satellite based environmental analysis (vegetation health, latest imagery). Use for nature, agriculture or urban green queries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Merkez enlem"},
                "lon": {"type": "number", "description": "Merkez boylam"},
                "analyze_vegetation": {"type": "boolean", "description": "Bitki örtüsü analizi yapılsın mı?", "default": True}
            },
            "required": ["lat", "lon"]
        }
    }
]

# main.py bu isimle import ediyor
LOCAL_TOOLS = MANUAL_TOOLS