-- 1. PostGIS Eklentisini Aç (Mekansal Zeka)
CREATE EXTENSION IF NOT EXISTS postgis;

-- 2. Mekanlar Tablosu
CREATE TABLE IF NOT EXISTS saved_places (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT DEFAULT 'Genel',
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    geom GEOMETRY(Point, 4326)
);

-- 3. Hız İndeksi
CREATE INDEX IF NOT EXISTS idx_saved_places_geom ON saved_places USING GIST(geom);