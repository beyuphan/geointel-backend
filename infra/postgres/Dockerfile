# pgrouting/pgrouting:15-3.5-3.7 = PostgreSQL 15 + PostGIS 3.5 + pgRouting 3.7
# Bu image içinde pgRouting 3.7 shared library sorunsuz gelir.
FROM pgrouting/pgrouting:15-3.5-3.7

# pgvector ekle (Spatial RAG için)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        postgresql-15-pgvector \
    && rm -rf /var/lib/apt/lists/*
