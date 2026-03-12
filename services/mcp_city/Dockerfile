FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# BASİT VE NET: Sadece scripti çalıştır.
# Scriptin içindeki mcp.run() gerisini halledecek.
CMD ["python", "server.py"]