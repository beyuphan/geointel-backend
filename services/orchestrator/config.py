from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    APP_NAME: str = "GeoIntel Orchestrator (Brain)"
    DEBUG: bool = False

    ANTHROPIC_API_KEY: str
    GOOGLE_API_KEY: str

    # MCP Servis URL'leri
    MCP_CITY_URL: str = "http://mcp_city:8000"
    MCP_INTEL_URL: str = "http://mcp_intel:8001"
    MCP_SATELLITE_URL: str = "http://mcp_satellite:8002"

    # Database (Orchestrator için — profile/history sorguları)
    DATABASE_URL: str = "postgresql://user:password@geo_db:5432/geodb"

    # Redis
    REDIS_HOST: str = "geo_redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    # Push Notification (opsiyonel)
    FIREBASE_CREDENTIALS_PATH: str = "firebase_credentials.json"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

# Ayarları yükle
settings = Settings()

# --- URL DÜZELTME (GARANTİ YÖNTEM) ---
# Sınıfın içinde değil, nesne oluştuktan hemen sonra müdahale ediyoruz.
# Böylece SyntaxError veya Pydantic hatası riski SIFIR oluyor.

if settings.MCP_CITY_URL and not settings.MCP_CITY_URL.startswith("http"):
    settings.MCP_CITY_URL = f"http://{settings.MCP_CITY_URL}"

if settings.MCP_INTEL_URL and not settings.MCP_INTEL_URL.startswith("http"):
    settings.MCP_INTEL_URL = f"http://{settings.MCP_INTEL_URL}"

if settings.MCP_SATELLITE_URL and not settings.MCP_SATELLITE_URL.startswith("http"):
    settings.MCP_SATELLITE_URL = f"http://{settings.MCP_SATELLITE_URL}"