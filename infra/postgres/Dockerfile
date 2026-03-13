FROM postgis/postgis:15-3.3

# Install pgvector and pgRouting in one image
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        postgresql-15-pgrouting \
        postgresql-15-pgvector \
    && rm -rf /var/lib/apt/lists/*
