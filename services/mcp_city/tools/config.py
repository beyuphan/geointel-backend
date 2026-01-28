# services/mcp_city/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # --- API ANAHTARLARI (Otomatik .env'den okur) ---
    DATABASE_URL: str
    OPENWEATHER_API_KEY: str
    HERE_API_KEY: str
    GOOGLE_MAPS_API_KEY: str
    
    # --- URL AYARLARI ---
    OVERPASS_URLS: list = [
        "https://overpass-api.de/api/interpreter",       # Ana Sunucu
        "https://overpass.kumi.systems/api/interpreter", # Hızlı Mirror
        "https://lz4.overpass-api.de/api/interpreter",   # Yedek Mirror
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter" # Rus Mirror (Bazen hayat kurtarır)
    ]
    HERE_ROUTING_URL: str = "https://router.hereapi.com/v8/routes"
    GOOGLE_PLACES_URL: str = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    OPENWEATHER_URL: str = "https://api.openweathermap.org/data/3.0/onecall"

    # --- OSM TAG MAPPER (Sihirli Sözlük) ---
    # LLM'in gönderdiği basit kategoriyi OSM sorgusuna çevirir
    OSM_TAG_MAP: dict = {
        "airport": ['"aeroway"="aerodrome"'],
        "park": ['"leisure"="park"', '"leisure"="garden"'],
        "square": ['"place"="square"', '"landuse"="plaza"'],
        "mosque": ['"amenity"="place_of_worship"["religion"="muslim"]'],
        "hospital": ['"amenity"="hospital"'],
        "school": ['"amenity"="school"', '"amenity"="university"']
    }

    REDIS_HOST: str = "geo_redis" 
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    
    class Config:
        env_file = ".env"
        extra = "ignore" # .env içinde fazladan bişey varsa patlama

settings = Settings()