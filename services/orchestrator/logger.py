# services/mcp_city/logger.py
import sys
from loguru import logger

# Varsayılan logger'ı temizle (Çakışma olmasın)
logger.remove()

# Yeni format: [SAAT] [SEVİYE] MESAJ
# Örn: [21:30:05] [INFO] Rota hesaplanıyor...
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
    level="INFO",
    colorize=True
)

# Dışarıya bu süslü logger'ı veriyoruz
log = logger