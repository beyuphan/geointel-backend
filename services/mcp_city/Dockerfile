# Hafif Python imajı
FROM python:3.11-slim

# Çalışma klasörü
WORKDIR /app

# Sistem kütüphanelerini güncelle (Postgres ve C++ derleyicileri için)
# asyncpg ve diğer coğrafi kütüphaneler bazen gcc ister.
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Gereksinimleri kopyala ve kur
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kodları kopyala
COPY . .

# Konteyner çalıştığında bu komutu çalıştır (FastAPI/MCP sunucusu)
# Not: server.py dosyasını birazdan oluşturacağız.
CMD ["uvicorn", "server:mcp_api", "--host", "0.0.0.0", "--port", "8000"]