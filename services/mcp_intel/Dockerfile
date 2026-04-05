# Playwright için tam bağımlılıklı Python imajı
FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

# Sistem paketlerini güncelle
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium Browser'ı yükle
RUN playwright install chromium

COPY . .

# FastMCP sunucusunu başlat
CMD ["python", "server.py"]