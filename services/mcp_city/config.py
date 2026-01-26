# services/mcp_city/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Uygulama Ayarları
    APP_NAME: str = "City MCP Service"
    DEBUG: bool = False
    
    # Veritabanı
    DATABASE_URL: str

    # API Anahtarları (Zorunlu)
    # Eğer .env dosyasında bunlar yoksa uygulama BAŞLAMAZ (Güvenlik)
    OPENWEATHER_API_KEY: str
    HERE_API_KEY: str
    GOOGLE_MAPS_API_KEY: str = "" # Google opsiyonel olsun şimdilik (Here kullanıyoruz)

    class Config:
        # .env dosyasını otomatik okur
        env_file = ".env"

# Ayarları tek bir obje olarak dışarı açıyoruz
settings = Settings()