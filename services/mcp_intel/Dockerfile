# Hafif Python imajı kullanıyoruz (Playwright bağımlılığı kaldırıldı)
FROM python:3.11-slim

WORKDIR /app

# Sistem paketlerini güncelle
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# FastMCP sunucusunu başlat
CMD ["python", "server.py"]