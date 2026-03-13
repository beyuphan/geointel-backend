import functools
import json
import logging
from pydantic import ValidationError

log = logging.getLogger(__name__)

def safe_tool(fallback_message="Bu araç geçici olarak kullanılamıyor."):
    """
    FastMCP araçları için evrensel hata yakalayıcı (Titanium Shield).
    Pydantic ValidationError, Timeout, API Hatalarını yakalar ve
    FastMCP'yi çökertecek (Exception) durumları JSON string veya güvenli sözlük olarak döner.
    LLM, bu JSON'u/Dict'i okuyarak hatayı anlar ve çökmek yerine alternatif bir yol arar.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                # Async kontrolü
                import inspect
                if inspect.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
                return result
            except ValidationError as ve:
                log.error(f"[Titanium Shield] Pydantic Validation Hatası ({func.__name__}): {ve}")
                return {"status": "error", "message": f"Parametre format hatası: {str(ve)}"}
            except Exception as e:
                log.exception(f"[Titanium Shield] Beklenmeyen Hata ({func.__name__}): {e}")
                return {"status": "error", "message": f"{fallback_message} | Detay: {str(e)}"}
        return wrapper
    return decorator
