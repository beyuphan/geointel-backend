FROM python:3.11-slim

WORKDIR /app

# Temel araçlar
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# --- DÜZELTME: PARÇA PARÇA KURULUM ---
# Hepsini tek seferde kurmak yerine, en ağır paketleri (numpy, rasterio) önden tek tek kuruyoruz.
# Bu sayede RAM şişmesi engellenir.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --prefer-binary numpy==2.2.0 && \
    pip install --no-cache-dir --prefer-binary rasterio && \
    pip install --no-cache-dir --prefer-binary -r requirements.txt

COPY . .

CMD ["python", "server.py"]