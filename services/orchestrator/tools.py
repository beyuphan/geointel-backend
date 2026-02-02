# services/orchestrator/tools.py

MANUAL_TOOLS = [
    # ==========================================
    # 🏙️ CITY AGENT ARAÇLARI (Eksik olanlar bunlardı)
    # ==========================================
    {
        "name": "get_route_data",
        "description": "İki nokta arasındaki en uygun rotayı, mesafeyi ve süreyi hesaplar. Rota çizildikten sonra çıkan 'polyline' verisi diğer araçlarda (mekan arama vb.) kullanılır.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Başlangıç noktası (Örn: Rize)"},
                "destination": {"type": "string", "description": "Varış noktası (Örn: Trabzon)"}
            },
            "required": ["origin", "destination"]
        }
    },
    {
        "name": "search_infrastructure_osm",
        "description": "Havalimanı, stadyum, park, hastane gibi KAMUSAL alanları bulur. Ticari mekanlar (restoran vb.) için bunu kullanma. Koordinat tespiti için ilk tercih bu olmalı.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "category": {"type": "string", "description": "airport, stadium, hospital, park"}
            },
            "required": ["lat", "lon", "category"]
        }
    },
    {
        "name": "search_places_google",
        "description": "Restoran, kafe, benzinlik gibi TİCARİ mekanları Google Maps üzerinden arar. Eğer aktif bir rota varsa 'route_polyline' parametresini mutlaka kullan.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Aranan yer (Örn: Köfteci)"},
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "route_polyline": {"type": "string", "description": "Rota üzerindeki mekanları bulmak için gerekli kod."}
            },
            "required": ["query", "lat", "lon"]
        }
    },
    {
        "name": "get_weather",
        "description": "Belirtilen koordinatın anlık hava durumunu verir.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"}
            },
            "required": ["lat", "lon"]
        }
    },
    {
        "name": "save_location",
        "description": "Kullanıcının beğendiği veya kaydetmek istediği bir konumu veritabanına işler.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "category": {"type": "string"},
                "note": {"type": "string"}
            },
            "required": ["name", "lat", "lon"]
        }
    },

    {
        "name": "get_toll_prices",
        "description": "Köprü, tünel ve otoyol geçiş ücretlerini sorgular. Rota planlamasında maliyet hesabı için kullanılır. Eğer kullanıcı 'Maliyet ne kadar?' diye sorarsa mutlaka bunu ve yakıt fiyatını kontrol et.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter_region": {
                    "type": "string", 
                    "description": "Filtrelemek için şehir veya bölge adı (Örn: 'İstanbul'). Hepsi için boş bırak."
                }
            },
            "required": []
        }
    },

    # ==========================================
    # 🕵️ INTEL AGENT ARAÇLARI
    # ==========================================
    {
        "name": "get_pharmacies",
        "description": "Belirtilen şehir ve ilçedeki nöbetçi eczaneleri bulur. Çıktıda eczane adını, adresini ve telefonunu mutlaka belirt. En yakın olanı vurgula.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Şehir adı (örn: samsun)"},
                "district": {"type": "string", "description": "İlçe adı (örn: atakum)"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "get_fuel_prices",
        "description": """
        Akaryakıt fiyatlarını getirir.
        
        KULLANIM KURALLARI:
        1. Uzun yolda sadece başlangıç noktasına bakma, rota üzerindeki ana ilçeleri de kontrol et.
        2. Sonucu sunarken MUTLAKA 'İlçe - Firma - Fiyat' sütunlu bir Markdown Tablosu oluştur.
        3. En ucuz istasyonu kalın harfle vurgula ve kullanıcıya oradan almasını öner.
        4. Dizel araç için Motorin, Benzinli araç için Benzin fiyatına odaklan.
        """,
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Şehir adı"},
                "district": {"type": "string", "description": "İlçe adı"}
            },
            "required": ["city", "district"]
        }
    },
    {
        "name": "get_city_events",
        "description": "Şehirdeki konser, tiyatro vb. etkinlikleri listeler. Trafiği etkileyebilecek büyük etkinlikleri özellikle belirt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Şehir adı"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "get_sports_events",
        "description": "Yaklaşan maçları ve bunların trafik etkisini getirir. Kullanıcı stadyuma gidiyorsa veya o bölgeden geçecekse trafik uyarısı yap.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },

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