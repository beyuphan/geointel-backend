from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    APP_NAME: str = "GeoIntel Orchestrator (Brain)"
    DEBUG: bool = False
    
    ANTHROPIC_API_KEY: str
    GOOGLE_API_KEY: str

    # Default değerler
    MCP_CITY_URL: str = "http://mcp_city:8000"
    MCP_INTEL_URL: str = "http://mcp_intel:8001"

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