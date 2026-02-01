# services/orchestrator/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    APP_NAME: str = "GeoIntel Orchestrator (Brain)"
    DEBUG: bool = False
    
    # LLM Anahtarı
    ANTHROPIC_API_KEY: str

    # Diğer Servislerin Adresleri (Docker içi iletişim)
    MCP_CITY_URL: str = "http://geo_mcp_city:8000"
    MCP_INTEL_URL: str = "http://mcp_intel:8001"

    # Ayarlar
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()