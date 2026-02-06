-- 1. PostGIS Eklentisini Aç (Mekansal Zeka)
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;
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


-- services/mcp_intel verileri için tablolar

-- 1. Akaryakıt Fiyatları
CREATE TABLE IF NOT EXISTS fuel_prices (
    id SERIAL PRIMARY KEY,
    city VARCHAR(50) NOT NULL,      -- örn: 'samsun'
    district VARCHAR(50) NOT NULL,  -- örn: 'atakum'
    company VARCHAR(50) NOT NULL,   -- örn: 'Opet'
    gasoline NUMERIC(10,2),          -- Benzin Fiyatı
    diesel NUMERIC(10,2),            -- Motorin Fiyatı
    lpg NUMERIC(10,2),               -- LPG Fiyatı
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(city, district, company) -- Aynı firmanın aynı ilçedeki verisi tekrar etmesin
);

-- 2. Nöbetçi Eczaneler (Her gün silinip yeniden yazılacak)
CREATE TABLE IF NOT EXISTS pharmacies (
    id SERIAL PRIMARY KEY,
    city VARCHAR(50) NOT NULL,
    district VARCHAR(50),
    name VARCHAR(100) NOT NULL,
    address TEXT,
    phone VARCHAR(20),
    coordinates VARCHAR(50), -- "Lat,Lon" formatında
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Spor Müsabakaları (Trafik Etkisi İçin)
CREATE TABLE IF NOT EXISTS sports_matches (
    id SERIAL PRIMARY KEY,
    home_team VARCHAR(100),
    away_team VARCHAR(100),
    match_date TIMESTAMP,
    stadium VARCHAR(100),
    city VARCHAR(50),
    traffic_impact_level INTEGER DEFAULT 1, -- 1: Düşük, 2: Orta, 3: Yüksek (Derbi vb.)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. Etkinlikler
CREATE TABLE IF NOT EXISTS city_events (
    id SERIAL PRIMARY KEY,
    city VARCHAR(50),
    title VARCHAR(200),
    venue VARCHAR(100),
    event_date VARCHAR(50), -- Metin olarak gelebilir bazen
    category VARCHAR(50),   -- Konser, Tiyatro vb.
    source_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


-- ==========================================
-- 👤 KULLANICI PROFİLİ VE HAFIZA SİSTEMİ
-- ==========================================

-- 1. Kullanıcılar (Mobil App için temel)
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(50) UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Araç Bilgileri (Yakıt hesabı için kritik)
CREATE TABLE IF NOT EXISTS user_vehicles (
    id SERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    vehicle_name VARCHAR(50), -- Örn: "Benim Kara Şimşek"
    fuel_type VARCHAR(20),    -- 'gasoline', 'diesel', 'lpg', 'electric'
    avg_consumption NUMERIC(4,1), -- 100km'de kaç litre? (Örn: 6.5)
    is_primary BOOLEAN DEFAULT FALSE -- Varsayılan araç mı?
);

-- 3. Kayıtlı Konumlar (Ev, İş, Favoriler)
CREATE TABLE IF NOT EXISTS saved_locations (
    id SERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(50), -- "Ev", "İş", "Ayşe Teyzem"
    address TEXT,
    coordinates VARCHAR(50), -- "41.0201,40.5234"
    category VARCHAR(30) -- 'home', 'work', 'favorite'
);

-- 4. Kullanıcı Tercihleri (Takım, ilgi alanı vb.)
CREATE TABLE IF NOT EXISTS user_preferences (
    id SERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    key VARCHAR(50),   -- "football_team", "music_genre"
    value VARCHAR(100) -- "Trabzonspor", "Rock"
);

-- TEST KULLANICISI (Senin için bir tane oluşturalım)
-- Bu sayede sistemi denerken "default_user" üzerinden test edebiliriz.
INSERT INTO users (username) VALUES ('test_pilot') ON CONFLICT DO NOTHING;