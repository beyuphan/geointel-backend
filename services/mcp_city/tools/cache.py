import redis
from .config import settings
from logger import log

class RedisCache:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RedisCache, cls).__new__(cls)
            try:
                cls._instance.client = redis.Redis(
                    host=settings.REDIS_HOST,
                    port=settings.REDIS_PORT,
                    db=settings.REDIS_DB,
                    decode_responses=True # String olarak okumak için önemli
                )
                log.info("✅ Redis Bağlantısı Başarılı")
            except Exception as e:
                log.error(f"❌ Redis Bağlantı Hatası: {e}")
                cls._instance.client = None
        return cls._instance

    def set_route(self, polyline: str):
        """Son hesaplanan rotayı Redis'e yazar (1 saat ömürlü)."""
        if self.client:
            # "latest_route" anahtarına yazıyoruz. 
            # İleride çoklu kullanıcı gelirse buraya session_id ekleriz.
            self.client.set("latest_route", polyline, ex=3600) 

    def get_route(self):
        """Hafızadaki rotayı getirir."""
        if self.client:
            return self.client.get("latest_route")
        return None

# Singleton instance
redis_store = RedisCache()