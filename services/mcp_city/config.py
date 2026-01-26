# services/mcp_city/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict # <--- BURASI DEĞİŞTİ

class Settings(BaseSettings):
    # Uygulama Ayarları
    APP_NAME: str = "City MCP Service"
    DEBUG: bool = False
    
    # Veritabanı
    DATABASE_URL: str

    # API Anahtarları (Zorunlu)
    OPENWEATHER_API_KEY: str
    HERE_API_KEY: str
    GOOGLE_MAPS_API_KEY: str = "" 

    # --- YENİ KONFİGÜRASYON (PYDANTIC V2) ---
    # env_file: .env dosyasını oku
    # extra="ignore": Tanımadığın değişkenleri görmezden gel (Hata verme)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

# Ayarları tek bir obje olarak dışarı açıyoruz
settings = Settings()